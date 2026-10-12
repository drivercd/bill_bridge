"""Step 4: classify each validated project as none / simple / complex exception."""

from __future__ import annotations

from .state import ProjectRecord


def classify(record: ProjectRecord, rules: dict) -> tuple[str, str]:
    """Return (exception_type, details). Unknown cases are treated as complex."""
    variance = record.variance
    if variance is None:
        return "complex", "variance could not be computed"

    tolerance = rules["tolerance"]
    complex_threshold = rules["complex_threshold"]
    pct = f"{variance:+.0%}"
    if abs(variance) <= tolerance:
        return "none", f"hours per unit {pct} vs standard, within tolerance"
    if tolerance < variance <= complex_threshold:
        return "simple", f"overrun {pct} vs standard, inside the capped band"
    if -complex_threshold <= variance < -tolerance:
        return "simple", f"under-run {pct} vs standard"
    if variance > complex_threshold:
        return "complex", f"overrun {pct} vs standard, beyond the capped band"
    return "complex", f"under-run {pct} vs standard, beyond the audit threshold"
