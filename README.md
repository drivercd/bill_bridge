# BillBridge

AI-assisted billing reconciliation for engineering services: faster, more accurate invoicing.

BillBridge is the Team 3 Enterprise AI group project (Chad Driver, Jorja Baker, Minh Pham,
Sao Lognon, Yohanes Teka). It is built around an illustrative case company, **Meridian Engineering
Services**, which designs fiber routes and pole attachments for telecom and utility clients.
This repository holds the **Milestone 3 (M3) prototype**: the M2 pipeline (one client, codified
billing rules, mock data, a real LLM call for complex exceptions) plus a human checkpoint,
an event-driven path, durable state in SQLite, and recovery from interruptions and lost
acknowledgements. The M3 design and test evidence are in [`docs/m3/`](docs/m3/).

## The problem

Meridian bills each project for **billable hours** and **billable units**. These live in two
systems that aren't connected:

- Hours are logged against the project number in an HR-governed time-tracking system.
- Units completed are recorded in the engineering workspace.

At the start of every billing cycle, a supervisor reconciles the two by hand before anything
can be invoiced. With workload up and staff short, that manual QC step now delays invoices,
causes billing errors and disputes, and slows cash collection.

## The approach: workflow first, agent second

BillBridge automates the routine pull, validate and load path with **deterministic code**, and
uses a **bounded AI agent** only for exceptions that need judgment. Supervisors review only the
projects that need a decision, and each one comes with an AI-prepared explanation, a suggested
resolution, and contract citations.

Three design decisions shape the system:

1. **Deterministic workflow, bounded agent.** Extraction, matching, validation, routing, retries
   and stop conditions are code. The model is called in exactly one place (complex exceptions)
   and can use exactly one tool (contract lookup).
2. **Retrieval-augmented exceptions.** The agent receives contract clauses and must cite them.
   An output without a valid citation is rejected and retried.
3. **Human approval for every exception.** Nothing that went through the rules engine or the
   agent is staged for invoicing without a supervisor decision.
4. **Durable, idempotent execution (M3).** Run state, review items, events and invoice commits
   live in SQLite. Every event carries an id that is de-duplicated, and every external commit
   carries an idempotency key saved before the call.

## Success metrics

| Measure | Baseline | Target |
|---|---|---|
| Invoice cycle time after period close | 15 days | ≤ 3 days |
| Auto-clear rate (projects that skip supervisor review) | 0% | 70–80% |
| Supervisor hours per billing cycle | n/a | ≤ 25 |
| Billing error / dispute rate | n/a | < 2% |
| Exception recommendation acceptance rate | n/a | ≥ 80% |

On the bundled mock period (20 projects: 17 clean, 2 simple exceptions, 1 complex), the
prototype auto-clears **85%**. That shows the routing works; it is not a production estimate.

## How a run works

```
 1 Input trigger ──► 2 Load data ──► 3 Validate & match ──► 4 Detect exceptions
   (billing period)   (hours, units,   (invalid record:         (none / simple / complex)
                       contracts)       early stop, notify)              │
                                                                         ▼
                                                        5 Exceptions found?
                                             no ◄──────────────┴──────────────► yes
                                             │                                  │
                                    auto-clear, stage           6 Simple? ──► 7a Rules engine
                                    as recorded                   Complex ──► 7b AI agent
                                                                              (contract lookup)
                                                                                  │
                                                                     8 Validate AI output
                                                                     9 Retry (max 3), then
                                                                       escalate to a human
                                                                                  │
                                                                10 Supervisor review
                                                                   approve / modify / reject
                                                                                  │
                                                                11 Finalize & output
                                                                                  │
                                                                12 Load draft invoice (M3)
                                                                   idempotency key; AR manager
                                                                   signs off before it is sent
```

| Step | Actor | What it does | On failure |
|---|---|---|---|
| 1. Input trigger | code | Starts a run for a billing period (`YYYY-MM`) and optional project | Stop the run, notify supervisor |
| 2. Load data | code | Reads `time_tracking.csv`, `units.csv`, client rules and contracts; filters by period | Missing file or columns: stop the run |
| 3. Validate & match | code | Checks required fields, duplicate entries, dates, hour ranges, units; totals hours per project; computes hours per unit and variance against the contract standard | Invalid record is skipped (never sent to the model), logged, supervisor notified |
| 4. Detect exceptions | code | Classifies each project as `none`, `simple` or `complex` from the variance | Unclassifiable: treated as complex |
| 5–6. Branch | code | No exception: auto-clear. Simple: rules engine. Complex: AI agent | Unknown type: AI agent |
| 7a. Rules engine | code | Applies the contract's variance rules (for example, cap at standard + 15%) | No rule matches: AI agent |
| 7b. AI exception agent | model | Retrieves contract sections, returns a JSON recommendation with billable hours, explanation, confidence and citations | API error or invalid output: retry |
| 8. Validate AI output | code | Schema check, required fields, at least one citation to a section that was actually retrieved | Retry with feedback on what was wrong |
| 9. Retry & fallback | code | Up to 3 attempts per exception | Escalate to human review |
| 10. Supervisor review | human | One review item per exception in SQLite; the run pauses (`awaiting_review`). Decisions arrive as events: approve, modify (set hours) or reject | No decision in 48 h: escalated to the AR manager. Nothing is ever auto-approved |
| 11. Finalize & output | code | Checks each approval is still valid (72 h), writes staged records, review queue, metrics, log and checkpoint | Expired approval: back to review |
| 12. Load draft invoice | code | Saves the idempotency key and payload hash, then creates one draft invoice in the Invoicing System | 503: retry same key. 504: look up the key before resending. 3 failures or a conflict: held for the AR manager |

### Billing rules for the M2 client

The single M2 client is **Northline Telecom (illustrative)**, client ID `NLT`. Its rules are in
[`data/clients.json`](data/clients.json) and its mock contract is
[`data/contracts/northline_telecom_msa.md`](data/contracts/northline_telecom_msa.md).

| Variance vs 8.0 h/unit standard | Type | Resolution | Contract |
|---|---|---|---|
| Within ±15% | none | Auto-clear, bill as recorded | §4.1 |
| +15% to +50% | simple | Rules engine caps at 9.2 h/unit | §4.1 |
| −15% to −50% | simple | Rules engine bills recorded hours | §4.3 |
| Above +50% or below −50% | complex | AI agent recommends, citing the contract | §4.2, §4.3 |

## Quick start

Requires Python 3.10 or newer.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional; mock mode needs no key

python -m billbridge run --billing-period 2026-09     # pauses at the human checkpoint
python -m billbridge review --reviewer "Your name"    # interactive approve / modify / reject
python -m billbridge invoices                          # what the simulated Invoicing System holds
```

`python run_reconciliation.py --billing-period 2026-09` still works and is the same as
`python -m billbridge run`. Add `--simulate-supervisor` for the M2 behavior (every exception
approved by a simulated supervisor), which gives the M2 summary: 20 projects, 17 auto-cleared,
2 simple and 1 complex exception, auto-clear rate 0.85, 1 model call.

The full M3 walkthrough (crash and resume, decisions arriving as webhook files with a duplicate
and a double click, a lost invoicing acknowledgement, and a timeout) is one script:

```bash
./scripts/demo_m3.sh
```

### Commands

| Command | Purpose |
|---|---|
| `run --billing-period YYYY-MM [--project-id P] [--run-id R]` | Start a run. Pauses with status `awaiting_review` when there are exceptions |
| `review --reviewer NAME [--role supervisor\|ar_manager] [--review-id ID --decision approve\|modify\|reject --hours H --note T] [--show]` | The human checkpoint. Without `--decision` it prompts |
| `event FILE...` / `inbox FOLDER` | Handle event JSON files (simulated webhook), see [`examples/inbox/`](examples/inbox/) |
| `tick [--now ISO]` | Timer event: expire overdue reviews and escalate them to the AR manager |
| `resume RUN_ID` | Continue a run from its saved checkpoint |
| `retry-commit RUN_ID --reviewer NAME` | AR manager releases a held invoice load (same idempotency key) |
| `status` / `invoices` | Runs, review queue and commits / what the receiving system actually holds |

Every command takes `--out-dir` (default `output`), `--data-dir` (default `data`), `--llm mock|live`
and `--invoicing-fault unavailable|lost_ack` (inject a fault into the next Invoicing System call).
`run` also takes `--simulate-crash agent` (the worker dies inside the AI agent step) and
`--decisions FILE` (scripted decisions, see [`examples/supervisor_decisions.json`](examples/supervisor_decisions.json)).

### Live model mode

Set `ANTHROPIC_API_KEY` and run with `--llm live`. The agent calls `claude-opus-5-5` by default
(override with `BILLBRIDGE_MODEL`) with low effort and output constrained to a JSON schema, and
it enables the API's server-side fallback so a policy decline is retried on another model. The M2
report specified temperature 0.0–0.2. Current Claude models don't accept a temperature setting,
so consistency comes from the schema, the citation check and the retry rules instead.

The live path has been exercised up to the API (a bad key is retried three times, then escalated)
but has not yet been run end to end with a real key.

## Outputs and state

| File in `--out-dir` | Contents |
|---|---|
| `billbridge.db` | SQLite: `runs` (checkpoint JSON + hash), `review_items`, `events`, `event_receipts`, `invoice_commits`, `execution_log` |
| `invoicing_system.db` | The simulated Invoicing System's own records (`invoices`, `requests`), used to verify effects |
| `invoicing_staging.json` | Approved lines with their approval (who, version, expiry) and contract citations |
| `review_queue.json` | Everything not staged: invalid records, escalations, rejections, pending reviews |
| `execution_log.json` | JSON export of the run's log: step, timestamp, actor, status, event id, idempotency key, tokens |
| `state_checkpoint.json` | JSON export of the latest run state (the authoritative copy is in SQLite) |

## Tests

```bash
python -m pytest              # 30 tests: the M2 cases plus M3 oversight, events and recovery
python scripts/m3_cases.py    # the four M3 report cases; writes docs/m3/test_evidence.md
```

| Test | Input | Expected |
|---|---|---|
| M2 case 1: clean run | P-101, 40 h, 5 units (8 h/unit) | Auto-cleared, zero model calls |
| M2 case 2: complex exception | P-102, 120 h, 3 units (40 h/unit) | Agent cites §4.2 and recommends 30.0 h, confidence 0.82 |
| M2 case 3: invalid input | P-102, units missing | Early stop, zero model calls, supervisor notified |
| M3 case 1: approved run | Approvals arrive in a new process | One draft invoice in the receiver, P-102 at 30.0 h |
| M3 case 2: rejection / timeout | Reject P-102; or no decision for 49 h | Line withheld; or escalated to AR manager, late approval refused, no invoice |
| M3 case 3: duplicates and retry | Same event 3 times, double click, a 503, period-close twice | One effect, one invoice, one idempotency key, one run |
| M3 case 4: crash and lost ack | Worker dies in the agent step; 504 after the invoice was created | Resume skips finished steps, no duplicate review items; lookup binds to the existing invoice |
| Plus | Stale version, unknown event, unauthorized role, approval expiry, half-processed event, duplicate period | See [`tests/test_m3_oversight.py`](tests/test_m3_oversight.py) |

## Repository layout

```
run_reconciliation.py        CLI entry point
billbridge/
  __main__.py                M3 command line (run, review, event, inbox, tick, resume, ...)
  workflow.py                Fixed-sequence orchestration, phases, checkpoint/resume, invoice commit
  events.py                  Event handler: de-duplication, late/stale/unknown checks, timer
  store.py                   SQLite state: runs, review queue, events, commits, execution log
  invoicing_sim.py           Simulated Invoicing System (idempotency keys, fault injection)
  loader.py                  Step 2: CSV and client-rule loading
  validation.py              Step 3: validation and hours-to-units matching
  detection.py               Step 4: none / simple / complex classification
  rules_engine.py            Step 7a: rules for simple exceptions
  contracts.py               Mock contract lookup (keyword retrieval over contract Markdown)
  agent.py                   Steps 7b–9: AI agent, output schema, validation, retry
  review.py                  Step 10: review proposal, conflict checks, reviewer view, simulated supervisor
  finalize.py                Step 11: staging, review queue, metrics
  state.py, audit.py         Run state, checkpoint and execution log
data/
  time_tracking.csv          Mock hours (145 entries, period 2026-09)
  units.csv                  Mock units (20 projects)
  clients.json               Per-client billing rules
  contracts/                 Mock contract documents
examples/                    Sample supervisor decisions and event files (inbox = simulated webhook)
scripts/                     demo_m3.sh walkthrough, m3_cases.py report evidence
docs/m3/                     M3 design notes, decision records, diagrams, test evidence
tests/                       End-to-end tests
```

All data is synthetic. No real HR, client or contract data is used.

## Status

| Component | Status |
|---|---|
| Input trigger, data loader, validation and matching | Implemented |
| Exception detection, rules engine | Implemented |
| AI exception agent (Anthropic API) | Implemented; live run with a real key not yet verified |
| Schema validation, citation check, hours and quote safety bounds, retry and escalation | Implemented |
| Contract lookup | Implemented with mock local files |
| Human checkpoint (review item with evidence, rule, conflicts, version, expiry) | Implemented, command-line prompt |
| Event handling (decision, timer, period close, commit retry) with de-duplication | Implemented; events from CLI and JSON files |
| Durable state and resume from checkpoint | Implemented (SQLite) |
| Invoice load with idempotency key, lookup after uncertain outcome | Implemented against a simulated Invoicing System |
| Time-tracking, units and contract data | Mock |
| Review web UI, notifications, Chroma index, monitoring dashboard | Planned |

## Roadmap (M4 and beyond)

- Governance, security and risk assessment (M4).
- Build a labeled set of 20+ complex cases to measure citation accuracy and calibrate confidence.
- Replace the command-line checkpoint with a web review UI with SSO roles.
- Replace SQLite with managed PostgreSQL and the keyword lookup with a Chroma index.
- Run the live model path end to end with a real key and record cost and latency per call.
