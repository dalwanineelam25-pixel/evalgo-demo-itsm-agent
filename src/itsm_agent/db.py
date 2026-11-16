"""SQLite persistence for incidents, work notes, diagnostic runs, confirmations and audit."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    role        TEXT NOT NULL,
    groups_json TEXT NOT NULL DEFAULT '[]',
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS incidents (
    number                      TEXT PRIMARY KEY,
    caller_id                   TEXT NOT NULL REFERENCES users(user_id),
    short_description           TEXT NOT NULL,
    description                 TEXT NOT NULL DEFAULT '',
    category                    TEXT NOT NULL,
    subcategory                 TEXT,
    priority                    INTEGER NOT NULL,
    state                       TEXT NOT NULL,
    hold_reason                 TEXT,
    assignment_group            TEXT NOT NULL,
    assigned_to                 TEXT,
    cmdb_ci                     TEXT,
    requires_caller_confirmation INTEGER NOT NULL DEFAULT 1,
    escalated                   INTEGER NOT NULL DEFAULT 0,
    resolution_code             TEXT,
    resolution_notes            TEXT,
    resolution_evidence_run_id  TEXT,
    resolved_by                 TEXT,
    resolved_at                 TEXT,
    closed_by                   TEXT,
    closed_at                   TEXT,
    opened_at                   TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_number TEXT NOT NULL REFERENCES incidents(number),
    author_id       TEXT NOT NULL,
    note_type       TEXT NOT NULL,          -- agent | system | troubleshooting | human
    body            TEXT NOT NULL,
    kb_article_id   TEXT,
    outcome         TEXT,                   -- troubleshooting only: successful | unsuccessful | inconclusive
    thread_id       TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnostic_runs (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL UNIQUE,
    incident_number TEXT NOT NULL REFERENCES incidents(number),
    device_id       TEXT NOT NULL,
    requested_by    TEXT NOT NULL,
    outcome         TEXT NOT NULL,          -- pass | fail | timeout | error
    error_code      TEXT,
    summary         TEXT NOT NULL,
    checks_json     TEXT NOT NULL DEFAULT '[]',
    raw_response    TEXT,
    service_run_id  TEXT,
    attempts        INTEGER NOT NULL,
    duration_ms     INTEGER NOT NULL,
    thread_id       TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS caller_confirmations (
    confirmation_id TEXT PRIMARY KEY,
    incident_number TEXT NOT NULL REFERENCES incidents(number),
    requested_by    TEXT NOT NULL,
    question        TEXT NOT NULL,
    evidence_run_id TEXT,
    status          TEXT NOT NULL,          -- pending | confirmed | declined
    responded_by    TEXT,
    comment         TEXT,
    thread_id       TEXT,
    requested_at    TEXT NOT NULL,
    responded_at    TEXT
);

CREATE TABLE IF NOT EXISTS escalations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_number TEXT NOT NULL REFERENCES incidents(number),
    from_group      TEXT NOT NULL,
    to_group        TEXT NOT NULL,
    reason          TEXT NOT NULL,
    reason_codes    TEXT NOT NULL DEFAULT '[]',
    escalated_by    TEXT NOT NULL,
    thread_id       TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_number TEXT NOT NULL,
    actor_id        TEXT NOT NULL,
    via             TEXT NOT NULL,
    thread_id       TEXT,
    field           TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    thread_id   TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    incident_number TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_incident ON work_notes(incident_number);
CREATE INDEX IF NOT EXISTS idx_runs_incident ON diagnostic_runs(incident_number);
CREATE INDEX IF NOT EXISTS idx_audit_incident ON incident_audit(incident_number);
"""

AGENT_VIA = "itsm-agent"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class NotFound(LookupError):
    pass


class Repository:
    """Thin data-access layer. Opens a short-lived connection per unit of work (thread-safe)."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Unit of work. ``immediate=True`` takes the write lock up front (read-then-write sequences)."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        try:
            with conn:  # commits on success, rolls back on exception
                yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ users
    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = _row(conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone())
        if row:
            row["groups"] = json.loads(row.pop("groups_json"))
            row["active"] = bool(row["active"])
        return row

    # -------------------------------------------------------------- incidents
    def get_incident(self, number: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return _row(conn.execute("SELECT * FROM incidents WHERE number = ?", (number,)).fetchone())

    def require_incident(self, number: str) -> dict[str, Any]:
        inc = self.get_incident(number)
        if inc is None:
            raise NotFound(f"Incident {number} not found")
        return inc

    def update_incident_fields(
        self,
        number: str,
        changes: dict[str, Any],
        *,
        actor_id: str,
        thread_id: str | None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Update incident columns and write one audit row per changed field, in one transaction."""
        if conn is None:
            with self.connect(immediate=True) as c:
                return self.update_incident_fields(
                    number, changes, actor_id=actor_id, thread_id=thread_id, conn=c
                )
        current = _row(conn.execute("SELECT * FROM incidents WHERE number = ?", (number,)).fetchone())
        if current is None:
            raise NotFound(f"Incident {number} not found")
        now = utcnow()
        changed = {k: v for k, v in changes.items() if current.get(k) != v}
        if not changed:
            return current
        cols = ", ".join(f"{k} = ?" for k in changed)
        conn.execute(
            f"UPDATE incidents SET {cols}, updated_at = ? WHERE number = ?",
            (*changed.values(), now, number),
        )
        for field_name, new_value in changed.items():
            conn.execute(
                "INSERT INTO incident_audit (incident_number, actor_id, via, thread_id, field, old_value, new_value, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    number,
                    actor_id,
                    AGENT_VIA,
                    thread_id,
                    field_name,
                    None if current.get(field_name) is None else str(current.get(field_name)),
                    None if new_value is None else str(new_value),
                    now,
                ),
            )
        current.update(changed)
        current["updated_at"] = now
        return current

    # ------------------------------------------------------------- work notes
    def add_work_note(
        self,
        number: str,
        *,
        author_id: str,
        note_type: str,
        body: str,
        thread_id: str | None,
        kb_article_id: str | None = None,
        outcome: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        sql = (
            "INSERT INTO work_notes (incident_number, author_id, note_type, body, kb_article_id, outcome, thread_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        params = (number, author_id, note_type, body, kb_article_id, outcome, thread_id, utcnow())
        if conn is not None:
            return conn.execute(sql, params).lastrowid
        with self.connect() as c:
            return c.execute(sql, params).lastrowid

    def list_work_notes(self, number: str, limit: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM work_notes WHERE incident_number = ? ORDER BY id", (number,)
            ).fetchall()
        notes = [dict(r) for r in rows]
        return notes[-limit:] if limit else notes

    def count_troubleshooting(self, number: str, outcome: str) -> int:
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM work_notes WHERE incident_number = ? AND note_type = 'troubleshooting' AND outcome = ?",
                (number, outcome),
            ).fetchone()[0]

    # -------------------------------------------------------- diagnostic runs
    def add_diagnostic_run(self, run: dict[str, Any], conn: sqlite3.Connection) -> str:
        """Insert a run; run ids are sequential per incident, e.g. DR-INC-1001-002."""
        n = conn.execute(
            "SELECT COUNT(*) FROM diagnostic_runs WHERE incident_number = ?", (run["incident_number"],)
        ).fetchone()[0]
        run_id = f"DR-{run['incident_number']}-{n + 1:03d}"
        sql = (
            "INSERT INTO diagnostic_runs (run_id, incident_number, device_id, requested_by, outcome, error_code, summary,"
            " checks_json, raw_response, service_run_id, attempts, duration_ms, thread_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        params = (
            run_id,
            run["incident_number"],
            run["device_id"],
            run["requested_by"],
            run["outcome"],
            run.get("error_code"),
            run["summary"],
            json.dumps(run.get("checks", [])),
            run.get("raw_response"),
            run.get("service_run_id"),
            run["attempts"],
            run["duration_ms"],
            run.get("thread_id"),
            utcnow(),
        )
        conn.execute(sql, params)
        return run_id

    def list_diagnostic_runs(self, number: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM diagnostic_runs WHERE incident_number = ? ORDER BY seq", (number,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["checks"] = json.loads(d.pop("checks_json") or "[]")
            out.append(d)
        return out

    # ---------------------------------------------------------- confirmations
    def create_confirmation(
        self,
        number: str,
        *,
        requested_by: str,
        question: str,
        evidence_run_id: str | None,
        thread_id: str | None,
    ) -> str:
        cid = f"CONF-{uuid.uuid4().hex[:10].upper()}"
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO caller_confirmations (confirmation_id, incident_number, requested_by, question, evidence_run_id,"
                " status, thread_id, requested_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (cid, number, requested_by, question, evidence_run_id, thread_id, utcnow()),
            )
        return cid

    def get_confirmation(self, confirmation_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return _row(
                conn.execute(
                    "SELECT * FROM caller_confirmations WHERE confirmation_id = ?", (confirmation_id,)
                ).fetchone()
            )

    def list_confirmations(self, number: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM caller_confirmations WHERE incident_number = ? ORDER BY requested_at",
                (number,),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_confirmation_response(
        self, confirmation_id: str, *, confirmed: bool, responded_by: str, comment: str | None
    ) -> dict[str, Any]:
        with self.connect() as conn:
            row = _row(
                conn.execute(
                    "SELECT * FROM caller_confirmations WHERE confirmation_id = ?", (confirmation_id,)
                ).fetchone()
            )
            if row is None:
                raise NotFound(f"Confirmation {confirmation_id} not found")
            if row["status"] != "pending":
                return row  # idempotent: already answered
            status = "confirmed" if confirmed else "declined"
            now = utcnow()
            conn.execute(
                "UPDATE caller_confirmations SET status = ?, responded_by = ?, comment = ?, responded_at = ?"
                " WHERE confirmation_id = ?",
                (status, responded_by, comment, now, confirmation_id),
            )
            row.update(status=status, responded_by=responded_by, comment=comment, responded_at=now)
            return row

    # ------------------------------------------------------------ escalations
    def add_escalation(
        self,
        number: str,
        *,
        from_group: str,
        to_group: str,
        reason: str,
        reason_codes: list[str],
        escalated_by: str,
        thread_id: str | None,
        conn: sqlite3.Connection,
    ) -> int:
        return conn.execute(
            "INSERT INTO escalations (incident_number, from_group, to_group, reason, reason_codes, escalated_by, thread_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (number, from_group, to_group, reason, json.dumps(reason_codes), escalated_by, thread_id, utcnow()),
        ).lastrowid

    def list_escalations(self, number: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM escalations WHERE incident_number = ? ORDER BY id", (number,)
            ).fetchall()
        return [dict(r) for r in rows]

    def list_audit(self, number: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM incident_audit WHERE incident_number = ? ORDER BY id", (number,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- conversations
    def create_conversation(self, thread_id: str, user_id: str, incident_number: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO conversations (thread_id, user_id, incident_number, created_at) VALUES (?, ?, ?, ?)",
                (thread_id, user_id, incident_number, utcnow()),
            )

    def get_conversation(self, thread_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return _row(
                conn.execute("SELECT * FROM conversations WHERE thread_id = ?", (thread_id,)).fetchone()
            )

    # ------------------------------------------------------------- inspection
    def incident_snapshot(self, number: str) -> dict[str, Any]:
        """Full record for inspection (CLI `show`, API, samples)."""
        return {
            "incident": self.require_incident(number),
            "work_notes": self.list_work_notes(number),
            "diagnostic_runs": self.list_diagnostic_runs(number),
            "caller_confirmations": self.list_confirmations(number),
            "escalations": self.list_escalations(number),
            "audit": self.list_audit(number),
        }
