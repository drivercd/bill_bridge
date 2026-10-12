"""Fixed-sequence orchestration (M2 report, section 2).

Control flow is deterministic code. The model is called in one place only
(complex exceptions) and every step writes to the execution log and checkpoint.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import detection, finalize, loader, rules_engine, validation
from .agent import ExceptionAgent
from .audit import ExecutionLog
from .contracts import ContractLibrary
from .review import SimulatedSupervisor
from .state import RunState

PERIOD_FORMAT = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class Workflow:
    def __init__(self, data_dir: Path, out_dir: Path, llm, supervisor: SimulatedSupervisor | None = None):
        self.data_dir = Path(data_dir)
        self.out_dir = Path(out_dir)
        self.llm = llm
        self.supervisor = supervisor or SimulatedSupervisor()

    # -- helpers -----------------------------------------------------------

    def _checkpoint(self, state: RunState, log: ExecutionLog, step: str) -> None:
        state.current_step = step
        state.save_checkpoint(self.out_dir / "state_checkpoint.json")
        log.save(self.out_dir / "execution_log.json")

    def _stop(self, state: RunState, log: ExecutionLog, step: str, reason: str) -> RunState:
        state.status = "stopped"
        state.stop_reason = reason
        state.notify_supervisor(f"Run stopped at {step}: {reason}")
        log.log(step, "code", "failed", stop_reason=reason)
        self._checkpoint(state, log, step)
        return state

    # -- run ---------------------------------------------------------------

    def run(self, billing_period: str | None, project_id: str | None = None) -> RunState:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        state = RunState(billing_period=billing_period, project_filter=project_id)
        log = ExecutionLog(state.run_id)

        # 1. Input trigger
        step = "1_input_trigger"
        if not billing_period or not PERIOD_FORMAT.match(billing_period):
            return self._stop(state, log, step, f"billing period must be YYYY-MM, got {billing_period!r}")
        log.log(step, "code", "ok", f"period={billing_period} project={project_id or 'all'}", f"run_id={state.run_id}")
        self._checkpoint(state, log, step)

        # 2. Load data
        step = "2_load_data"
        try:
            clients = loader.load_clients(self.data_dir)
            hours_rows, units_rows = loader.load_period(self.data_dir, billing_period, project_id)
        except loader.DataLoadError as exc:
            return self._stop(state, log, step, str(exc))
        if not hours_rows and not units_rows:
            return self._stop(state, log, step, f"no records found for period {billing_period}")
        contracts = ContractLibrary(self.data_dir, clients)
        log.log(step, "code", "ok", str(self.data_dir), f"{len(hours_rows)} time entries, {len(units_rows)} unit records")
        self._checkpoint(state, log, step)

        # 3. Validate and match. Invalid records stop early: logged, skipped, supervisor notified.
        step = "3_validate_match"
        state.records = validation.validate_and_match(hours_rows, units_rows, clients)
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
        self._checkpoint(state, log, step)

        agent = ExceptionAgent(self.llm, contracts)
        for record in state.records.values():
            if record.status == "invalid":
                continue
            rules = clients[record.client_id]
            pid = record.project_id

            # 4. Detect exceptions
            record.exception_type, record.exception_details = detection.classify(record, rules)
            record.exception_flag = record.exception_type != "none"
            record.status = "classified"
            log.log("4_detect_exceptions", "code", "ok", output_summary=f"{record.exception_type}: {record.exception_details}", project_id=pid)

            # 5. Are exceptions found? No: auto-clear, bypassing the AI nodes.
            if not record.exception_flag:
                record.resolution_source = "auto_clear"
                record.final_billable_hours = record.hours
                record.status = "approved"
                log.log("5_exception_branch", "code", "auto_cleared", output_summary="no exception; staged as recorded", project_id=pid)
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
                result = agent.run(record, rules)
                state.model_calls += len(result.attempts)
                for attempt in result.attempts:
                    log.log(
                        "7b_ai_agent", "model", "ok" if attempt.ok else "retry",
                        f"sections={[s.section_id for s in result.sections]}",
                        "valid recommendation" if attempt.ok else attempt.error,
                        project_id=pid, attempt=attempt.attempt, usage=attempt.usage,
                    )
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

            # 10. Supervisor review (mandatory for every exception)
            decision = self.supervisor.review(record)
            if decision is None:
                if record.status != "escalated":
                    record.status = "pending_review"
                log.log("10_supervisor_review", "human", "pending", output_summary="left in review queue", project_id=pid)
                continue
            record.supervisor_decision = decision["decision"]
            record.final_adjustments = decision.get("note")
            if decision["decision"] == "reject":
                record.status = "rejected"
            else:
                record.status = "approved"
                if decision["decision"] == "modify":
                    record.final_billable_hours = float(decision["billable_hours"])
                else:
                    record.final_billable_hours = record.recommendation["recommended_billable_hours"]
            log.log(
                "10_supervisor_review", "human", decision["decision"],
                output_summary=f"billable_hours={record.final_billable_hours}", project_id=pid,
            )
        self._checkpoint(state, log, "7_handle_exceptions")

        # 11. Finalize and output
        step = "11_finalize_output"
        state.metrics = finalize.compute_metrics(state)
        state.status = "completed"
        finalize.write_outputs(state, self.out_dir)
        log.log(step, "code", "ok", output_summary=f"{state.metrics['approved']} records staged; metrics={state.metrics}")
        self._checkpoint(state, log, step)
        return state
