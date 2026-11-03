"""Fixed-sequence orchestration (M2 report, section 2), made durable in M3.

Control flow is deterministic code. The model is called in one place only
(complex exceptions). In M3 the run state lives in SQLite and is saved after
every step and after every project, so a run can:

* pause at the human checkpoint and be resumed by a later process when a
  decision event arrives,
* restart after a crash from the last saved checkpoint without redoing
  finished steps or duplicating review items, and
* load the draft invoice into the (simulated) Invoicing System exactly once,
  using an idempotency key that is saved before the call is made.

Phases: start -> exceptions -> review -> finalize -> commit -> done
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, detection, finalize, loader, rules_engine, validation
from .agent import ExceptionAgent, WorkerCrash  # noqa: F401  (re-exported)
from .audit import ExecutionLog
from .contracts import ContractLibrary
from .events import EventHandler, parse_time
from .invoicing_sim import InvoicingConflict, InvoicingTimeout, InvoicingUnavailable, SimulatedInvoicingSystem
from .review import build_proposal
from .state import RunState
from .store import Store, canonical_hash

PERIOD_FORMAT = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
OPEN_REVIEW = ("pending", "escalated")


class Workflow:
    def __init__(
        self,
        data_dir: Path,
        out_dir: Path,
        llm,
        supervisor=None,
        db_path: Path | None = None,
        invoicing: SimulatedInvoicingSystem | None = None,
        clock=None,
        sleep=time.sleep,
        review_window_hours: float = config.REVIEW_WINDOW_HOURS,
        approval_validity_hours: float = config.APPROVAL_VALIDITY_HOURS,
    ):
        self.data_dir = Path(data_dir)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.llm = llm
        # None means a person decides at the checkpoint (the M3 default).
        # A SimulatedSupervisor turns its scripted decisions into decision events.
        self.supervisor = supervisor
        self.store = Store(db_path or self.out_dir / "billbridge.db")
        self.invoicing = invoicing or SimulatedInvoicingSystem(self.out_dir / "invoicing_system.db")
        self._fixed_now = None
        self._clock = clock
        self.sleep = sleep
        self.review_window_hours = review_window_hours
        self.approval_validity_hours = approval_validity_hours
        self.events = EventHandler(self)

    # -- clock ----------------------------------------------------------------

    def now(self) -> datetime:
        if self._fixed_now is not None:
            return self._fixed_now
        return self._clock() if self._clock else datetime.now(timezone.utc)

    def now_iso(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def set_clock(self, when: datetime) -> None:
        self._fixed_now = when

    # -- helpers --------------------------------------------------------------

    def _checkpoint(self, state: RunState, log: ExecutionLog, step: str) -> None:
        state.current_step = step
        state.save_checkpoint(self.out_dir / "state_checkpoint.json")
        self.store.save_run(state.to_dict(), self.now_iso())
        log.save(self.out_dir / "execution_log.json")

    def _stop(self, state: RunState, log: ExecutionLog, step: str, reason: str) -> RunState:
        state.status = "stopped"
        state.stop_reason = reason
        state.notify_supervisor(f"Run stopped at {step}: {reason}")
        log.log(step, "code", "failed", stop_reason=reason)
        self._checkpoint(state, log, step)
        return state

    def log_global(self, step, actor, status, event_id, detail, run_id=None) -> None:
        self.store.append_log({
            "run_id": run_id or "-", "step": step, "timestamp": self.now_iso(), "actor": actor,
            "status": status, "event_id": event_id, "output_summary": detail,
        })

    def load_state(self, run_id: str) -> RunState:
        data = self.store.load_run(run_id)
        if data is None:
            raise KeyError(f"unknown run {run_id}")
        return RunState.from_dict(data)

    def handle_event(self, event: dict) -> dict:
        return self.events.handle(event)

    # -- public entry points ----------------------------------------------------

    def run(self, billing_period: str | None, project_id: str | None = None, run_id: str | None = None) -> RunState:
        state = RunState(billing_period=billing_period, project_filter=project_id, data_dir=str(self.data_dir))
        if run_id:
            if self.store.load_run(run_id) is not None:
                raise ValueError(f"run {run_id} already exists; use resume")
            state.run_id = run_id
        log = ExecutionLog(state.run_id, self.store)
        return self._advance(state, log)

    def resume(self, run_id: str) -> RunState:
        """Continue a run from its saved checkpoint (new process, crash, or decision event)."""
        state = self.load_state(run_id)
        log = ExecutionLog(run_id, self.store)
        if state.status in ("completed", "stopped"):
            return state
        state.resumed_from.append({"at": self.now_iso(), "phase": state.phase, "step": state.current_step})
        log.log("resume", "code", "ok", f"phase={state.phase} step={state.current_step}",
                "checkpoint hash verified; finished steps are skipped")
        return self._advance(state, log)

    # -- the run ------------------------------------------------------------------

    def _advance(self, state: RunState, log: ExecutionLog) -> RunState:
        if state.phase == "start":
            if self._intake(state, log) is not None:
                return state
        clients = loader.load_clients(Path(state.data_dir))
        contracts = ContractLibrary(Path(state.data_dir), clients)
        if state.phase == "exceptions":
            self._handle_exceptions(state, log, clients, contracts)
        if state.phase == "review":
            self._review(state, log, clients, contracts)
            if state.phase == "review":
                return state
        if state.phase == "finalize":
            self._finalize(state, log)
            if state.phase != "commit":
                return state
        if state.phase == "commit":
            self._commit(state, log)
        return state

    def _intake(self, state: RunState, log: ExecutionLog):
        # 1. Input trigger
        step = "1_input_trigger"
        period = state.billing_period
        if not period or not PERIOD_FORMAT.match(period):
            return self._stop(state, log, step, f"billing period must be YYYY-MM, got {period!r}")
        log.log(step, "code", "ok", f"period={period} project={state.project_filter or 'all'}", f"run_id={state.run_id}")
        self._checkpoint(state, log, step)

        # 2. Load data
        step = "2_load_data"
        try:
            clients = loader.load_clients(self.data_dir)
            hours_rows, units_rows = loader.load_period(self.data_dir, period, state.project_filter)
        except loader.DataLoadError as exc:
            return self._stop(state, log, step, str(exc))
        if not hours_rows and not units_rows:
            return self._stop(state, log, step, f"no records found for period {period}")
        log.log(step, "code", "ok", str(self.data_dir), f"{len(hours_rows)} time entries, {len(units_rows)} unit records")
        self._checkpoint(state, log, step)

        # 3. Validate and match. Invalid records stop early: logged, skipped, supervisor notified.
        step = "3_validate_match"
        state.records = validation.validate_and_match(hours_rows, units_rows, clients)
        client_ids = {r.client_id for r in state.records.values() if r.client_id}
        state.client_id = sorted(client_ids)[0] if client_ids else None
        for record in state.records.values():
            if record.status == "invalid":
                reason = "; ".join(record.validation_errors)
                state.notify_supervisor(f"{record.project_id} skipped: {reason}")
                log.log(step, "code", "early_stop", project_id=record.project_id, stop_reason=reason)
            else:
                log.log(
                    step, "code", "ok",
                    f"hours={record.hours} units={record.units}",
                    f"h/unit={record.hours_per_unit} variance={record.variance:+.1%}",
                    project_id=record.project_id,
                )
        state.status = "validated"
        state.phase = "exceptions"
        self._checkpoint(state, log, step)
        return None

    def _handle_exceptions(self, state, log, clients, contracts) -> None:
        agent = ExceptionAgent(self.llm, contracts)
        for record in state.records.values():
            if record.status == "invalid":
                continue
            pid = record.project_id
            if record.agent_status == "in_progress":
                # The worker died while the agent was working on this project.
                log.log("7b_ai_agent", "code", "discarded", output_summary="partial agent work from interrupted worker discarded; reprocessing", project_id=pid)
                record.agent_status = None
                record.status = "validated"
            if record.status != "validated":
                continue  # finished before the interruption
            rules = clients[record.client_id]

            # 4. Detect exceptions
            record.exception_type, record.exception_details = detection.classify(record, rules)
            record.exception_flag = record.exception_type != "none"
            log.log("4_detect_exceptions", "code", "ok", output_summary=f"{record.exception_type}: {record.exception_details}", project_id=pid)

            # 5. No exception: auto-clear, bypassing the AI nodes and the checkpoint (later audit).
            if not record.exception_flag:
                record.resolution_source = "auto_clear"
                record.final_billable_hours = record.hours
                record.status = "approved"
                log.log("5_exception_branch", "code", "auto_cleared", output_summary="no exception; staged as recorded", project_id=pid)
                self._checkpoint(state, log, f"4_project_{pid}")
                continue

            # 6/7a. Simple exceptions go to the rules engine; no matching rule falls through to the agent.
            resolution = None
            if record.exception_type == "simple":
                resolution = rules_engine.resolve(record, rules)
                log.log(
                    "7a_rules_engine", "code", "resolved" if resolution else "no_rule",
                    output_summary=resolution["recommendation"] if resolution else "no rule matched; routing to AI agent",
                    project_id=pid,
                )
                if resolution:
                    record.resolution_source = "rules_engine"
                    record.recommendation = resolution
                    record.status = "resolved"

            # 7b-9. Complex (or unmatched) exceptions go to the AI agent with bounded retry.
            if resolution is None:
                record.status = "classified"
                record.agent_status = "in_progress"
                self._checkpoint(state, log, f"7b_agent_start_{pid}")
                result = agent.run(record, rules)
                state.model_calls += len(result.attempts)
                for attempt in result.attempts:
                    log.log(
                        "7b_ai_agent", "model", "ok" if attempt.ok else "retry",
                        f"sections={[s.section_id for s in result.sections]}",
                        "valid recommendation" if attempt.ok else attempt.error,
                        project_id=pid, attempt=attempt.attempt, usage=attempt.usage,
                    )
                record.agent_status = "done"
                record.retry_count = sum(1 for a in result.attempts if not a.ok)
                if result.escalated:
                    record.status = "escalated"
                    state.notify_supervisor(f"{pid} escalated after {record.retry_count} failed AI attempts")
                    log.log("9_retry_fallback", "code", "escalated", project_id=pid, stop_reason="retry limit reached")
                else:
                    record.resolution_source = "ai_agent"
                    record.recommendation = result.recommendation.model_dump()
                    record.status = "resolved"
                    log.log("8_validate_ai_output", "code", "valid", output_summary=record.recommendation["recommendation"], project_id=pid)
            self._checkpoint(state, log, f"7_project_{pid}")

        state.phase = "review"
        self._checkpoint(state, log, "7_handle_exceptions")

    # -- 10. the human checkpoint ---------------------------------------------------

    def _review(self, state, log, clients, contracts) -> None:
        now = self.now()
        # Enqueue one review item per exception. UNIQUE(run_id, project_id) makes this idempotent.
        for record in state.records.values():
            if record.status not in ("resolved", "escalated") or record.review_id:
                continue
            rules = clients[record.client_id]
            section_text = {s.section_id: s.text for s in contracts.sections(record.client_id)}
            proposal = build_proposal(record, rules, state.run_id, state.billing_period, section_text)
            escalated = record.status == "escalated"
            item = {
                "review_id": f"RV-{state.run_id[4:]}-{record.project_id}",
                "run_id": state.run_id,
                "project_id": record.project_id,
                "status": "pending",
                "reviewer_role": "supervisor",
                "proposal_json": json.dumps(proposal),
                "content_version": canonical_hash(proposal)[:12],
                "created_at": now.isoformat(timespec="seconds"),
                "expires_at": (now + timedelta(hours=self.review_window_hours)).isoformat(timespec="seconds"),
            }
            stored, created = self.store.enqueue_review(item)
            record.review_id = stored["review_id"]
            if not escalated:
                record.status = "pending_review"
            log.log(
                "10_supervisor_review", "code", "enqueued" if created else "already_queued",
                output_summary=f"{stored['review_id']} v{stored['content_version']}; conflicts={len(proposal['conflicts'])}",
                project_id=record.project_id,
            )
            if created:
                state.notify_supervisor(f"{record.project_id} needs review ({stored['review_id']}) by {item['expires_at']}")
        self._checkpoint(state, log, "10_enqueue_reviews")

        # Scripted decisions (M2 SimulatedSupervisor) go through the same event checks.
        if self.supervisor is not None:
            for item in self.store.reviews(run_id=state.run_id, status="pending"):
                record = state.records[item["project_id"]]
                decision = self.supervisor.review(record)
                if decision is None:
                    continue
                self.events.handle({
                    "event_id": f"sim-{item['review_id']}",
                    "type": "review.decision",
                    "review_id": item["review_id"],
                    "content_version": item["content_version"],
                    "decision": decision["decision"],
                    "billable_hours": decision.get("billable_hours"),
                    "note": decision.get("note"),
                    "reviewer": "simulated-supervisor",
                    "role": "supervisor",
                }, advance=False)

        self._sync_reviews(state, log)
        open_items = [i for i in self.store.reviews(run_id=state.run_id) if i["status"] in OPEN_REVIEW]
        if open_items:
            state.status = "awaiting_review"
            log.log("10_supervisor_review", "human", "pending",
                    output_summary=f"waiting on {len(open_items)} review(s): {[i['project_id'] for i in open_items]}")
            self._write_outputs(state)
            self._checkpoint(state, log, "10_awaiting_review")
            return
        state.phase = "finalize"
        self._checkpoint(state, log, "10_reviews_complete")

    def _sync_reviews(self, state, log) -> None:
        """Copy decisions from the review table into the run state (idempotent)."""
        for item in self.store.reviews(run_id=state.run_id):
            record = state.records[item["project_id"]]
            if item["status"] in ("approved", "modified"):
                if record.status == "approved" and record.approval and record.approval["event_id"] == item["decision_event_id"]:
                    continue
                record.status = "approved"
                record.supervisor_decision = item["decision"]
                record.final_billable_hours = item["authorized_hours"]
                record.final_adjustments = item["note"]
                record.approval = {
                    "action": "stage_line_for_draft_invoice",
                    "target": f"{state.run_id}/{state.billing_period}/{record.client_id}/{record.project_id}",
                    "content_version": item["content_version"],
                    "authorized_hours": item["authorized_hours"],
                    "decided_by": item["decided_by"],
                    "role": item["decided_role"],
                    "decided_at": item["decided_at"],
                    "expires_at": item["approval_expires_at"],
                    "event_id": item["decision_event_id"],
                }
                log.log("10_supervisor_review", "human", item["decision"],
                        output_summary=f"billable_hours={record.final_billable_hours} by {item['decided_by']} ({item['decided_role']})",
                        project_id=record.project_id, event_id=item["decision_event_id"])
            elif item["status"] == "rejected" and record.status != "rejected":
                record.status = "rejected"
                record.supervisor_decision = "reject"
                record.final_adjustments = item["note"]
                log.log("10_supervisor_review", "human", "reject", output_summary=item["note"] or "rejected",
                        project_id=record.project_id, event_id=item["decision_event_id"])
                state.notify_supervisor(f"{record.project_id} rejected; withheld from the invoice and returned for correction")

    def on_review_escalated(self, item: dict) -> None:
        state = self.load_state(item["run_id"])
        log = ExecutionLog(state.run_id, self.store)
        record = state.records[item["project_id"]]
        record.status = "escalated"
        record.final_adjustments = "no supervisor decision in the review window; escalated to the AR manager"
        state.notify_supervisor(f"AR manager: {item['project_id']} review timed out; escalated, nothing staged")
        log.log("10_review_timeout", "code", "escalated", project_id=item["project_id"],
                stop_reason="no decision in review window; escalated to AR manager")
        self._write_outputs(state)
        self._checkpoint(state, log, "10_review_timeout")

    # -- 11. finalize ----------------------------------------------------------------

    def _finalize(self, state, log) -> None:
        # Approvals are only good for a limited time. A stale approval goes back for review.
        now = self.now()
        stale = [
            r for r in state.records.values()
            if r.status == "approved" and r.approval and r.approval.get("expires_at") and now >= parse_time(r.approval["expires_at"])
        ]
        for record in stale:
            self.store.update_review(
                record.review_id, "approved" if record.supervisor_decision == "approve" else "modified",
                status="pending", decision=None, decided_by=None, decided_role=None, decided_at=None,
                decision_event_id=None, authorized_hours=None, approval_expires_at=None,
                expires_at=(now + timedelta(hours=self.review_window_hours)).isoformat(timespec="seconds"),
                note=f"approval expired before execution ({record.approval['expires_at']}); re-review required",
            )
            log.log("11_finalize_output", "code", "approval_expired", project_id=record.project_id,
                    stop_reason="approval expired before the invoice load; returned to review")
            record.status = "pending_review"
            record.approval = None
        if stale:
            state.phase = "review"
            state.status = "awaiting_review"
            self._checkpoint(state, log, "11_approval_expired")
            return

        state.metrics = finalize.compute_metrics(state)
        self._write_outputs(state)
        log.log("11_finalize_output", "code", "ok", output_summary=f"{state.metrics['approved']} records staged; metrics={state.metrics}")
        state.phase = "commit"
        self._checkpoint(state, log, "11_finalize_output")

    def _write_outputs(self, state) -> None:
        finalize.write_outputs(state, self.out_dir)

    # -- 12. load the draft invoice (external effect) -----------------------------------

    def _payload(self, state) -> dict:
        lines = []
        for row in finalize.staging_rows(state):
            record = state.records[row["project_id"]]
            lines.append({
                "project_id": row["project_id"],
                "unit_type": row["unit_type"],
                "units": row["units"],
                "billable_hours": row["billable_hours"],
                "resolution_source": row["resolution_source"],
                "authorization": record.approval["event_id"] if record.approval else "auto_clear",
            })
        return {"client_id": state.client_id, "billing_period": state.billing_period, "run_id": state.run_id, "lines": lines}

    def _hold(self, state, log, key, reason) -> None:
        self.store.update_commit(key, status="held", last_error=reason, updated_at=self.now_iso())
        state.status = "commit_held"
        state.stop_reason = reason
        state.notify_supervisor(f"AR manager: invoice load held for {state.billing_period}: {reason}")
        log.log("12_invoice_load", "code", "held", stop_reason=reason, idempotency_key=key)
        self._checkpoint(state, log, "12_invoice_held")

    def _bind(self, state, log, key, invoice, how) -> None:
        payload = json.loads(self.store.commit_record(key)["payload_json"])
        expected_total = round(sum(line["billable_hours"] for line in payload["lines"]), 2)
        if abs(invoice["total_hours"] - expected_total) > 1e-6 or len(invoice["lines"]) != len(payload["lines"]):
            self.store.update_commit(key, external_invoice_id=invoice["invoice_id"])
            return self._hold(state, log, key, f"invoice {invoice['invoice_id']} total {invoice['total_hours']} h != staging {expected_total} h")
        self.store.update_commit(key, status="committed", external_invoice_id=invoice["invoice_id"], updated_at=self.now_iso())
        state.invoice = {"invoice_id": invoice["invoice_id"], "idempotency_key": key, "total_hours": invoice["total_hours"],
                         "lines": len(invoice["lines"]), "bound_by": how}
        state.status = "completed"
        state.phase = "done"
        log.log("12_invoice_load", "code", "committed", output_summary=f"{invoice['invoice_id']} ({how}); total {invoice['total_hours']} h",
                idempotency_key=key)
        self._checkpoint(state, log, "12_invoice_committed")

    def _commit(self, state, log) -> None:
        payload = self._payload(state)
        if not payload["lines"]:
            state.status = "completed"
            state.phase = "done"
            log.log("12_invoice_load", "code", "skipped", output_summary="no approved lines; nothing sent to the invoicing system")
            self._checkpoint(state, log, "12_invoice_skipped")
            return
        key = f"{state.run_id}:{state.billing_period}:batch-1"
        payload_hash = canonical_hash(payload)
        record = self.store.commit_record(key)
        if record is None:
            # A second run for a period that already has a draft invoice must not create another.
            others = [c for c in self.store.commits_for_period(state.client_id, state.billing_period)
                      if c["run_id"] != state.run_id and c["status"] in ("pending", "unknown", "committed", "held")]
            now = self.now_iso()
            # The key and payload hash are saved BEFORE the call, and never regenerated.
            self.store.insert_commit({
                "idempotency_key": key, "run_id": state.run_id, "client_id": state.client_id,
                "billing_period": state.billing_period, "payload_hash": payload_hash,
                "payload_json": json.dumps(payload), "status": "pending", "attempts": 0,
                "created_at": now, "updated_at": now,
            })
            log.log("12_invoice_load", "code", "intent_saved", output_summary=f"key={key} hash={payload_hash[:12]}", idempotency_key=key)
            if others:
                return self._hold(state, log, key, f"period already has invoice commit {others[0]['idempotency_key']} ({others[0]['status']})")
            record = self.store.commit_record(key)
        elif record["payload_hash"] != payload_hash:
            return self._hold(state, log, key, "staged lines changed after the idempotency key was issued")
        if record["status"] == "committed":
            invoice = self.invoicing.find_by_key(key)
            return self._bind(state, log, key, invoice, "already committed")
        if record["status"] == "held":
            state.status = "commit_held"
            self._checkpoint(state, log, "12_invoice_held")
            return

        attempts = record["attempts"]
        uncertain = record["status"] == "unknown" or attempts > 0
        for n in range(config.MAX_COMMIT_ATTEMPTS):
            attempts += 1
            self.store.update_commit(key, attempts=attempts, updated_at=self.now_iso())
            try:
                if uncertain:
                    # Check what actually happened before sending again.
                    found = self.invoicing.find_by_key(key)
                    log.log("12_invoice_load", "code", "lookup", output_summary="found existing invoice" if found else "no invoice for key",
                            idempotency_key=key, attempt=attempts)
                    if found:
                        return self._bind(state, log, key, found, "lookup after uncertain outcome")
                invoice = self.invoicing.create_draft_invoice(key, payload_hash, payload)
                return self._bind(state, log, key, invoice, "replayed" if invoice.get("replayed") else "created")
            except InvoicingTimeout as exc:
                uncertain = True
                self.store.update_commit(key, status="unknown", last_error=str(exc), updated_at=self.now_iso())
                log.log("12_invoice_load", "code", "unknown", output_summary=f"{exc}; outcome unknown, will check before retrying",
                        idempotency_key=key, attempt=attempts)
            except InvoicingUnavailable as exc:
                self.store.update_commit(key, last_error=str(exc), updated_at=self.now_iso())
                log.log("12_invoice_load", "code", "retry", output_summary=str(exc), idempotency_key=key, attempt=attempts)
            except InvoicingConflict as exc:
                return self._hold(state, log, key, f"409 conflict: {exc}")
            if n < config.MAX_COMMIT_ATTEMPTS - 1:
                self.sleep(config.COMMIT_BACKOFF_SECONDS[min(n, len(config.COMMIT_BACKOFF_SECONDS) - 1)])
        self._hold(state, log, key, f"invoicing system unavailable after {config.MAX_COMMIT_ATTEMPTS} attempts; no new key issued")

    def release_commit(self, run_id: str, reviewer: str) -> None:
        state = self.load_state(run_id)
        log = ExecutionLog(run_id, self.store)
        key = f"{run_id}:{state.billing_period}:batch-1"
        record = self.store.commit_record(key)
        if record:
            # Back to "unknown": the next attempt looks the key up before creating anything.
            self.store.update_commit(key, status="unknown", attempts=0, last_error=f"released by {reviewer}", updated_at=self.now_iso())
        state.status = "committing"
        state.stop_reason = None
        log.log("12_invoice_load", "human", "released", output_summary=f"AR manager {reviewer} released the held commit", idempotency_key=key)
        self._checkpoint(state, log, "12_invoice_released")
