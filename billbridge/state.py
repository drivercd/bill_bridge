"""Central state object carried between workflow steps (M2 report, section 3.2)."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ProjectRecord:
    project_id: str
    client_id: str | None = None
    unit_type: str | None = None
    hours: float | None = None
    units: int | None = None
    hours_per_unit: float | None = None
    expected_hours_per_unit: float | None = None
    variance: float | None = None
    # loaded -> validated | invalid -> classified -> resolved | escalated
    # -> approved | rejected | pending_review
    status: str = "loaded"
    validation_errors: list[str] = field(default_factory=list)
    exception_flag: bool | None = None
    exception_type: str | None = None  # "none" | "simple" | "complex"
    exception_details: str | None = None
    resolution_source: str | None = None  # "auto_clear" | "rules_engine" | "ai_agent"
    recommendation: dict | None = None
    retry_count: int = 0
    supervisor_decision: str | None = None  # "approve" | "modify" | "reject"
    final_billable_hours: float | None = None
    final_adjustments: str | None = None


@dataclass
class RunState:
    billing_period: str | None
    project_filter: str | None = None
    run_id: str = field(default_factory=lambda: f"run-{uuid.uuid4().hex[:8]}")
    started_at: str = field(default_factory=utc_now)
    status: str = "started"
    current_step: str | None = None
    stop_reason: str | None = None
    records: dict[str, ProjectRecord] = field(default_factory=dict)
    notifications: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    model_calls: int = 0

    def notify_supervisor(self, message: str) -> None:
        self.notifications.append(message)

    def to_dict(self) -> dict:
        return asdict(self)

    def save_checkpoint(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2))
