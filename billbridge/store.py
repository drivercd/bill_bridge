"""Durable workflow state (M3): one SQLite file holds runs, the review queue,
processed events, invoice commits and the execution log.

Everything a later process needs to resume a run lives here, so a pending
review survives the process that created it. Every write is a short
transaction, committed before the next side effect.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    client_id      TEXT,
    billing_period TEXT,
    status         TEXT NOT NULL,
    current_step   TEXT,
    started_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    state_json     TEXT NOT NULL,
    state_hash     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_items (
    review_id            TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL,
    project_id           TEXT NOT NULL,
    status               TEXT NOT NULL,   -- pending | approved | modified | rejected | escalated
    reviewer_role        TEXT NOT NULL,   -- role allowed to decide now: supervisor | ar_manager
    proposal_json        TEXT NOT NULL,
    content_version      TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    expires_at           TEXT,
    decision             TEXT,
    decided_by           TEXT,
    decided_role         TEXT,
    decided_at           TEXT,
    decision_event_id    TEXT,
    authorized_hours     REAL,
    approval_expires_at  TEXT,
    note                 TEXT,
    UNIQUE (run_id, project_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    run_id       TEXT,
    payload_json TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    outcome      TEXT NOT NULL,   -- applied | duplicate | ignored | rejected
    detail       TEXT
);

-- Rejected duplicates of an event id are kept here so repeats stay visible.
CREATE TABLE IF NOT EXISTS event_receipts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS invoice_commits (
    idempotency_key     TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    client_id           TEXT NOT NULL,
    billing_period      TEXT NOT NULL,
    payload_hash        TEXT NOT NULL,
    payload_json        TEXT NOT NULL,
    status              TEXT NOT NULL,  -- pending | unknown | committed | held
    attempts            INTEGER NOT NULL DEFAULT 0,
    external_invoice_id TEXT,
    last_error          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    step         TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    actor        TEXT NOT NULL,
    status       TEXT NOT NULL,
    project_id   TEXT,
    entry_json   TEXT NOT NULL
);
"""


def canonical_hash(obj) -> str:
    """Stable SHA-256 of a JSON-serializable object (sorted keys, no whitespace)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)  # autocommit; explicit BEGIN below
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- runs ---------------------------------------------------------------

    def save_run(self, state_dict: dict, now: str) -> str:
        state_hash = canonical_hash(state_dict)
        with self.tx() as c:
            c.execute(
                """INSERT INTO runs (run_id, client_id, billing_period, status, current_step,
                                     started_at, updated_at, state_json, state_hash)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     client_id=excluded.client_id, status=excluded.status,
                     current_step=excluded.current_step, updated_at=excluded.updated_at,
                     state_json=excluded.state_json, state_hash=excluded.state_hash""",
                (
                    state_dict["run_id"], state_dict.get("client_id"), state_dict["billing_period"],
                    state_dict["status"], state_dict.get("current_step"), state_dict["started_at"], now,
                    json.dumps(state_dict), state_hash,
                ),
            )
        return state_hash

    def load_run(self, run_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        state = json.loads(row["state_json"])
        if canonical_hash(state) != row["state_hash"]:
            raise RuntimeError(f"checkpoint for {run_id} failed its integrity check")
        return state

    def runs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs ORDER BY started_at").fetchall()

    # -- review queue -------------------------------------------------------

    def enqueue_review(self, item: dict) -> tuple[dict, bool]:
        """Insert a review item once per (run_id, project_id). Returns (item, created)."""
        with self.tx() as c:
            existing = c.execute(
                "SELECT * FROM review_items WHERE run_id=? AND project_id=?", (item["run_id"], item["project_id"])
            ).fetchone()
            if existing is not None:
                return dict(existing), False
            cols = ",".join(item)
            c.execute(f"INSERT INTO review_items ({cols}) VALUES ({','.join('?' * len(item))})", tuple(item.values()))
        return item, True

    def review(self, review_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM review_items WHERE review_id=?", (review_id,)).fetchone()
        return dict(row) if row else None

    def reviews(self, run_id: str | None = None, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM review_items WHERE 1=1", []
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY created_at, project_id", args)]

    def update_review(self, review_id: str, expected_status: str, **fields) -> bool:
        """Compare-and-set on status so two deciders can't both win."""
        sets = ",".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            cur = c.execute(
                f"UPDATE review_items SET {sets} WHERE review_id=? AND status=?",
                (*fields.values(), review_id, expected_status),
            )
            return cur.rowcount == 1

    # -- events -------------------------------------------------------------

    def record_event(self, event: dict, outcome: str, detail: str, now: str) -> bool:
        """Insert the event once. Returns False when the event id was already seen."""
        with self.tx() as c:
            seen = c.execute("SELECT 1 FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
            if seen:
                c.execute(
                    "INSERT INTO event_receipts (event_id, received_at, outcome, detail) VALUES (?,?,?,?)",
                    (event["event_id"], now, "duplicate", "event id already processed; no effect"),
                )
                return False
            c.execute(
                "INSERT INTO events (event_id, event_type, run_id, payload_json, received_at, outcome, detail)"
                " VALUES (?,?,?,?,?,?,?)",
                (event["event_id"], event.get("type", "?"), event.get("run_id"), json.dumps(event), now, outcome, detail),
            )
            c.execute(
                "INSERT INTO event_receipts (event_id, received_at, outcome, detail) VALUES (?,?,?,?)",
                (event["event_id"], now, outcome, detail),
            )
            return True

    def event(self, event_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def set_event_outcome(self, event_id: str, outcome: str, detail: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE events SET outcome=?, detail=? WHERE event_id=?", (outcome, detail, event_id))
            c.execute(
                "UPDATE event_receipts SET outcome=?, detail=? WHERE id=(SELECT MAX(id) FROM event_receipts WHERE event_id=?)",
                (outcome, detail, event_id),
            )

    def event_receipts(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM event_receipts ORDER BY id")]

    # -- invoice commits (outbox) ------------------------------------------

    def commit_record(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM invoice_commits WHERE idempotency_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def commits_for_period(self, client_id: str, billing_period: str) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM invoice_commits WHERE client_id=? AND billing_period=?", (client_id, billing_period)
            )
        ]

    def insert_commit(self, record: dict) -> None:
        with self.tx() as c:
            cols = ",".join(record)
            c.execute(
                f"INSERT INTO invoice_commits ({cols}) VALUES ({','.join('?' * len(record))})", tuple(record.values())
            )

    def update_commit(self, key: str, **fields) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE invoice_commits SET {sets} WHERE idempotency_key=?", (*fields.values(), key))

    # -- execution log ------------------------------------------------------

    def append_log(self, entry: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO execution_log (run_id, step, timestamp, actor, status, project_id, entry_json)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    entry["run_id"], entry["step"], entry["timestamp"], entry["actor"], entry["status"],
                    entry.get("project_id"), json.dumps(entry),
                ),
            )

    def log_entries(self, run_id: str) -> list[dict]:
        return [
            json.loads(r["entry_json"])
            for r in self.conn.execute("SELECT entry_json FROM execution_log WHERE run_id=? ORDER BY id", (run_id,))
        ]
