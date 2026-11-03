"""BillBridge command line (M3).

    python -m billbridge run --billing-period 2026-09      # pauses at the checkpoint
    python -m billbridge review --reviewer "J. Baker"        # interactive checkpoint
    python -m billbridge status
    python -m billbridge event examples/events/approve_p102.json
    python -m billbridge inbox examples/inbox
    python -m billbridge tick --now 2026-10-05T09:00:00+00:00
    python -m billbridge resume RUN_ID
    python -m billbridge retry-commit RUN_ID --reviewer "AR manager"
    python -m billbridge invoices                              # what the receiver actually holds

All state lives in <out-dir>/billbridge.db; the simulated Invoicing System
keeps its own records in <out-dir>/invoicing_system.db.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from . import config
from .agent import AnthropicLLM, CrashingLLM, MockLLM, WorkerCrash
from .events import process_inbox
from .invoicing_sim import SimulatedInvoicingSystem
from .review import SimulatedSupervisor, render_review
from .workflow import Workflow


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data-dir", default="data", type=Path)
    p.add_argument("--out-dir", default="output", type=Path)
    p.add_argument("--llm", choices=["mock", "live"], default=None,
                   help="mock (no API key) or live (Anthropic API); default from BILLBRIDGE_LLM_MODE")
    p.add_argument("--invoicing-fault", action="append", default=[], choices=["unavailable", "lost_ack"],
                   help="inject a fault into the next simulated Invoicing System call (repeatable)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m billbridge", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="start a reconciliation run for a billing period")
    _common(p)
    p.add_argument("--billing-period", required=True, help="YYYY-MM, e.g. 2026-09")
    p.add_argument("--project-id", help="run a single project")
    p.add_argument("--run-id", help="fixed run id (makes the example event files match), e.g. run-demo0001")
    p.add_argument("--decisions", type=Path, help="JSON of scripted supervisor decisions (M2 behavior)")
    p.add_argument("--simulate-supervisor", action="store_true",
                   help="approve every exception with a simulated supervisor (M2 behavior)")
    p.add_argument("--simulate-crash", choices=["agent"], help="kill the worker during the AI agent step")

    p = sub.add_parser("review", help="human checkpoint: show pending reviews and record decisions")
    _common(p)
    p.add_argument("--reviewer", required=True, help="name of the person deciding")
    p.add_argument("--role", choices=["supervisor", "ar_manager"], default="supervisor")
    p.add_argument("--review-id", help="decide one item without prompting")
    p.add_argument("--decision", choices=["approve", "modify", "reject"])
    p.add_argument("--hours", type=float, help="billable hours for 'modify'")
    p.add_argument("--note")
    p.add_argument("--show", action="store_true", help="print what the reviewer sees without deciding")
    p.add_argument("--event-id", help="reuse a specific event id (to demonstrate duplicate handling)")
    p.add_argument("--content-version", help="the version you reviewed (defaults to the current one)")

    p = sub.add_parser("event", help="handle event JSON files (simulated webhook)")
    _common(p)
    p.add_argument("files", nargs="+", type=Path)

    p = sub.add_parser("inbox", help="handle every *.json event in a folder")
    _common(p)
    p.add_argument("folder", type=Path)

    p = sub.add_parser("tick", help="timer event: expire overdue reviews and escalate to the AR manager")
    _common(p)
    p.add_argument("--now", help="ISO time to evaluate expiries at (simulated clock)")
    p.add_argument("--event-id")

    p = sub.add_parser("resume", help="continue a run from its saved checkpoint")
    _common(p)
    p.add_argument("run_id")

    p = sub.add_parser("retry-commit", help="AR manager releases a held invoice load")
    _common(p)
    p.add_argument("run_id")
    p.add_argument("--reviewer", required=True)

    p = sub.add_parser("status", help="runs, review queue and commits")
    _common(p)

    p = sub.add_parser("invoices", help="list what the simulated Invoicing System actually holds")
    _common(p)
    return parser


def make_workflow(args, supervisor=None, crash=False) -> Workflow:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    mode = args.llm or config.llm_mode()
    llm = AnthropicLLM() if mode == "live" else MockLLM()
    if crash:
        llm = CrashingLLM(llm)
    invoicing = SimulatedInvoicingSystem(args.out_dir / "invoicing_system.db", faults=args.invoicing_fault)
    return Workflow(args.data_dir, args.out_dir, llm, supervisor, invoicing=invoicing)


def print_state(state) -> None:
    print(f"Run {state.run_id}: {state.status} (phase {state.phase})")
    if state.stop_reason:
        print(f"  reason: {state.stop_reason}")
    for key, value in state.metrics.items():
        print(f"  {key}: {value}")
    if state.invoice:
        print(f"  invoice: {state.invoice}")
    for note in state.notifications[-8:]:
        print(f"  notice: {note}")


def print_result(result: dict) -> None:
    print(json.dumps({k: v for k, v in result.items() if v is not None}))


def cmd_review(args, wf: Workflow) -> int:
    role_items = wf.store.reviews(status="pending") + wf.store.reviews(status="escalated")
    allowed = [i for i in role_items if args.role == "ar_manager" or i["reviewer_role"] == "supervisor"]
    if args.review_id:
        allowed = [i for i in allowed if i["review_id"] == args.review_id] or [wf.store.review(args.review_id) or {}]
    if not allowed or not allowed[0]:
        print("Nothing waiting for review.")
        return 0
    for item in allowed:
        if item.get("proposal_json"):
            print(render_review(item))
        if args.show:
            continue
        decision, hours, note = args.decision, args.hours, args.note
        if decision is None:
            answer = input("[a]pprove  [m]odify  [r]eject  [s]kip > ").strip().lower()[:1]
            decision = {"a": "approve", "m": "modify", "r": "reject"}.get(answer)
            if decision is None:
                print("skipped; item stays in the queue")
                continue
            if decision == "modify":
                hours = float(input("billable hours > "))
            note = input("note (optional) > ").strip() or None
        event = {
            "event_id": args.event_id or f"cli-{uuid.uuid4().hex[:12]}",
            "type": "review.decision",
            "review_id": item.get("review_id", args.review_id),
            "content_version": args.content_version or item.get("content_version"),
            "decision": decision,
            "billable_hours": hours,
            "note": note,
            "reviewer": args.reviewer,
            "role": args.role,
        }
        print_result(wf.handle_event(event))
        if args.review_id:
            break
    return 0


def cmd_status(wf: Workflow) -> int:
    for run in wf.store.runs():
        print(f"{run['run_id']}  {run['billing_period']}  {run['status']:<16} step={run['current_step']}  updated={run['updated_at']}")
    print("\nReview queue")
    for item in wf.store.reviews():
        p = json.loads(item["proposal_json"])
        print(f"  {item['review_id']}  {item['project_id']}  {item['status']:<9} role={item['reviewer_role']:<10} "
              f"v{item['content_version']}  proposed={p['proposed_billable_hours']}  authorized={item['authorized_hours']}  "
              f"by={item['decided_by']}  expires={item['expires_at']}")
    print("\nInvoice commits")
    for run in wf.store.runs():
        for c in wf.store.commits_for_period(run["client_id"] or "", run["billing_period"] or ""):
            if c["run_id"] == run["run_id"]:
                print(f"  {c['idempotency_key']}  {c['status']}  attempts={c['attempts']}  invoice={c['external_invoice_id']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command

    if cmd == "run":
        supervisor = None
        if args.decisions:
            supervisor = SimulatedSupervisor.from_file(args.decisions)
        elif args.simulate_supervisor:
            supervisor = SimulatedSupervisor()
        wf = make_workflow(args, supervisor, crash=args.simulate_crash == "agent")
        try:
            state = wf.run(args.billing_period, args.project_id, args.run_id)
        except WorkerCrash as exc:
            run = wf.store.runs()[-1]
            print(f"WORKER CRASHED: {exc}")
            print(f"Run {run['run_id']} saved at step {run['current_step']}. Resume with:")
            print(f"  python -m billbridge resume {run['run_id']}")
            return 3
        print_state(state)
        if state.status == "awaiting_review":
            print("Next: python -m billbridge review --reviewer <name>")
        return 0 if state.status in ("completed", "awaiting_review") else 1

    wf = make_workflow(args)
    if cmd == "review":
        return cmd_review(args, wf)
    if cmd == "event":
        for path in args.files:
            print_result({"file": path.name, **wf.handle_event(json.loads(path.read_text()))})
        return 0
    if cmd == "inbox":
        for result in process_inbox(wf, args.folder):
            print_result(result)
        return 0
    if cmd == "tick":
        event = {"event_id": args.event_id or f"tick-{args.now or wf.now_iso()}", "type": "timer.tick", "now": args.now}
        print_result(wf.handle_event(event))
        return 0
    if cmd == "resume":
        print_state(wf.resume(args.run_id))
        return 0
    if cmd == "retry-commit":
        event = {"event_id": f"retry-{uuid.uuid4().hex[:12]}", "type": "commit.retry", "run_id": args.run_id,
                 "reviewer": args.reviewer, "role": "ar_manager"}
        print_result(wf.handle_event(event))
        return 0
    if cmd == "status":
        return cmd_status(wf)
    if cmd == "invoices":
        invoices = wf.invoicing.invoices()
        print(f"{len(invoices)} invoice(s) in the simulated Invoicing System")
        for inv in invoices:
            print(f"  {inv['invoice_id']}  {inv['client_id']} {inv['billing_period']}  {inv['status']}  "
                  f"lines={len(inv['lines'])}  total={inv['total_hours']} h  key={inv['idempotency_key']}")
        print("Requests received:")
        for r in wf.invoicing.requests():
            print(f"  #{r['id']} {r['kind']:<6} {r['result']:<28} key={r['idempotency_key']}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
