"""Step 10: the human checkpoint (M3).

Every exception (rules engine or AI agent) becomes a review item in the
SQLite review queue. The item carries exactly what the reviewer needs: the
proposed action, the evidence, the applicable rule, and any conflict that
code can detect. Approval is bound to a content version and an expiry.

`SimulatedSupervisor` from M2 is kept for scripted runs and tests; its
decisions are turned into ordinary decision events, so they go through the
same checks as a person's decision.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .state import ProjectRecord

DECISIONS = {"approve", "modify", "reject"}
ACTION = "stage_line_for_draft_invoice"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def find_conflicts(record: ProjectRecord, rules: dict, recommendation: dict | None, section_text: dict[str, str]) -> list[str]:
    """Deterministic checks that put disagreements in front of the reviewer.

    These do not block the item. They make sure a well-formed, cited,
    confident recommendation can't hide a second reading of the contract.
    """
    conflicts: list[str] = []
    standard = rules["standard_hours_per_unit"]
    if recommendation is None:
        conflicts.append(
            f"No recommendation: the AI agent failed {record.retry_count} attempts. "
            "The reviewer must set billable hours (modify) or reject."
        )
        return conflicts

    hours = recommendation.get("recommended_billable_hours")
    if hours is not None and record.hours is not None and hours > record.hours + 1e-9:
        conflicts.append(f"Proposed {hours} h is more than the {record.hours} h actually recorded.")
    if hours is not None and record.units:
        hard_cap = round(record.units * rules["complex_cap_hours_per_unit"], 2)
        if hours > hard_cap + 1e-9:
            conflicts.append(f"Proposed {hours} h exceeds the contract hard cap of {hard_cap} h.")

    cited = [c["section_id"] for c in recommendation.get("citations", [])]
    for citation in recommendation.get("citations", []):
        text = section_text.get(citation["section_id"])
        if text is None:
            conflicts.append(f"Cited §{citation['section_id']} is not in this client's contract.")
        elif _norm(citation["quote"]).rstrip(".…") not in _norm(text):
            conflicts.append(f"Quote attributed to §{citation['section_id']} is not found verbatim in that section.")

    if "4.2" in cited and record.units:
        band_cap = round(record.units * standard * (1 + rules["tolerance"]), 2)
        if hours is not None and hours > band_cap:
            conflicts.append(
                "§4.2 (make-ready, up to "
                f"{rules['complex_cap_hours_per_unit']} h/unit) applies only with documented make-ready "
                "justification, and the source data contains none. Without it, §4.1 caps billing at "
                f"{band_cap} h ({round(hours - band_cap, 2)} h less)."
            )
    if record.variance is not None and record.variance < -rules["complex_threshold"]:
        conflicts.append("Hours are more than 50% under standard: §4.3 lets the client request a unit audit before payment.")
    return conflicts


def build_proposal(record: ProjectRecord, rules: dict, run_id: str, billing_period: str, section_text: dict[str, str]) -> dict:
    """The content the reviewer approves. Its hash is the content version."""
    rec = record.recommendation
    citations = (rec or {}).get("citations", [])
    applicable = [
        {"section_id": c["section_id"], "quote": c["quote"], "section_text": section_text.get(c["section_id"], "")}
        for c in citations
    ]
    return {
        "action": ACTION,
        "target": {
            "run_id": run_id,
            "billing_period": billing_period,
            "client_id": record.client_id,
            "project_id": record.project_id,
        },
        "proposed_billable_hours": rec.get("recommended_billable_hours") if rec else None,
        "evidence": {
            "recorded_hours": record.hours,
            "units": record.units,
            "unit_type": record.unit_type,
            "hours_per_unit": record.hours_per_unit,
            "standard_hours_per_unit": rules["standard_hours_per_unit"],
            "variance": record.variance,
            "exception_type": record.exception_type,
            "exception_details": record.exception_details,
        },
        "source": record.resolution_source or "ai_agent_failed",
        "recommendation": rec.get("recommendation") if rec else None,
        "explanation": rec.get("explanation") if rec else None,
        "confidence": rec.get("confidence") if rec else None,
        "applicable_rule": applicable,
        "conflicts": find_conflicts(record, rules, rec, section_text),
        "caveats": (
            ["Confidence is self-reported by the model and is not calibrated (no labeled set yet)."]
            if record.resolution_source == "ai_agent"
            else []
        ),
        "not_authorized": "sending the invoice to the client, changing rates, or changing client billing rules",
    }


def render_review(item: dict) -> str:
    """What the reviewer sees at the command-line checkpoint."""
    p = json.loads(item["proposal_json"])
    ev, t = p["evidence"], p["target"]
    hours = p["proposed_billable_hours"]
    lines = [
        "=" * 78,
        f"REVIEW {item['review_id']}   project {t['project_id']}   client {t['client_id']}   period {t['billing_period']}",
        f"status: {item['status']}   decided by: {item['reviewer_role']}   content version: {item['content_version']}",
        f"expires: {item['expires_at'] or 'no automatic expiry (AR manager holds it)'}",
        "-" * 78,
        "PROPOSED ACTION",
        f"  Stage {hours if hours is not None else '(no proposal)'} billable hours on the draft invoice "
        f"(source: {p['source']}).",
        f"  Approval does NOT authorize: {p['not_authorized']}.",
        "EVIDENCE",
        f"  recorded {ev['recorded_hours']} h for {ev['units']} {ev['unit_type']} units = {ev['hours_per_unit']} h/unit "
        f"vs {ev['standard_hours_per_unit']} standard ({ev['variance']:+.0%})" if ev["variance"] is not None else
        f"  recorded {ev['recorded_hours']} h for {ev['units']} units",
        f"  {ev['exception_type']} exception: {ev['exception_details']}",
    ]
    if p["explanation"]:
        lines.append(f"  explanation: {p['explanation']}")
    if p["confidence"] is not None:
        lines.append(f"  confidence: {p['confidence']}")
    lines.append("APPLICABLE RULE")
    for rule in p["applicable_rule"] or [{"section_id": "-", "quote": "none cited", "section_text": ""}]:
        lines.append(f"  §{rule['section_id']}: \"{rule['quote']}\"")
    lines.append("CONFLICTS")
    lines.extend(f"  ! {c}" for c in p["conflicts"]) if p["conflicts"] else lines.append("  none detected")
    for caveat in p["caveats"]:
        lines.append(f"  note: {caveat}")
    lines.append("=" * 78)
    return "\n".join(lines)


class SimulatedSupervisor:
    """Applies scripted decisions per project, else a default decision.

    Escalated records (no recommendation) stay in the review queue unless a
    scripted decision gives explicit billable hours.
    """

    def __init__(self, decisions: dict[str, dict] | None = None, default_decision: str | None = "approve"):
        self.decisions = decisions or {}
        self.default_decision = default_decision

    @classmethod
    def from_file(cls, path: Path, default_decision: str | None = "approve") -> "SimulatedSupervisor":
        return cls(json.loads(Path(path).read_text()), default_decision)

    def review(self, record: ProjectRecord) -> dict | None:
        """Return {"decision", "billable_hours", "note"} or None to leave it in the queue."""
        scripted = self.decisions.get(record.project_id)
        if scripted:
            decision = scripted.get("decision")
            if decision not in DECISIONS:
                raise ValueError(f"invalid supervisor decision for {record.project_id}: {decision!r}")
            if decision == "modify" and scripted.get("billable_hours") is None:
                raise ValueError(f"'modify' for {record.project_id} needs billable_hours")
            return scripted
        if record.recommendation is None or self.default_decision is None:
            return None
        return {"decision": self.default_decision, "note": "simulated default decision"}
