"""Command-line entry point kept from M2.

    python run_reconciliation.py --billing-period 2026-09

Equivalent to `python -m billbridge run ...`. Since M3 a run pauses at the
human checkpoint when there are exceptions; pass --simulate-supervisor (or
--decisions FILE) for the M2 behavior of scripted approvals.
"""

from __future__ import annotations

import sys

from billbridge.__main__ import main

if __name__ == "__main__":
    sys.exit(main(["run", *sys.argv[1:]]))
