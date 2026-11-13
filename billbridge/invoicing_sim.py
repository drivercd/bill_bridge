"""Simulated enterprise Invoicing System (the external receiver).

It keeps its own SQLite file, separate from BillBridge's state, so tests can
count what the receiving system actually created. It behaves like an API that
honours an Idempotency-Key header:

* same key + same payload hash  -> returns the invoice already created (replay)
* same key + different payload  -> 409 conflict, nothing created
* new key                       -> creates one draft invoice

Faults can be injected to reproduce the M3 recovery conditions:

* "unavailable"   503 before anything is written (safe to retry)
* "lost_ack"      the invoice IS written, then the caller sees a 504 timeout
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from .state import utc_now


class InvoicingUnavailable(Exception):
    """503 / connection refused: the request was not processed."""


class InvoicingTimeout(Exception):
    """504 / socket drop: the request may or may not have been processed."""


class InvoicingConflict(Exception):
    """409: the idempotency key was already used with a different payload."""


class SimulatedInvoicingSystem:
    def __init__(self, path: Path, faults: list[str] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS invoices (
                invoice_id      TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE NOT NULL,
                payload_hash    TEXT NOT NULL,
                client_id       TEXT NOT NULL,
                billing_period  TEXT NOT NULL,
                status          TEXT NOT NULL,   -- draft (awaiting AR manager sign-off)
                total_hours     REAL NOT NULL,
                lines_json      TEXT NOT NULL,
                created_at      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS requests (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT,
                kind            TEXT,
                result          TEXT,
                at              TEXT
            );
            """
        )
        self.faults = list(faults or [])

    def close(self) -> None:
        self.conn.close()

    def _log(self, key: str, kind: str, result: str) -> None:
        self.conn.execute(
            "INSERT INTO requests (idempotency_key, kind, result, at) VALUES (?,?,?,?)", (key, kind, result, utc_now())
        )

    # -- API ---------------------------------------------------------------

    def create_draft_invoice(self, idempotency_key: str, payload_hash: str, payload: dict) -> dict:
        fault = self.faults.pop(0) if self.faults else None
        if fault == "unavailable":
            self._log(idempotency_key, "create", "503 unavailable")
            raise InvoicingUnavailable("503 Service Unavailable")

        existing = self.conn.execute(
            "SELECT * FROM invoices WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                self._log(idempotency_key, "create", "409 conflict")
                raise InvoicingConflict("idempotency key reused with a different payload")
            self._log(idempotency_key, "create", "200 replay")
            return {**self._row(existing), "replayed": True}

        invoice_id = f"INV-{uuid.uuid4().hex[:8].upper()}"
        lines = payload["lines"]
        self.conn.execute(
            "INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?,?)",
            (
                invoice_id, idempotency_key, payload_hash, payload["client_id"], payload["billing_period"], "draft",
                round(sum(line["billable_hours"] for line in lines), 2), json.dumps(lines), utc_now(),
            ),
        )
        if fault == "lost_ack":
            self._log(idempotency_key, "create", "201 created, ack lost (504)")
            raise InvoicingTimeout("504 Gateway Timeout")
        self._log(idempotency_key, "create", "201 created")
        row = self.conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
        return {**self._row(row), "replayed": False}

    def find_by_key(self, idempotency_key: str) -> dict | None:
        fault = self.faults[0] if self.faults else None
        if fault == "unavailable":
            self.faults.pop(0)
            self._log(idempotency_key, "lookup", "503 unavailable")
            raise InvoicingUnavailable("503 Service Unavailable")
        row = self.conn.execute("SELECT * FROM invoices WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        self._log(idempotency_key, "lookup", "found" if row else "not found")
        return self._row(row) if row else None

    # -- inspection (what a test or auditor checks) ------------------------

    def invoices(self) -> list[dict]:
        return [self._row(r) for r in self.conn.execute("SELECT * FROM invoices ORDER BY created_at")]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]

    def requests(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM requests ORDER BY id")]

    @staticmethod
    def _row(row) -> dict:
        data = dict(row)
        data["lines"] = json.loads(data.pop("lines_json"))
        return data
