"""Step 7a: predefined rules for simple exceptions. Returns None when no rule matches."""

from __future__ import annotations

from .state import ProjectRecord


def resolve(record: ProjectRecord, rules: dict) -> dict | None:
    variance = record.variance
    tolerance = rules["tolerance"]
    standard = rules["standard_hours_per_unit"]

    if variance is not None and tolerance < variance <= rules["complex_threshold"]:
        cap = round(record.units * standard * (1 + tolerance), 2)
        return {
            "exception_classification": "overrun_within_capped_band",
            "explanation": (
                f"{record.hours} hours for {record.units} units is {record.hours_per_unit} h/unit, "
                f"{variance:+.0%} vs the {standard} h/unit standard."
            ),
            "recommendation": f"Cap billable hours at {cap} (standard + {tolerance:.0%}); the rest is non-billable.",
            "recommended_billable_hours": cap,
            "confidence": 0.95,
            "citations": [{"section_id": "4.1", "quote": "billable hours are capped at the standard effort plus 15%"}],
        }

    if variance is not None and -rules["complex_threshold"] <= variance < -tolerance:
        return {
            "exception_classification": "under_run",
            "explanation": (
                f"{record.hours} hours for {record.units} units is {record.hours_per_unit} h/unit, "
                f"{variance:+.0%} vs the {standard} h/unit standard."
            ),
            "recommendation": f"Bill the {record.hours} recorded hours.",
            "recommended_billable_hours": record.hours,
            "confidence": 0.9,
            "citations": [{"section_id": "4.3", "quote": "the Contractor bills recorded hours"}],
        }
    return None
