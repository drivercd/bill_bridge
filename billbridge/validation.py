"""Step 3: validate completeness, duplicates and formats, then match hours to units."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date

from .state import ProjectRecord


def _parse_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value: str) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


def _valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def validate_and_match(hours_rows: list[dict], units_rows: list[dict], clients: dict) -> dict[str, ProjectRecord]:
    """Build one ProjectRecord per project. Records that fail validation are marked invalid."""
    entry_counts = Counter(r["entry_id"] for r in hours_rows if r["entry_id"])
    hours_by_project: dict[str, list[dict]] = defaultdict(list)
    units_by_project: dict[str, list[dict]] = defaultdict(list)
    for row in hours_rows:
        hours_by_project[row["project_id"] or "<missing project_id>"].append(row)
    for row in units_rows:
        units_by_project[row["project_id"] or "<missing project_id>"].append(row)

    records: dict[str, ProjectRecord] = {}
    for project_id in sorted(set(hours_by_project) | set(units_by_project)):
        record = ProjectRecord(project_id=project_id)
        errors: list[str] = []
        if project_id.startswith("<"):
            errors.append("record has no project_id")

        entries = hours_by_project.get(project_id, [])
        if not entries:
            errors.append("project not found in time-tracking records")
        total_hours = 0.0
        for entry in entries:
            entry_id = entry["entry_id"] or "<no entry_id>"
            if not entry["entry_id"]:
                errors.append("time entry is missing entry_id")
            elif entry_counts[entry_id] > 1:
                errors.append(f"duplicate time entry {entry_id}")
            if not _valid_date(entry["work_date"]):
                errors.append(f"invalid work_date '{entry['work_date']}' on {entry_id}")
            hours = _parse_float(entry["hours"])
            if hours is None or not 0 < hours <= 24:
                errors.append(f"invalid hours '{entry['hours']}' on {entry_id}")
            else:
                total_hours += hours

        unit_rows = units_by_project.get(project_id, [])
        units = None
        if not unit_rows:
            errors.append("project not found in engineering units records")
        elif len(unit_rows) > 1:
            errors.append("more than one units record for project")
        else:
            row = unit_rows[0]
            record.client_id = row["client_id"] or None
            record.unit_type = row["unit_type"] or None
            units = _parse_int(row["units_completed"])
            if units is None or units <= 0:
                errors.append(f"missing or invalid units_completed '{row['units_completed']}'")
            if record.client_id not in clients:
                errors.append(f"no contract on file for client '{record.client_id}'")

        record.hours = round(total_hours, 2) if entries else None
        record.units = units
        if errors:
            record.status = "invalid"
            # De-duplicate while keeping order (a duplicate entry is reported once).
            record.validation_errors = list(dict.fromkeys(errors))
        else:
            rules = clients[record.client_id]
            record.expected_hours_per_unit = rules["standard_hours_per_unit"]
            record.hours_per_unit = round(record.hours / units, 3)
            record.variance = round(
                (record.hours_per_unit - record.expected_hours_per_unit) / record.expected_hours_per_unit, 4
            )
            record.status = "validated"
        records[project_id] = record
    return records
