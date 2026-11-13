"""Execution log: one entry per step so a run can be reconstructed (M2 report, 3.2)."""

from __future__ import annotations

import json
from pathlib import Path

from .state import utc_now


class ExecutionLog:
    """Appends each entry to the SQLite store as it happens (M3) and keeps the
    JSON export from M2. On resume the entries are reloaded from the store."""

    def __init__(self, run_id: str, store=None):
        self.run_id = run_id
        self.store = store
        self.entries: list[dict] = store.log_entries(run_id) if store else []

    def log(
        self,
        step: str,
        actor: str,
        status: str,
        input_summary: str = "",
        output_summary: str = "",
        project_id: str | None = None,
        stop_reason: str | None = None,
        **extra,
    ) -> None:
        entry = {
            "run_id": self.run_id,
            "step": step,
            "timestamp": utc_now(),
            "actor": actor,  # "code" | "model" | "human"
            "status": status,
            "input_summary": input_summary,
            "output_summary": output_summary,
        }
        if project_id:
            entry["project_id"] = project_id
        if stop_reason:
            entry["stop_reason"] = stop_reason
        entry.update(extra)
        self.entries.append(entry)
        if self.store is not None:
            self.store.append_log(entry)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.entries, indent=2))
