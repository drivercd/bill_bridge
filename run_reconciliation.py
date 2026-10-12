"""Command-line entry point for a BillBridge reconciliation run.

    python run_reconciliation.py --billing-period 2026-09
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from billbridge import config
from billbridge.agent import AnthropicLLM, MockLLM
from billbridge.review import SimulatedSupervisor
from billbridge.workflow import Workflow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile billable hours and units for one billing period.")
    parser.add_argument("--billing-period", required=True, help="YYYY-MM, e.g. 2026-09")
    parser.add_argument("--project-id", help="run a single project")
    parser.add_argument("--data-dir", default="data", type=Path)
    parser.add_argument("--out-dir", default="output", type=Path)
    parser.add_argument("--llm", choices=["mock", "live"], default=None,
                        help="mock (no API key) or live (Anthropic API); default from BILLBRIDGE_LLM_MODE")
    parser.add_argument("--decisions", type=Path,
                        help="JSON file of scripted supervisor decisions keyed by project_id")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    mode = args.llm or config.llm_mode()
    llm = AnthropicLLM() if mode == "live" else MockLLM()
    supervisor = SimulatedSupervisor.from_file(args.decisions) if args.decisions else SimulatedSupervisor()

    state = Workflow(args.data_dir, args.out_dir, llm, supervisor).run(args.billing_period, args.project_id)

    print(f"Run {state.run_id} ({mode} model): {state.status}")
    if state.stop_reason:
        print(f"Stopped: {state.stop_reason}")
    for key, value in state.metrics.items():
        print(f"  {key}: {value}")
    for note in state.notifications:
        print(f"  supervisor notice: {note}")
    print(f"Outputs written to {args.out_dir}/")
    return 0 if state.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
