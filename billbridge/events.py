"""Event-driven path (M3).

Events arrive from the command-line review prompt, from JSON files dropped in
an inbox folder (a simulated webhook), or from a timer. Each one carries an
`event_id`. The handler:

1. records the event id in SQLite before acting (duplicates are detected
   there and have no effect),
2. checks the event against the current saved state (unknown, stale, late,
   unauthorized and repeated decisions are refused and logged),
3. applies the change with a compare-and-set on the review status, and
4. asks the workflow to advance the run from its saved state.

Event types
-----------
review.decision  {review_id, content_version, decision, reviewer, role, billable_hours?, note?}
timer.tick       {now?}  expires overdue reviews and escalates them to the AR manager
commit.retry     {run_id, reviewer, role="ar_manager"}  release a held invoice commit
run.requested    {billing_period, project_id?}  period-close signal that starts a run once
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .review import DECISIONS

KNOWN_TYPES = {"review.decision", "timer.tick", "commit.retry", "run.requested"}
ROLES = {"supervisor", "ar_manager"}


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class EventHandler:
    def __init__(self, workflow):
        self.wf = workflow
        self.store = workflow.store

    # -- entry point --------------------------------------------------------

    def handle(self, event: dict, advance: bool = True) -> dict:
        now = self.wf.now_iso()
        event_id = event.get("event_id")
        if not event_id:
            return {"event_id": None, "outcome": "rejected", "detail": "event has no event_id; cannot de-duplicate"}

        first_time = self.store.record_event(event, "received", "processing", now)
        if not first_time:
            prior = self.store.event(event_id)
            if prior["outcome"] != "received":
                self.wf.log_global("event", "code", "duplicate", event_id, f"already {prior['outcome']}; no effect")
                return {"event_id": event_id, "outcome": "duplicate", "detail": f"already processed ({prior['outcome']})"}
            # Seen but never finished (the process died mid-event): finish it.
            # The steps below are idempotent, so re-running them is safe.

        etype = event.get("type")
        if etype not in KNOWN_TYPES:
            result = ("rejected", f"unexpected event type {etype!r}", None)
        elif etype == "review.decision":
            result = self._decision(event)
        elif etype == "timer.tick":
            result = self._tick(event)
        elif etype == "commit.retry":
            result = self._commit_retry(event)
        else:
            result = self._run_requested(event)
        outcome, detail, run_id = result
        self.store.set_event_outcome(event_id, outcome, detail)
        self.wf.log_global("event", "code", outcome, event_id, detail, run_id=run_id)

        state = None
        if advance and run_id and outcome == "applied" and etype in ("review.decision", "commit.retry"):
            state = self.wf.resume(run_id)
        return {"event_id": event_id, "outcome": outcome, "detail": detail, "run_id": run_id,
                "run_status": state.status if state else None}

    # -- review.decision ----------------------------------------------------

    def _decision(self, e: dict):
        item = self.store.review(e.get("review_id", ""))
        if item is None:
            return "rejected", f"unknown review_id {e.get('review_id')!r}", None
        run_id = item["run_id"]
        if item["decision_event_id"] == e["event_id"]:
            return "applied", "decision already applied by this event (resumed after interruption)", run_id

        now = self.wf.now()
        if item["status"] == "pending" and item["expires_at"] and now >= parse_time(item["expires_at"]):
            self._expire(item)
            return "rejected", (
                f"late: review window closed at {item['expires_at']}; item escalated to the AR manager, "
                "nothing staged"
            ), run_id
        if item["status"] not in ("pending", "escalated"):
            return "ignored", (
                f"review already {item['status']} by {item['decided_by']} at {item['decided_at']}; "
                "repeated or conflicting decision has no effect"
            ), run_id

        decision, role = e.get("decision"), e.get("role")
        if decision not in DECISIONS:
            return "rejected", f"invalid decision {decision!r}", run_id
        if role not in ROLES:
            return "rejected", f"unknown reviewer role {role!r}", run_id
        if item["status"] == "escalated" and role != "ar_manager":
            return "rejected", "escalated items can only be decided by the AR manager", run_id
        if e.get("content_version") != item["content_version"]:
            return "rejected", (
                f"stale: decision was made on version {e.get('content_version')!r}, "
                f"current version is {item['content_version']!r}"
            ), run_id

        proposal = json.loads(item["proposal_json"])
        recorded = proposal["evidence"]["recorded_hours"]
        if decision == "approve":
            hours = proposal["proposed_billable_hours"]
            if hours is None:
                return "rejected", "nothing to approve: no proposal exists; use modify with billable_hours or reject", run_id
        elif decision == "modify":
            hours = e.get("billable_hours")
            if not isinstance(hours, (int, float)) or hours < 0 or (recorded is not None and hours > recorded):
                return "rejected", f"modify needs billable_hours between 0 and recorded {recorded}", run_id
            hours = float(hours)
        else:
            hours = None

        status = {"approve": "approved", "modify": "modified", "reject": "rejected"}[decision]
        approval_expires = (
            (now + timedelta(hours=self.wf.approval_validity_hours)).isoformat(timespec="seconds")
            if decision != "reject" else None
        )
        won = self.store.update_review(
            item["review_id"], item["status"],
            status=status, decision=decision, decided_by=e.get("reviewer", "unknown"), decided_role=role,
            decided_at=self.wf.now_iso(), decision_event_id=e["event_id"], authorized_hours=hours,
            approval_expires_at=approval_expires, note=e.get("note"),
        )
        if not won:  # another decision landed between our read and write
            return "ignored", "review was decided concurrently; this decision has no effect", run_id
        return "applied", f"{decision} {item['project_id']}" + (f" at {hours} h" if hours is not None else ""), run_id

    # -- timer.tick ---------------------------------------------------------

    def _expire(self, item: dict) -> bool:
        won = self.store.update_review(
            item["review_id"], "pending", status="escalated", reviewer_role="ar_manager", expires_at=None,
            note=f"no supervisor decision by {item['expires_at']}; escalated to AR manager",
        )
        if won:
            self.wf.on_review_escalated(item)
        return won

    def _tick(self, e: dict):
        if e.get("now"):
            self.wf.set_clock(parse_time(e["now"]))
        now = self.wf.now()
        expired = []
        for item in self.store.reviews(status="pending"):
            if item["expires_at"] and now >= parse_time(item["expires_at"]) and self._expire(item):
                expired.append(item["project_id"])
        detail = f"expired and escalated: {expired}" if expired else "no reviews overdue"
        return "applied", detail, None

    # -- commit.retry -------------------------------------------------------

    def _commit_retry(self, e: dict):
        run_id = e.get("run_id")
        if e.get("role") != "ar_manager":
            return "rejected", "only the AR manager can release a held invoice commit", run_id
        state = self.store.load_run(run_id) if run_id else None
        if state is None:
            return "rejected", f"unknown run {run_id!r}", run_id
        if state["status"] != "commit_held":
            return "ignored", f"run is {state['status']}, not commit_held; nothing to retry", run_id
        self.wf.release_commit(run_id, e.get("reviewer", "unknown"))
        return "applied", "held commit released for retry with the same idempotency key", run_id

    # -- run.requested ------------------------------------------------------

    def _run_requested(self, e: dict):
        state = self.wf.run(e.get("billing_period"), e.get("project_id"))
        return "applied", f"started {state.run_id}: {state.status}", state.run_id


def process_inbox(workflow, inbox: Path) -> list[dict]:
    """Read every *.json event in `inbox` (oldest name first) and handle it.

    Processed files are kept; re-running the inbox is safe because event ids
    are de-duplicated in SQLite.
    """
    results = []
    for path in sorted(Path(inbox).glob("*.json")):
        try:
            event = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            results.append({"file": path.name, "outcome": "rejected", "detail": f"not JSON: {exc.msg}"})
            continue
        results.append({"file": path.name, **workflow.handle_event(event)})
    return results

