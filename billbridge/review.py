"""Step 10: supervisor review. Simulated for M2; a review UI is planned for M3."""

from __future__ import annotations

import json
from pathlib import Path

from .state import ProjectRecord

DECISIONS = {"approve", "modify", "reject"}


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
        return cls(json.loads(path.read_text()), default_decision)

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
