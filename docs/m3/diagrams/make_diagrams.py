"""Generate the M3 diagrams as SVG (no dependencies).

    python docs/m3/diagrams/make_diagrams.py

Writes component.svg, deployment.svg and state.svg next to this file.
Edit the box lists below to change a diagram.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
INK, MUTED, ACCENT, NEW_FILL, BOUND = "#1f2937", "#6b7280", "#0f766e", "#e6f4f1", "#94a3b8"
FONT = "font-family='Helvetica, Arial, sans-serif'"


class Svg:
    def __init__(self, w, h):
        self.w, self.h, self.parts = w, h, []

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, size=12, weight="normal", color=INK, anchor="middle", italic=False):
        style = " font-style='italic'" if italic else ""
        self.add(f"<text x='{x}' y='{y}' {FONT} font-size='{size}' font-weight='{weight}' fill='{color}' "
                 f"text-anchor='{anchor}'{style}>{escape(s)}</text>")

    def box(self, x, y, w, h, title, lines=(), status="implemented", new=False, title_size=12.5):
        dash = {"implemented": "", "simulated": " stroke-dasharray='6 4'", "planned": " stroke-dasharray='2 3'"}[status]
        stroke = MUTED if status == "planned" else INK
        fill = NEW_FILL if new else "#ffffff"
        self.add(f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='6' fill='{fill}' stroke='{stroke}' stroke-width='1.4'{dash}/>")
        color = MUTED if status == "planned" else INK
        self.text(x + w / 2, y + 17, title, title_size, "bold", color)
        for i, line in enumerate(lines):
            self.text(x + w / 2, y + 32 + i * 13, line, 10.5, color=MUTED)
        if new:
            self.add(f"<rect x='{x + w - 34}' y='{y - 7}' width='30' height='14' rx='3' fill='{ACCENT}'/>")
            self.text(x + w - 19, y + 3.5, "M3", 9, "bold", "#ffffff")

    def arrow(self, pts, label=None, lx=None, ly=None, color=INK, dashed=False, both=False):
        d = "M " + " L ".join(f"{x} {y}" for x, y in pts)
        dash = " stroke-dasharray='5 4'" if dashed else ""
        start = " marker-start='url(#arrS)'" if both else ""
        self.add(f"<path d='{d}' fill='none' stroke='{color}' stroke-width='1.3'{dash} marker-end='url(#arr)'{start}/>")
        if label:
            x = lx if lx is not None else (pts[0][0] + pts[-1][0]) / 2
            y = ly if ly is not None else (pts[0][1] + pts[-1][1]) / 2 - 4
            self.text(x, y, label, 10, color=MUTED)

    def region(self, x, y, w, h, label, color=BOUND, dashed=True):
        dash = " stroke-dasharray='8 5'" if dashed else ""
        self.add(f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='10' fill='none' stroke='{color}' stroke-width='1.6'{dash}/>")
        self.text(x + 12, y + 18, label, 12, "bold", color, anchor="start")

    def legend(self, x, y):
        items = [("implemented", "Implemented"), ("simulated", "Simulated"), ("planned", "Planned")]
        for i, (st, label) in enumerate(items):
            bx = x + i * 120
            dash = {"implemented": "", "simulated": " stroke-dasharray='6 4'", "planned": " stroke-dasharray='2 3'"}[st]
            self.add(f"<rect x='{bx}' y='{y}' width='26' height='14' rx='3' fill='#fff' stroke='{INK if st != 'planned' else MUTED}'{dash}/>")
            self.text(bx + 32, y + 11, label, 10.5, anchor="start")
        bx = x + 360
        self.add(f"<rect x='{bx}' y='{y}' width='26' height='14' rx='3' fill='{NEW_FILL}' stroke='{INK}'/>")
        self.text(bx + 32, y + 11, "New or changed in M3", 10.5, anchor="start")

    def write(self, name):
        defs = ("<defs><marker id='arr' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'>"
                f"<path d='M0,0 L10,5 L0,10 z' fill='{INK}'/></marker>"
                "<marker id='arrS' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'>"
                f"<path d='M0,0 L10,5 L0,10 z' fill='{INK}'/></marker></defs>")
        svg = (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {self.w} {self.h}' width='{self.w}' height='{self.h}'>"
               f"<rect width='100%' height='100%' fill='#ffffff'/>{defs}{''.join(self.parts)}</svg>")
        (HERE / name).write_text(svg)


def component():
    s = Svg(1060, 640)
    s.text(530, 24, "BillBridge M3 component view: checkpoint, events, state and system boundary", 15, "bold")
    s.legend(300, 36)

    # external sources (left)
    s.region(14, 70, 180, 330, "Enterprise sources")
    s.box(28, 100, 152, 52, "Time-tracking (HR)", ["mock CSV"], "simulated")
    s.box(28, 172, 152, 52, "Engineering units", ["mock CSV"], "simulated")
    s.box(28, 244, 152, 52, "Contract documents", ["mock Markdown MSA"], "simulated")
    s.box(28, 316, 152, 52, "Period-close signal", ["run.requested event"], "simulated", new=True)

    # boundary
    s.region(214, 70, 600, 548, "BillBridge system boundary", color=ACCENT)
    s.box(232, 100, 170, 64, "Orchestrator", ["fixed sequence, phases", "resume from checkpoint"], new=True)
    s.box(232, 184, 170, 52, "Load, validate, match", ["steps 2-3"])
    s.box(232, 254, 170, 52, "Detect + rules engine", ["steps 4-7a"])
    s.box(422, 184, 180, 64, "AI exception agent", ["steps 7b-9, contract lookup", "+ hours/quote safety bounds"], new=True)
    s.box(422, 100, 180, 64, "Event handler", ["dedupe by event_id", "late/stale/unknown checks"], new=True)
    s.box(622, 100, 176, 64, "Timer", ["timer.tick: expire reviews", "escalate to AR manager"], "simulated", new=True)
    s.box(422, 268, 180, 76, "Human checkpoint", ["review item: action, evidence,", "rule, conflicts, version,", "expiry (CLI prompt)"], new=True)
    s.box(622, 268, 176, 76, "Invoice commit (outbox)", ["idempotency key saved", "before the call; lookup", "after uncertain outcome"], new=True)
    s.box(232, 380, 566, 92, "Durable state: SQLite billbridge.db", [
        "runs (checkpoint + hash)  |  review_items (UNIQUE run_id, project_id)  |  events + event_receipts",
        "invoice_commits (idempotency key, payload hash, status)  |  execution_log (one row per step/event)",
        "JSON exports: invoicing_staging, review_queue, execution_log, state_checkpoint"], new=True, title_size=13)
    s.box(232, 494, 270, 52, "Review web UI", ["replaces the CLI prompt"], "planned")
    s.box(528, 494, 270, 52, "Chroma contract index", ["replaces keyword lookup"], "planned")
    s.box(232, 556, 566, 44, "Monitoring dashboard + alerts (run state, stuck reviews, held commits, cost)", [], "planned")

    # right side externals
    s.region(834, 70, 212, 548, "External services and people")
    s.box(848, 100, 184, 52, "LLM provider", ["Anthropic API (mock default;"], "implemented")
    s.text(940, 145, "live path unverified)", 10.5, color=MUTED)
    s.box(848, 268, 184, 64, "Invoicing System", ["simulated receiver, own", "SQLite; draft invoices"], "simulated", new=True)
    s.box(848, 356, 184, 64, "AR manager", ["escalations, held commits,", "signs invoice before send"])
    s.box(848, 440, 184, 52, "Billing supervisor", ["decides review items"])
    s.box(848, 508, 184, 46, "Notifications", ["email / chat (CLI notices now)"], "planned")
    s.box(848, 566, 184, 44, "Client", ["gets invoice after AR sign-off"], "planned", title_size=12)

    # arrows
    for y in (126, 198, 270):
        s.arrow([(180, y), (232, 210 if y != 126 else 205)])
    s.arrow([(180, 342), (222, 342), (222, 132), (232, 132)])
    s.text(104, 384, "emits run.requested", 10, color=MUTED)
    s.arrow([(402, 132), (422, 132)])
    s.arrow([(402, 215), (422, 215)])
    s.arrow([(602, 205), (838, 205), (838, 126), (848, 126)], "prompt + clauses", 720, 199)
    s.arrow([(512, 248), (512, 268)])
    s.arrow([(602, 150), (612, 150), (612, 290), (602, 290)])
    s.arrow([(622, 132), (602, 132)])
    s.arrow([(602, 320), (622, 320)])
    s.arrow([(798, 300), (848, 300)], "create", 823, 316)
    s.arrow([(940, 332), (940, 356)], dashed=True)
    s.arrow([(798, 150), (826, 150), (826, 388), (848, 388)], dashed=True)
    s.text(812, 176, "escalate", 10, color=MUTED, anchor="end")
    s.arrow([(848, 466), (812, 466), (812, 362), (560, 362), (560, 344)], dashed=True)
    s.text(690, 357, "decision", 10, color=MUTED)
    s.arrow([(470, 344), (470, 380)], "state", 492, 374)
    s.arrow([(317, 306), (317, 380)])
    s.arrow([(760, 344), (760, 380)])
    s.write("component.svg")


def deployment():
    s = Svg(1060, 520)
    s.text(530, 24, "Production deployment view (proposed; nothing is deployed for M3)", 15, "bold")
    s.region(20, 46, 700, 456, "Meridian cloud account (private network, one region)", color=ACCENT, dashed=False)
    s.box(40, 80, 200, 78, "Review web app", ["container, 1 replica", "SSO login; supervisor +", "AR manager roles"], "planned")
    s.box(260, 80, 210, 78, "Event intake API", ["container; webhook + form", "posts; writes event row", "then enqueues"], "planned")
    s.box(490, 80, 210, 78, "Scheduler", ["cron: period close (day 1),", "timer.tick every 15 min,", "reconcile held commits"], "planned")
    s.box(40, 190, 320, 92, "Orchestrator workers (2 containers)", ["same code as the prototype: phases, checkpoint", "after each project, lease per run; agent calls", "go through the model gateway"], "implemented")
    s.box(380, 190, 320, 92, "Model gateway", ["holds the API key, logs tokens and cost per", "run_id, enforces monthly budget and", "timeouts (60 s) / retries (3)"], "planned")
    s.box(40, 316, 320, 92, "Managed PostgreSQL (SQLite today)", ["same tables as the prototype: runs, review_items,", "events, invoice_commits, execution_log;", "point-in-time recovery, 7-year retention"], "planned")
    s.box(380, 316, 320, 92, "Logs + metrics", ["structured JSON logs (run_id, event_id,", "idempotency_key), dashboards and alerts", "on stuck reviews and held commits"], "planned")
    s.box(40, 430, 320, 56, "Object storage", ["source extracts per period, contract PDFs, exports"], "planned")
    s.box(380, 430, 320, 56, "Vector index (Chroma) on the worker", ["contract clauses per client"], "planned")
    s.region(740, 46, 300, 456, "Outside Meridian's network")
    s.box(760, 90, 260, 64, "Anthropic API", ["exception agent only; project", "figures + clauses, no personal data"], "implemented")
    s.box(760, 184, 260, 64, "Invoicing System API", ["draft invoices with", "Idempotency-Key header"], "simulated")
    s.box(760, 278, 260, 64, "HR time-tracking + workspace", ["read-only nightly extracts"], "simulated")
    s.box(760, 372, 260, 64, "Email / chat", ["review requests, escalations"], "planned")
    s.arrow([(360, 236), (380, 236)])
    s.arrow([(700, 236), (730, 236), (730, 122), (760, 122)])
    s.arrow([(360, 270), (370, 270), (370, 292), (740, 292), (740, 216), (760, 216)])
    s.arrow([(200, 282), (200, 316)], "state", 222, 304)
    s.arrow([(365, 158), (300, 190)])
    s.arrow([(595, 158), (300, 192)])
    s.arrow([(140, 158), (140, 190)])
    s.arrow([(760, 306), (375, 306), (375, 282)], dashed=True)
    s.arrow([(360, 362), (380, 362)])
    s.legend(330, 504 - 0)
    s.write("deployment.svg")


def state():
    s = Svg(1060, 570)
    s.text(530, 24, "Run and review-item states: normal progress, waiting, failure and escalation", 15, "bold")
    W = 140

    def b(x, y, title, sub, new=True, h=50):
        s.box(x, y, W, h, title, sub if isinstance(sub, list) else [sub], new=new)

    b(20, 70, "started", "trigger received", new=False)
    b(200, 70, "validated", "load + validate", new=False)
    b(380, 70, "exceptions", "checkpoint per project")
    b(560, 70, "awaiting_review", "WAITING")
    b(740, 70, "finalize", "approval expiry check")
    b(200, 190, "stopped", "FAILURE: input", new=False)
    b(380, 190, "interrupted", "FAILURE: crash")
    b(740, 190, "commit pending", "key + hash saved")
    b(905, 190, "completed", "invoice bound")
    b(740, 320, "outcome unknown", "504 / lost ack")
    b(740, 440, "commit_held", "ESCALATION: AR mgr")
    A = s.arrow
    A([(160, 95), (200, 95)])
    A([(340, 95), (380, 95)])
    A([(520, 95), (560, 95)])
    s.text(540, 88, "queued", 9.5, color=MUTED)
    A([(700, 95), (740, 95)])
    s.text(720, 88, "decided", 9.5, color=MUTED)
    A([(810, 70), (810, 52), (630, 52), (630, 70)], "approval expired: back to review", 720, 47)
    s.text(630, 136, "timeout: item escalates to", 10, color=MUTED)
    s.text(630, 149, "the AR manager, run keeps waiting", 10, color=MUTED)
    s.text(450, 136, "AI fails 3x: item escalated", 10, color=MUTED)
    A([(90, 120), (90, 215), (200, 215)])
    s.text(96, 168, "bad period /", 10, color=MUTED, anchor="start")
    s.text(96, 181, "missing file", 10, color=MUTED, anchor="start")
    A([(470, 145), (470, 190)])
    s.text(478, 172, "crash", 10, color=MUTED, anchor="start")
    A([(410, 190), (410, 145)])
    s.text(402, 172, "resume", 10, color=MUTED, anchor="end")
    A([(810, 120), (810, 190)])
    s.text(818, 160, "approvals valid", 10, color=MUTED, anchor="start")
    A([(880, 215), (905, 215)])
    s.text(975, 258, "201 created, replay,", 10, color=MUTED)
    s.text(975, 271, "or lookup found", 10, color=MUTED)
    A([(790, 240), (790, 320)])
    s.text(782, 285, "504 / no ack", 10, color=MUTED, anchor="end")
    A([(840, 320), (840, 240)])
    s.text(848, 285, "lookup by key", 10, color=MUTED, anchor="start")
    A([(790, 370), (790, 440)])
    s.text(782, 410, "3 failed attempts", 10, color=MUTED, anchor="end")
    A([(840, 440), (840, 370)])
    s.text(848, 410, "AR manager commit.retry", 10, color=MUTED, anchor="start")
    s.text(810, 515, "409 conflict or second invoice", 10, color=MUTED)
    s.text(810, 528, "for the same period also hold here", 10, color=MUTED)

    s.region(20, 290, 660, 265, "Review item (one per run + project; UNIQUE run_id, project_id)")
    b(40, 335, "pending", ["supervisor decides", "within 48 h"], h=56)
    b(270, 320, "approved / modified", ["hours + version bound", "valid 72 h"], h=56)
    b(270, 400, "rejected", "line withheld")
    b(40, 470, "escalated", ["AR manager only", "no auto-expiry"], h=56)
    b(270, 475, "AR decision", "approve / modify / reject")
    A([(180, 355), (270, 348)], "approve", 225, 344)
    A([(180, 378), (270, 425)], "reject", 212, 412)
    A([(110, 391), (110, 470)])
    s.text(118, 435, "timer tick or", 10, color=MUTED, anchor="start")
    s.text(118, 448, "late decision", 10, color=MUTED, anchor="start")
    A([(180, 500), (270, 500)])
    s.text(555, 340, "Events that cause no transition", 10.5, "bold")
    for i, line in enumerate(["duplicate event_id: logged, no effect", "decision on a decided item: ignored",
                              "stale content version: refused", "unknown type / review / role: refused",
                              "modify above recorded hours: refused", "half-processed event: finished on redelivery"]):
        s.text(555, 358 + i * 14, line, 10, color=MUTED)
    s.write("state.svg")


if __name__ == "__main__":
    component()
    deployment()
    state()
    print("wrote component.svg, deployment.svg, state.svg")
