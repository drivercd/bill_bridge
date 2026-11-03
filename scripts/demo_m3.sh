#!/usr/bin/env bash
# M3 walkthrough with the mock model: crash and resume, human checkpoint via
# webhook-style inbox files (with a duplicate and a double click), lost
# acknowledgement from the invoicing system, and a timer-driven escalation.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${OUT:-output/demo}
rm -rf "$OUT"
bb() { python -m billbridge "$@" --out-dir "$OUT"; }

echo "== 1. Start the run; the worker dies inside the AI agent step"
bb run --billing-period 2026-09 --run-id run-demo0001 --simulate-crash agent || true

echo; echo "== 2. A new worker resumes from the saved checkpoint"
bb resume run-demo0001

echo; echo "== 3. Reviewer view for P-102 (what the supervisor sees)"
bb review --reviewer "demo" --review-id RV-demo0001-P-102 --show

echo; echo "== 4. Decisions arrive as files in the inbox; the invoicing API loses its first reply (504)"
bb inbox examples/inbox --invoicing-fault lost_ack

echo; echo "== 5. Re-deliver the whole inbox: every event is a duplicate, nothing changes"
bb inbox examples/inbox

echo; echo "== 6. What the receiving system actually holds"
bb invoices

echo; echo "== 7. Timeout path on a second, single-project period: no decision, timer fires 49 h later"
OUT2="$OUT-timeout"; rm -rf "$OUT2"
python -m billbridge run --billing-period 2026-09 --project-id P-102 --run-id run-demo0002 --out-dir "$OUT2"
python -m billbridge tick --now "$(python -c 'from datetime import datetime,timedelta,timezone;print((datetime.now(timezone.utc)+timedelta(hours=49)).isoformat(timespec="seconds"))')" --out-dir "$OUT2"
python -m billbridge status --out-dir "$OUT2"
python -m billbridge invoices --out-dir "$OUT2"
