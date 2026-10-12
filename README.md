# BillBridge

AI-assisted billing reconciliation for engineering services: faster, more accurate invoicing.

BillBridge is the Team 3 Enterprise AI group project (Chad Driver, Jorja Baker, Minh Pham,
Sao Lognon, Yohanes Teka). It is built around an illustrative case company, **Meridian Engineering
Services**, which designs fiber routes and pole attachments for telecom and utility clients.
This repository holds the **Milestone 2 (M2) prototype**: one client, codified billing rules,
mock data, and a real LLM call for complex exceptions.

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
| 10. Supervisor review | human | Approve, modify (set hours) or reject. Simulated in M2 | No decision: stays in the review queue |
| 11. Finalize & output | code | Writes staged records, the review queue, metrics, log and checkpoint | n/a |

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

python run_reconciliation.py --billing-period 2026-09
```

Expected summary:

```
Run run-xxxxxxxx (mock model): completed
  projects: 20
  auto_cleared: 17
  simple_exceptions: 2
  complex_exceptions: 1
  auto_clear_rate: 0.85
  model_calls: 1
```

### Options

| Flag | Default | Purpose |
|---|---|---|
| `--billing-period` | required | Period to reconcile, `YYYY-MM` |
| `--project-id` | all | Run a single project, e.g. `P-102` |
| `--data-dir` | `data` | Source CSVs, client rules and contracts |
| `--out-dir` | `output` | Where outputs are written |
| `--llm` | `BILLBRIDGE_LLM_MODE` or `mock` | `mock` (deterministic, no key) or `live` (Anthropic API) |
| `--decisions` | none | JSON of scripted supervisor decisions, see [`examples/supervisor_decisions.json`](examples/supervisor_decisions.json) |

### Live model mode

Set `ANTHROPIC_API_KEY` and run with `--llm live`. The agent calls `claude-opus-5-5` by default
(override with `BILLBRIDGE_MODEL`) with low effort and output constrained to a JSON schema, and
it enables the API's server-side fallback so a policy decline is retried on another model. The M2
report specified temperature 0.0–0.2. Current Claude models don't accept a temperature setting,
so consistency comes from the schema, the citation check and the retry rules instead.

The live path has been exercised up to the API (a bad key is retried three times, then escalated)
but has not yet been run end to end with a real key.

## Outputs

Each run writes four files to `--out-dir`:

| File | Contents |
|---|---|
| `invoicing_staging.json` | Approved records ready for invoicing: recorded vs billable hours, how each was resolved, supervisor decision, contract citations |
| `review_queue.json` | Everything not staged: invalid records, escalations, rejections, pending reviews, each with a reason |
| `execution_log.json` | One entry per step and project: timestamp, actor (`code`, `model`, `human`), input and output summaries, status, stop reason, retry attempt and token usage |
| `state_checkpoint.json` | The full run state after the last completed step. A rerun starts from the beginning; resume-from-checkpoint is planned for M3 |

## Tests

```bash
python -m pytest
```

The tests cover the three M2 test cases and the failure paths:

| Test | Input | Expected |
|---|---|---|
| Case 1: clean run | P-101, 40 h, 5 units (8 h/unit) | Auto-cleared, zero model calls |
| Case 2: complex exception | P-102, 120 h, 3 units (40 h/unit) | Agent cites §4.2 and recommends 30.0 h, confidence 0.82 |
| Case 3: invalid input | P-102, units missing | Early stop, zero model calls, supervisor notified |
| Retry then success | Two scripted model failures | Approved on attempt 3 |
| Retry exhaustion | Three scripted model failures | Escalated to human, not staged |
| Plus | Rules engine, modify/reject, missing file, bad period, duplicate entries, invented citation | See [`tests/test_workflow.py`](tests/test_workflow.py) |

## Repository layout

```
run_reconciliation.py        CLI entry point
billbridge/
  workflow.py                Fixed-sequence orchestration (steps 1–11)
  loader.py                  Step 2: CSV and client-rule loading
  validation.py              Step 3: validation and hours-to-units matching
  detection.py               Step 4: none / simple / complex classification
  rules_engine.py            Step 7a: rules for simple exceptions
  contracts.py               Mock contract lookup (keyword retrieval over contract Markdown)
  agent.py                   Steps 7b–9: AI agent, output schema, validation, retry
  review.py                  Step 10: simulated supervisor
  finalize.py                Step 11: staging, review queue, metrics
  state.py, audit.py         Run state, checkpoint and execution log
data/
  time_tracking.csv          Mock hours (145 entries, period 2026-09)
  units.csv                  Mock units (20 projects)
  clients.json               Per-client billing rules
  contracts/                 Mock contract documents
examples/                    Sample supervisor decisions
tests/                       End-to-end tests
```

All data is synthetic. No real HR, client or contract data is used.

## Status

| Component | Status |
|---|---|
| Input trigger, data loader, validation and matching | Implemented |
| Exception detection, rules engine | Implemented |
| AI exception agent (Anthropic API) | Implemented; live run with a real key not yet verified |
| JSON schema validation, citation check, retry and escalation | Implemented |
| Contract lookup | Implemented with mock local files |
| Supervisor review | Simulated |
| Finalize, outputs, execution log, checkpoint | Implemented |
| Time-tracking, units and contract data | Mock |

## Roadmap (M3)

- Replace the keyword contract lookup with a Chroma vector index over contract Markdown.
- Build an interactive supervisor review UI (Streamlit or notebook).
- Expand mock data to 100+ projects across 3 clients.
- Handle provider timeouts separately from schema failures.
- Build a labeled set of 20+ contract clauses to measure citation accuracy, then measure
  auto-clear rate, supervisor override rate and citation accuracy on the larger set.
- Resume a stopped run from its checkpoint.
- Integrate with the invoicing system and add an audit dashboard.
