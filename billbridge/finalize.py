"""Step 11: write approved records to invoicing staging and everything else to the review queue."""

from __future__ import annotations

import json
from pathlib import Path

from .state import RunState


def staging_rows(state: RunState) -> list[dict]:
    rows = []
    for record in state.records.values():
        if record.status != "approved":
            continue
        citations = (record.recommendation or {}).get("citations", [])
        rows.append(
            {
                "run_id": state.run_id,
                "billing_period": state.billing_period,
                "project_id": record.project_id,
                "client_id": record.client_id,
                "unit_type": record.unit_type,
                "units": record.units,
                "recorded_hours": record.hours,
                "billable_hours": record.final_billable_hours,
                "resolution_source": record.resolution_source,
                "supervisor_decision": record.supervisor_decision,
                "adjustment_note": record.final_adjustments,
                "contract_citations": [c["section_id"] for c in citations],
            }
        )
    return rows


def review_queue_rows(state: RunState) -> list[dict]:
    rows = []
    for record in state.records.values():
        if record.status == "approved":
            continue
        if record.status == "invalid":
            reason = "; ".join(record.validation_errors)
        elif record.status == "rejected":
            reason = record.final_adjustments or "rejected by supervisor"
        elif record.status == "escalated":
            reason = f"AI agent failed {record.retry_count} attempts; needs human review"
        else:
            reason = "awaiting supervisor decision"
        rows.append(
            {
                "project_id": record.project_id,
                "status": record.status,
                "reason": reason,
                "recommendation": record.recommendation,
            }
        )
    return rows


def compute_metrics(state: RunState) -> dict:
    records = list(state.records.values())
    valid = [r for r in records if r.status != "invalid"]
    by_type = {t: sum(1 for r in valid if r.exception_type == t) for t in ("none", "simple", "complex")}
    return {
        "projects": len(records),
        "valid_projects": len(valid),
        "invalid_projects": len(records) - len(valid),
        "auto_cleared": by_type["none"],
        "simple_exceptions": by_type["simple"],
        "complex_exceptions": by_type["complex"],
        "escalated": sum(1 for r in records if r.status == "escalated"),
        "approved": sum(1 for r in records if r.status == "approved"),
        "auto_clear_rate": round(by_type["none"] / len(valid), 4) if valid else None,
        "model_calls": state.model_calls,
    }


def write_outputs(state: RunState, out_dir: Path) -> None:
    (out_dir / "invoicing_staging.json").write_text(json.dumps(staging_rows(state), indent=2))
    (out_dir / "review_queue.json").write_text(json.dumps(review_queue_rows(state), indent=2))
