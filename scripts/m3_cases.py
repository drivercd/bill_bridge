"""Run the four M3 report cases end to end and record the evidence.

    python scripts/m3_cases.py            # prints a summary, writes docs/m3/test_evidence.{md,json}

Each case uses a fresh copy of the mock data and fresh SQLite files, and
checks the effect in the simulated Invoicing System (what it actually holds),
not in BillBridge's own records.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from billbridge.agent import CrashingLLM, MockLLM, WorkerCrash  # noqa: E402
from billbridge.invoicing_sim import SimulatedInvoicingSystem  # noqa: E402
from billbridge.workflow import Workflow  # noqa: E402

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.at = T0

    def __call__(self):
        return self.at


def sandbox():
    tmp = Path(tempfile.mkdtemp(prefix="bb-m3-"))
    shutil.copytree(ROOT / "data", tmp / "data")
    return tmp / "data", tmp / "out"


def workflow(data, out, llm=None, faults=None, clock=None):
    inv = SimulatedInvoicingSystem(out / "invoicing_system.db", faults=faults)
    return Workflow(data, out, llm or MockLLM(), invoicing=inv, clock=clock or Clock(), sleep=lambda s: None)


def decide(item, decision, event_id, role="supervisor", reviewer="billing supervisor", **extra):
    return {"event_id": event_id, "type": "review.decision", "review_id": item["review_id"],
            "content_version": item["content_version"], "decision": decision, "reviewer": reviewer,
            "role": role, **extra}


def receiver_view(w):
    return {
        "invoices_in_receiver": w.invoicing.count(),
        "invoices": [{"invoice_id": i["invoice_id"], "lines": len(i["lines"]), "total_hours": i["total_hours"],
                      "P-102_hours": next((l["billable_hours"] for l in i["lines"] if l["project_id"] == "P-102"), None)}
                     for i in w.invoicing.invoices()],
        "receiver_requests": [f"{r['kind']}: {r['result']}" for r in w.invoicing.requests()],
    }


def case1_approved():
    data, out = sandbox()
    w = workflow(data, out)
    state = w.run("2026-09")
    paused = state.status
    items = {i["project_id"]: i for i in w.store.reviews(run_id=state.run_id)}
    before = w.invoicing.count()
    w2 = workflow(data, out)  # decisions arrive in a new process
    results = [w2.handle_event(decide(items[p], "approve", f"cli-approve-{p}")) for p in ("P-102", "P-107", "P-114")]
    final = w2.load_state(state.run_id)
    rv = receiver_view(w2)
    ok = (paused == "awaiting_review" and before == 0 and final.status == "completed"
          and rv["invoices_in_receiver"] == 1 and rv["invoices"][0]["lines"] == 20 and rv["invoices"][0]["P-102_hours"] == 30.0)
    return {
        "case": "1. Approved run",
        "input": "Period 2026-09, 20 projects (17 clean, 2 simple, 1 complex). Supervisor approves P-102 (30.0 h, §4.2), P-107 and P-114 through review.decision events handled by a new process.",
        "expected": "Run pauses with 3 review items and 0 invoices; after the third approval one draft invoice with 20 lines exists, P-102 at 30.0 h.",
        "actual": f"Paused as {paused} with {len(items)} items and {before} invoices. After approvals: run {final.status}; "
                  f"receiver holds {rv['invoices_in_receiver']} invoice with {rv['invoices'][0]['lines']} lines, total "
                  f"{rv['invoices'][0]['total_hours']} h, P-102 = {rv['invoices'][0]['P-102_hours']} h.",
        "evidence": {"event_results": results, **rv, "P-102_approval": final.records["P-102"].approval},
        "passed": ok,
    }


def case2_reject_and_timeout():
    # 2a rejection
    data, out = sandbox()
    w = workflow(data, out)
    state = w.run("2026-09")
    items = {i["project_id"]: i for i in w.store.reviews(run_id=state.run_id)}
    w.handle_event(decide(items["P-102"], "reject", "rej-P-102", note="No make-ready justification on file"))
    w.handle_event(decide(items["P-107"], "approve", "ok-P-107"))
    w.handle_event(decide(items["P-114"], "approve", "ok-P-114"))
    a_final = w.load_state(state.run_id)
    a_rv = receiver_view(w)

    # 2b timeout
    data, out = sandbox()
    clock = Clock()
    w = workflow(data, out, clock=clock)
    s = w.run("2026-09", "P-102")
    item = w.store.reviews(run_id=s.run_id)[0]
    clock.at = T0 + timedelta(hours=49)
    tick = w.handle_event({"event_id": "timer-2026-10-03T10", "type": "timer.tick"})
    escalated = w.store.review(item["review_id"])
    late = w.handle_event(decide(item, "approve", "late-supervisor-approval"))
    b_rv = receiver_view(w)
    b_status = w.load_state(s.run_id).status

    ok = (a_final.records["P-102"].status == "rejected" and a_rv["invoices_in_receiver"] == 1
          and a_rv["invoices"][0]["P-102_hours"] is None and a_rv["invoices"][0]["lines"] == 19
          and escalated["status"] == "escalated" and escalated["reviewer_role"] == "ar_manager"
          and late["outcome"] == "rejected" and b_rv["invoices_in_receiver"] == 0 and b_status == "awaiting_review")
    return {
        "case": "2. Rejection and timeout",
        "input": "(a) Supervisor rejects P-102, approves the two simple exceptions. (b) P-102 only; no decision; timer event at T+49 h; then a late supervisor approval arrives.",
        "expected": "(a) P-102 withheld: invoice has 19 lines, P-102 status rejected and listed in the review queue. (b) Item escalated to the AR manager, no invoice, late approval refused, run still waiting.",
        "actual": f"(a) P-102 {a_final.records['P-102'].status}; invoice lines {a_rv['invoices'][0]['lines']}, P-102 on invoice: {a_rv['invoices'][0]['P-102_hours']}. "
                  f"(b) tick: {tick['detail']}; item {escalated['status']} -> {escalated['reviewer_role']}; late approval {late['outcome']}; "
                  f"receiver invoices {b_rv['invoices_in_receiver']}; run {b_status}.",
        "evidence": {"rejection": a_rv, "timeout_tick": tick, "late_event": late, "timeout_receiver": b_rv},
        "passed": ok,
    }


def case3_duplicates():
    data, out = sandbox()
    w = workflow(data, out, faults=["unavailable"])
    s = w.run("2026-09", "P-102")
    item = w.store.reviews(run_id=s.run_id)[0]
    event = decide(item, "approve", "webhook-7f3a")
    r1 = w.handle_event(event)
    r2 = w.handle_event(event)
    r3 = workflow(data, out).handle_event(event)  # redelivered to another process
    r4 = workflow(data, out).handle_event(decide(item, "approve", "webhook-8b21"))  # double click, new id
    period = {"event_id": "period-close-NLT-2026-09", "type": "run.requested", "billing_period": "2026-09", "project_id": "P-101"}
    rv = receiver_view(w)
    keys = {r["idempotency_key"] for r in w.invoicing.requests()}
    data5, out5 = sandbox()  # period-close signal on a clean system
    w5 = workflow(data5, out5)
    r5 = w5.handle_event(period)
    r6 = w5.handle_event(period)
    rv5 = receiver_view(w5)
    ok = (r1["outcome"] == "applied" and r2["outcome"] == r3["outcome"] == "duplicate" and r4["outcome"] == "ignored"
          and rv["invoices_in_receiver"] == 1 and len(keys) == 1 and rv["receiver_requests"][0] == "create: 503 unavailable"
          and r6["outcome"] == "duplicate" and len(w5.store.runs()) == 1 and rv5["invoices_in_receiver"] == 1)
    return {
        "case": "3. Duplicate event and retry",
        "input": "Approval event webhook-7f3a delivered 3 times (twice to one process, once to a new one); a second approval with a new id for the same review; the invoicing API returns 503 once; a period-close event for P-101 delivered twice.",
        "expected": "One decision applied; repeats are duplicate or ignored; the 503 is retried with the same idempotency key; exactly one invoice; the second period-close starts no run.",
        "actual": f"Outcomes: {r1['outcome']}, {r2['outcome']}, {r3['outcome']}, {r4['outcome']}; period-close {r5['outcome']}, {r6['outcome']}. "
                  f"P-102 run: receiver holds {rv['invoices_in_receiver']} invoice, requests {rv['receiver_requests']}, idempotency keys used {len(keys)}. "
                  f"Period-close: runs started {len(w5.store.runs())}, invoices {rv5['invoices_in_receiver']}.",
        "evidence": {"outcomes": [r1, r2, r3, r4, r5, r6], **rv,
                     "event_receipts": [f"{e['event_id']}: {e['outcome']}" for e in w.store.event_receipts()]},
        "passed": ok,
    }


def case4_interruption_and_uncertain():
    # 4a crash during the agent step, then resume
    data, out = sandbox()
    w = workflow(data, out, llm=CrashingLLM(MockLLM()))
    try:
        w.run("2026-09")
        crashed = False
    except WorkerCrash:
        crashed = True
    run_id = w.store.runs()[0]["run_id"]
    saved = w.store.load_run(run_id)
    loads_before = sum(1 for e in w.store.log_entries(run_id) if e["step"] == "2_load_data")
    llm = MockLLM()
    w2 = workflow(data, out, llm=llm)
    resumed = w2.resume(run_id)
    w2b = workflow(data, out)
    w2b.resume(run_id)
    loads_after = sum(1 for e in w2b.store.log_entries(run_id) if e["step"] == "2_load_data")
    queue = len(w2b.store.reviews(run_id=run_id))

    # 4b lost acknowledgement
    data, out = sandbox()
    w = workflow(data, out, faults=["lost_ack"])
    s = w.run("2026-09", "P-102")
    item = w.store.reviews(run_id=s.run_id)[0]
    res = w.handle_event(decide(item, "approve", "approve-after-504"))
    final = w.load_state(s.run_id)
    rv = receiver_view(w)

    ok = (crashed and saved["records"]["P-102"]["agent_status"] == "in_progress" and loads_before == loads_after == 1
          and llm.calls == 1 and resumed.status == "awaiting_review" and queue == 3
          and res["run_status"] == "completed" and rv["invoices_in_receiver"] == 1
          and final.invoice["bound_by"] == "lookup after uncertain outcome")
    return {
        "case": "4. Interruption and resume; uncertain outcome",
        "input": "(a) Worker dies while the AI agent processes P-102; a new worker resumes the run, then a third resume is issued. (b) Approved P-102; the invoicing API creates the invoice but the reply is lost (504).",
        "expected": "(a) Resume skips load/validate, reprocesses only P-102 (1 model call), 3 review items, no duplicates. (b) BillBridge looks up the key before resending, binds to the existing invoice, receiver holds 1 invoice.",
        "actual": f"(a) crash={crashed}; saved P-102 agent_status={saved['records']['P-102']['agent_status']}; load step ran {loads_after}x total; "
                  f"model calls on resume {llm.calls}; status {resumed.status}; review items {queue}. "
                  f"(b) run {res['run_status']}; bound by '{final.invoice['bound_by']}'; receiver invoices {rv['invoices_in_receiver']}; requests {rv['receiver_requests']}.",
        "evidence": {"resume_log": resumed.resumed_from, "lost_ack_receiver": rv, "invoice": final.invoice},
        "passed": ok,
    }


def main() -> int:
    cases = [case1_approved(), case2_reject_and_timeout(), case3_duplicates(), case4_interruption_and_uncertain()]
    out_dir = ROOT / "docs" / "m3"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "test_evidence.json").write_text(json.dumps(cases, indent=2, default=str))
    lines = ["# M3 test evidence", "", "Generated by `python scripts/m3_cases.py`. Each case runs on fresh mock data and",
             "fresh SQLite files; effects are counted in the simulated Invoicing System.", ""]
    for c in cases:
        lines += [f"## {c['case']}: {'PASS' if c['passed'] else 'FAIL'}", "", f"**Input.** {c['input']}", "",
                  f"**Expected.** {c['expected']}", "", f"**Actual.** {c['actual']}", "", "```json",
                  json.dumps(c["evidence"], indent=2, default=str)[:4000], "```", ""]
    (out_dir / "test_evidence.md").write_text("\n".join(lines))
    for c in cases:
        print(f"{'PASS' if c['passed'] else 'FAIL'}  {c['case']}\n      {c['actual']}")
    return 0 if all(c["passed"] for c in cases) else 1


if __name__ == "__main__":
    sys.exit(main())
