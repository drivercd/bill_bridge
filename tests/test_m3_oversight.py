"""M3: human checkpoint, event handling, durable state and recovery.

Every external effect is verified in the simulated Invoicing System (its own
SQLite file), not in BillBridge's own records.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from billbridge.agent import CrashingLLM, MockLLM, WorkerCrash
from billbridge.invoicing_sim import SimulatedInvoicingSystem
from billbridge.workflow import Workflow

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        return self.at


def wf(data_dir, out_dir, llm=None, faults=None, clock=None):
    """A fresh Workflow object = a fresh process: new SQLite connections, no memory."""
    invoicing = SimulatedInvoicingSystem(out_dir / "invoicing_system.db", faults=faults)
    return Workflow(data_dir, out_dir, llm or MockLLM(), invoicing=invoicing, clock=clock or Clock(), sleep=lambda s: None)


def start(data_dir, out_dir, project_id="P-102", **kw):
    w = wf(data_dir, out_dir, **kw)
    return w, w.run("2026-09", project_id)


def decision(item, decision="approve", event_id=None, role="supervisor", **extra):
    return {
        "event_id": event_id or f"evt-{item['project_id']}-{decision}",
        "type": "review.decision",
        "review_id": item["review_id"],
        "content_version": item["content_version"],
        "decision": decision,
        "reviewer": "test-reviewer",
        "role": role,
        **extra,
    }


def only_item(w, run_id):
    items = w.store.reviews(run_id=run_id)
    assert len(items) == 1
    return items[0]


# -- Case 1: approved run -----------------------------------------------------------


def test_case1_approved_run_loads_invoice_once(data_dir, out_dir):
    w, state = start(data_dir, out_dir)
    assert state.status == "awaiting_review"
    assert w.invoicing.count() == 0  # nothing leaves BillBridge before approval

    item = only_item(w, state.run_id)
    proposal = json.loads(item["proposal_json"])
    assert proposal["proposed_billable_hours"] == 30.0
    assert proposal["applicable_rule"][0]["section_id"] == "4.2"
    assert any("27.6 h" in c for c in proposal["conflicts"])  # §4.1 vs §4.2 conflict is shown

    # A new process receives the decision: the pending review survived in SQLite.
    w2 = wf(data_dir, out_dir)
    result = w2.handle_event(decision(item))
    assert result["outcome"] == "applied"
    assert result["run_status"] == "completed"

    invoices = w2.invoicing.invoices()
    assert len(invoices) == 1
    assert invoices[0]["lines"] == [
        {"project_id": "P-102", "unit_type": "pole_attachment", "units": 3, "billable_hours": 30.0,
         "resolution_source": "ai_agent", "authorization": "evt-P-102-approve"}
    ]
    approval = w2.load_state(state.run_id).records["P-102"].approval
    assert approval["content_version"] == item["content_version"]
    assert approval["expires_at"] == (T0 + timedelta(hours=72)).isoformat(timespec="seconds")


def test_modify_binds_reviewer_hours(data_dir, out_dir):
    w, state = start(data_dir, out_dir)
    item = only_item(w, state.run_id)
    w.handle_event(decision(item, "modify", billable_hours=27.6, note="no make-ready justification"))
    assert w.invoicing.invoices()[0]["lines"][0]["billable_hours"] == 27.6


# -- Case 2: rejection and timeout ------------------------------------------------------


def test_case2a_rejection_withholds_line(data_dir, out_dir):
    w, state = start(data_dir, out_dir, project_id=None)
    for item in w.store.reviews(run_id=state.run_id):
        d = "reject" if item["project_id"] == "P-102" else "approve"
        w.handle_event(decision(item, d, note="needs make-ready evidence" if d == "reject" else None))
    final = w.load_state(state.run_id)
    assert final.status == "completed"
    assert final.records["P-102"].status == "rejected"
    invoice_ids = [line["project_id"] for line in w.invoicing.invoices()[0]["lines"]]
    assert "P-102" not in invoice_ids and len(invoice_ids) == 19
    queue = json.loads((out_dir / "review_queue.json").read_text())
    assert queue == [{"project_id": "P-102", "status": "rejected", "review_id": final.records["P-102"].review_id,
                      "reason": "needs make-ready evidence", "recommendation": final.records["P-102"].recommendation}]


def test_case2b_timeout_escalates_and_late_approval_is_refused(data_dir, out_dir):
    clock = Clock()
    w, state = start(data_dir, out_dir, clock=clock)
    item = only_item(w, state.run_id)

    clock.at = T0 + timedelta(hours=49)
    tick = w.handle_event({"event_id": "tick-1", "type": "timer.tick"})
    assert "P-102" in tick["detail"]
    escalated = w.store.review(item["review_id"])
    assert escalated["status"] == "escalated" and escalated["reviewer_role"] == "ar_manager"
    assert w.load_state(state.run_id).status == "awaiting_review"
    assert w.invoicing.count() == 0

    late = w.handle_event(decision(item, event_id="late-supervisor"))
    assert late["outcome"] == "rejected"
    assert "AR manager" in late["detail"]
    assert w.invoicing.count() == 0

    ar = w.handle_event(decision(item, "modify", event_id="ar-1", role="ar_manager", billable_hours=27.6))
    assert ar["outcome"] == "applied" and ar["run_status"] == "completed"
    assert w.invoicing.count() == 1


def test_late_decision_without_tick_is_refused_and_escalates(data_dir, out_dir):
    clock = Clock()
    w, state = start(data_dir, out_dir, clock=clock)
    item = only_item(w, state.run_id)
    clock.at = T0 + timedelta(hours=60)
    result = w.handle_event(decision(item))
    assert result["outcome"] == "rejected" and result["detail"].startswith("late")
    assert w.store.review(item["review_id"])["status"] == "escalated"
    assert w.invoicing.count() == 0


# -- Case 3: duplicates and retries --------------------------------------------------------


def test_case3_duplicate_events_have_one_effect(data_dir, out_dir):
    w, state = start(data_dir, out_dir)
    item = only_item(w, state.run_id)
    event = decision(item, event_id="webhook-123")
    assert w.handle_event(event)["outcome"] == "applied"
    assert w.handle_event(event)["outcome"] == "duplicate"           # same event id redelivered
    w3 = wf(data_dir, out_dir)                                        # redelivered to a new process
    assert w3.handle_event(event)["outcome"] == "duplicate"
    again = w3.handle_event(decision(item, "reject", event_id="webhook-999"))  # different id, same review
    assert again["outcome"] == "ignored"
    assert w3.invoicing.count() == 1
    assert w3.store.review(item["review_id"])["decision"] == "approve"
    receipts = [r["outcome"] for r in w3.store.event_receipts() if r["event_id"] in ("webhook-123", "webhook-999")]
    assert receipts == ["applied", "duplicate", "duplicate", "ignored"]


def test_duplicate_period_close_starts_one_run(data_dir, out_dir):
    w = wf(data_dir, out_dir)
    event = {"event_id": "period-close-NLT-2026-09", "type": "run.requested", "billing_period": "2026-09", "project_id": "P-101"}
    first = w.handle_event(event)
    second = w.handle_event(event)
    assert first["outcome"] == "applied" and second["outcome"] == "duplicate"
    assert len(w.store.runs()) == 1
    assert w.invoicing.count() == 1


def test_second_run_for_same_period_is_held(data_dir, out_dir):
    w = wf(data_dir, out_dir)
    assert w.run("2026-09", "P-101").status == "completed"
    second = w.run("2026-09", "P-101")
    assert second.status == "commit_held"
    assert "already has invoice" in second.stop_reason
    assert w.invoicing.count() == 1


def test_resume_of_completed_run_does_not_reload(data_dir, out_dir):
    w, state = start(data_dir, out_dir, project_id="P-101")
    assert state.status == "completed"
    wf(data_dir, out_dir).resume(state.run_id)
    assert wf(data_dir, out_dir).invoicing.count() == 1


# -- Case 4: interruption, resume and uncertain outcomes ---------------------------------------


def test_case4a_worker_crash_resumes_without_duplicates(data_dir, out_dir):
    w = wf(data_dir, out_dir, llm=CrashingLLM(MockLLM()))
    with pytest.raises(WorkerCrash):
        w.run("2026-09")
    run_id = w.store.runs()[0]["run_id"]
    saved = w.store.load_run(run_id)
    assert saved["records"]["P-102"]["agent_status"] == "in_progress"
    loads_before = [e for e in w.store.log_entries(run_id) if e["step"] == "2_load_data"]

    llm = MockLLM()
    w2 = wf(data_dir, out_dir, llm=llm)                     # new worker
    state = w2.resume(run_id)
    entries = w2.store.log_entries(run_id)
    assert [e for e in entries if e["step"] == "2_load_data"] == loads_before  # extraction not re-run
    assert any(e["status"] == "discarded" for e in entries)
    assert llm.calls == 1
    assert state.status == "awaiting_review"
    assert len(w2.store.reviews(run_id=run_id)) == 3        # P-102, P-107, P-114: one item each

    w3 = wf(data_dir, out_dir)
    w3.resume(run_id)                                       # resuming again changes nothing
    assert len(w3.store.reviews(run_id=run_id)) == 3


def test_case4b_lost_ack_binds_to_existing_invoice(data_dir, out_dir):
    w, state = start(data_dir, out_dir, faults=["lost_ack"])
    item = only_item(w, state.run_id)
    result = w.handle_event(decision(item))
    assert result["run_status"] == "completed"
    assert w.invoicing.count() == 1
    final = w.load_state(state.run_id)
    assert final.invoice["bound_by"] == "lookup after uncertain outcome"
    kinds = [(r["kind"], r["result"]) for r in w.invoicing.requests()]
    assert kinds == [("create", "201 created, ack lost (504)"), ("lookup", "found")]


def test_case4c_crash_after_intent_saved_checks_before_sending(data_dir, out_dir):
    w, state = start(data_dir, out_dir, faults=["lost_ack", "unavailable", "unavailable"])
    item = only_item(w, state.run_id)
    # lost ack, then the lookup and the create both fail: held after 3 attempts
    result = w.handle_event(decision(item))
    assert result["run_status"] == "commit_held"
    assert w.invoicing.count() == 1                           # it was in fact created
    refused = w.handle_event({"event_id": "r0", "type": "commit.retry", "run_id": state.run_id, "role": "supervisor"})
    assert refused["outcome"] == "rejected"
    released = w.handle_event({"event_id": "r1", "type": "commit.retry", "run_id": state.run_id,
                               "role": "ar_manager", "reviewer": "AR manager"})
    assert released["run_status"] == "completed"
    assert w.invoicing.count() == 1                           # bound, not duplicated
    assert w.store.commit_record(f"{state.run_id}:2026-09:batch-1")["status"] == "committed"


def test_unavailable_then_success_uses_same_key(data_dir, out_dir):
    w, state = start(data_dir, out_dir, faults=["unavailable"])
    w.handle_event(decision(only_item(w, state.run_id)))
    reqs = w.invoicing.requests()
    assert {r["idempotency_key"] for r in reqs} == {f"{state.run_id}:2026-09:batch-1"}
    assert w.invoicing.count() == 1


def test_event_interrupted_mid_processing_is_finished_on_redelivery(data_dir, out_dir):
    w, state = start(data_dir, out_dir)
    item = only_item(w, state.run_id)
    event = decision(item, event_id="half-done")
    # Simulate a crash after the event id was recorded but before it was applied.
    w.store.record_event(event, "received", "processing", w.now_iso())
    result = wf(data_dir, out_dir).handle_event(event)
    assert result["outcome"] == "applied"
    assert wf(data_dir, out_dir).invoicing.count() == 1


# -- unexpected and unsafe events ----------------------------------------------------------------


def test_unexpected_events_are_refused(data_dir, out_dir):
    w, state = start(data_dir, out_dir)
    item = only_item(w, state.run_id)
    assert w.handle_event({"type": "review.decision"})["outcome"] == "rejected"                   # no id
    assert w.handle_event({"event_id": "x1", "type": "invoice.send"})["outcome"] == "rejected"    # unknown type
    stale = decision(item, event_id="x2")
    stale["content_version"] = "000000000000"
    assert w.handle_event(stale)["detail"].startswith("stale")
    assert w.handle_event(decision(item, "modify", event_id="x3", billable_hours=500))["outcome"] == "rejected"
    assert w.handle_event(decision(item, event_id="x4", role="intern"))["outcome"] == "rejected"
    assert w.handle_event({**decision(item, event_id="x5"), "review_id": "RV-nope"})["outcome"] == "rejected"
    assert w.store.review(item["review_id"])["status"] == "pending"
    assert w.invoicing.count() == 0


def test_escalated_agent_failure_needs_hours_from_reviewer(data_dir, out_dir):
    w, state = start(data_dir, out_dir, llm=MockLLM(["invalid_json", "missing_field", "no_citation"]))
    item = only_item(w, state.run_id)
    assert json.loads(item["proposal_json"])["proposed_billable_hours"] is None
    assert w.handle_event(decision(item, event_id="e1"))["outcome"] == "rejected"
    assert w.handle_event(decision(item, "modify", event_id="e2", billable_hours=27.6))["run_status"] == "completed"


def test_approval_expires_if_not_executed(data_dir, out_dir):
    clock = Clock()
    w, state = start(data_dir, out_dir, project_id=None, clock=clock)
    items = {i["project_id"]: i for i in w.store.reviews(run_id=state.run_id)}
    w.handle_event(decision(items["P-102"]))
    w.handle_event(decision(items["P-107"]))
    clock.at = T0 + timedelta(hours=80)          # P-102 / P-107 approvals are now 80 h old
    late_item = w.store.review(items["P-114"]["review_id"])
    assert late_item["status"] == "pending"
    # P-114's own window has also passed, so the AR manager decides it.
    w.handle_event({"event_id": "t", "type": "timer.tick"})
    result = w.handle_event(decision(items["P-114"], event_id="ar", role="ar_manager"))
    assert result["run_status"] == "awaiting_review"
    reopened = [i["project_id"] for i in w.store.reviews(run_id=state.run_id, status="pending")]
    assert sorted(reopened) == ["P-102", "P-107"]
    assert w.invoicing.count() == 0


def test_validator_rejects_unsafe_hours():
    from billbridge.agent import validate_output

    body = {
        "exception_classification": "x", "explanation": "x", "recommendation": "x",
        "recommended_billable_hours": 120.0, "confidence": 0.99,
        "citations": [{"section_id": "3.3", "quote": "invented words"}],
    }
    with pytest.raises(ValueError, match="exceeds the allowed maximum"):
        validate_output(json.dumps(body), {"3.3"}, max_hours=30.0)
    body["recommended_billable_hours"] = 30.0
    with pytest.raises(ValueError, match="verbatim"):
        validate_output(json.dumps(body), {"3.3"}, max_hours=30.0, section_text={"3.3": "Hours are billable only when"})
