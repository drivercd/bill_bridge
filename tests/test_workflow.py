"""End-to-end tests for the M2 test cases plus the retry and stop paths."""

import csv
import json

from billbridge.agent import MockLLM, validate_output
from billbridge.review import SimulatedSupervisor
from billbridge.workflow import Workflow


def run(data_dir, out_dir, project_id=None, llm=None, supervisor=None, period="2026-09"):
    llm = llm or MockLLM()
    state = Workflow(data_dir, out_dir, llm, supervisor).run(period, project_id)
    return state, llm


def rewrite_units(data_dir, project_id, units_value):
    path = data_dir / "units.csv"
    rows = list(csv.DictReader(path.open()))
    for row in rows:
        if row["project_id"] == project_id:
            row["units_completed"] = units_value
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_full_period_matches_m2_measurement(data_dir, out_dir):
    state, llm = run(data_dir, out_dir)
    assert state.status == "completed"
    assert state.metrics["projects"] == 20
    assert state.metrics["auto_cleared"] == 17
    assert state.metrics["simple_exceptions"] == 2
    assert state.metrics["complex_exceptions"] == 1
    assert state.metrics["auto_clear_rate"] == 0.85
    assert llm.calls == 1
    staged = json.loads((out_dir / "invoicing_staging.json").read_text())
    assert len(staged) == 20
    for name in ("execution_log.json", "state_checkpoint.json", "review_queue.json"):
        assert (out_dir / name).exists()


def test_case1_clean_run_bypasses_ai(data_dir, out_dir):
    state, llm = run(data_dir, out_dir, project_id="P-101")
    record = state.records["P-101"]
    assert (record.hours, record.units, record.hours_per_unit) == (40.0, 5, 8.0)
    assert record.resolution_source == "auto_clear"
    assert record.status == "approved"
    assert llm.calls == 0


def test_case2_complex_exception_cites_contract(data_dir, out_dir):
    state, llm = run(data_dir, out_dir, project_id="P-102")
    record = state.records["P-102"]
    assert record.exception_type == "complex"
    assert record.resolution_source == "ai_agent"
    assert record.recommendation["recommended_billable_hours"] == 30.0
    assert record.recommendation["citations"][0]["section_id"] == "4.2"
    assert record.supervisor_decision == "approve"
    assert record.final_billable_hours == 30.0


def test_case3_invalid_input_stops_with_zero_model_calls(data_dir, out_dir):
    rewrite_units(data_dir, "P-102", "")
    state, llm = run(data_dir, out_dir, project_id="P-102")
    record = state.records["P-102"]
    assert record.status == "invalid"
    assert "missing or invalid units_completed" in record.validation_errors[0]
    assert llm.calls == 0
    assert any("P-102 skipped" in n for n in state.notifications)
    queue = json.loads((out_dir / "review_queue.json").read_text())
    assert queue[0]["project_id"] == "P-102"


def test_simple_exception_uses_rules_engine(data_dir, out_dir):
    state, llm = run(data_dir, out_dir, project_id="P-107")
    record = state.records["P-107"]
    assert record.resolution_source == "rules_engine"
    assert record.final_billable_hours == 36.8  # 4 units x 8.0 h x 1.15
    assert llm.calls == 0


def test_retry_then_success(data_dir, out_dir):
    llm = MockLLM(failures=["api_error", "no_citation"])
    state, _ = run(data_dir, out_dir, project_id="P-102", llm=llm)
    record = state.records["P-102"]
    assert record.status == "approved"
    assert record.retry_count == 2
    assert llm.calls == 3


def test_retry_exhaustion_escalates_to_human(data_dir, out_dir):
    llm = MockLLM(failures=["invalid_json", "missing_field", "no_citation"])
    state, _ = run(data_dir, out_dir, project_id="P-102", llm=llm)
    record = state.records["P-102"]
    assert record.status == "escalated"
    assert record.retry_count == 3
    staged = json.loads((out_dir / "invoicing_staging.json").read_text())
    assert staged == []
    queue = json.loads((out_dir / "review_queue.json").read_text())
    assert queue[0]["status"] == "escalated"


def test_supervisor_modify_and_reject(data_dir, out_dir):
    supervisor = SimulatedSupervisor(
        {
            "P-102": {"decision": "modify", "billable_hours": 27.5, "note": "agreed with client"},
            "P-114": {"decision": "reject", "note": "units count disputed"},
        }
    )
    state, _ = run(data_dir, out_dir, supervisor=supervisor)
    assert state.records["P-102"].final_billable_hours == 27.5
    assert state.records["P-114"].status == "rejected"
    staged_ids = {r["project_id"] for r in json.loads((out_dir / "invoicing_staging.json").read_text())}
    assert "P-114" not in staged_ids


def test_missing_source_file_stops_run(data_dir, out_dir):
    (data_dir / "units.csv").unlink()
    state, llm = run(data_dir, out_dir)
    assert state.status == "stopped"
    assert "units.csv" in state.stop_reason
    assert llm.calls == 0


def test_bad_billing_period_stops_run(data_dir, out_dir):
    state, _ = run(data_dir, out_dir, period="Sept")
    assert state.status == "stopped"


def test_duplicate_time_entry_is_invalid(data_dir, out_dir):
    path = data_dir / "time_tracking.csv"
    lines = path.read_text().splitlines()
    first_p101 = next(line for line in lines if ",P-101," in line)
    path.write_text("\n".join(lines + [first_p101]) + "\n")
    state, _ = run(data_dir, out_dir, project_id="P-101")
    assert state.records["P-101"].status == "invalid"
    assert any("duplicate time entry" in e for e in state.records["P-101"].validation_errors)


def test_citation_must_reference_retrieved_section():
    body = {
        "exception_classification": "x",
        "explanation": "x",
        "recommendation": "x",
        "recommended_billable_hours": 1.0,
        "confidence": 0.5,
        "citations": [{"section_id": "9.9", "quote": "made up"}],
    }
    try:
        validate_output(json.dumps(body), {"4.1", "4.2"})
    except ValueError as exc:
        assert "not retrieved" in str(exc)
    else:
        raise AssertionError("expected a ValueError")
