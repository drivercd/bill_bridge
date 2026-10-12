"""Step 2: deterministic data loader for hours, units, client rules and contracts."""

from __future__ import annotations

import csv
import json
from pathlib import Path

HOURS_COLUMNS = {"entry_id", "project_id", "employee_id", "role", "work_date", "hours", "billing_period"}
UNITS_COLUMNS = {"project_id", "client_id", "unit_type", "units_completed", "billing_period"}


class DataLoadError(Exception):
    """A source file is missing or does not match the expected schema. Stops the run."""


def read_csv(path: Path, required_columns: set[str]) -> list[dict]:
    if not path.exists():
        raise DataLoadError(f"source file not found: {path}")
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        missing = required_columns - set(reader.fieldnames or [])
        if missing:
            raise DataLoadError(f"{path.name} is missing columns: {sorted(missing)}")
        return [{k: (v or "").strip() for k, v in row.items()} for row in reader]


def load_clients(data_dir: Path) -> dict:
    path = data_dir / "clients.json"
    if not path.exists():
        raise DataLoadError(f"client rules not found: {path}")
    return json.loads(path.read_text())


def load_period(data_dir: Path, billing_period: str, project_id: str | None = None):
    """Return (hours_rows, units_rows) for one billing period, optionally one project."""
    hours = read_csv(data_dir / "time_tracking.csv", HOURS_COLUMNS)
    units = read_csv(data_dir / "units.csv", UNITS_COLUMNS)

    def keep(row: dict) -> bool:
        if row["billing_period"] != billing_period:
            return False
        return project_id is None or row["project_id"] == project_id

    return [r for r in hours if keep(r)], [r for r in units if keep(r)]
