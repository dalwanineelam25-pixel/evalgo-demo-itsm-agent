"""Load deterministic fictional seed data into the incident database."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .db import Repository

log = logging.getLogger(__name__)

TABLES_IN_DELETE_ORDER = (
    "incident_audit",
    "escalations",
    "caller_confirmations",
    "diagnostic_runs",
    "work_notes",
    "conversations",
    "incidents",
    "users",
)


def seed_database(repo: Repository, seed_path: Path, *, reset: bool = True) -> dict[str, int]:
    data = json.loads(Path(seed_path).read_text())
    repo.init_schema()
    with repo.connect() as conn:
        if reset:
            for table in TABLES_IN_DELETE_ORDER:
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM sqlite_sequence")
        for u in data["users"]:
            conn.execute(
                "INSERT OR REPLACE INTO users (user_id, name, email, role, groups_json, active) VALUES (?, ?, ?, ?, ?, ?)",
                (u["user_id"], u["name"], u["email"], u["role"], json.dumps(u.get("groups", [])), int(u.get("active", True))),
            )
        for inc in data["incidents"]:
            row = {
                "subcategory": None,
                "hold_reason": None,
                "assigned_to": None,
                "requires_caller_confirmation": 1,
                "escalated": 0,
                "resolution_code": None,
                "resolution_notes": None,
                "resolution_evidence_run_id": None,
                "resolved_by": None,
                "resolved_at": None,
                "closed_by": None,
                "closed_at": None,
                **inc,
            }
            row.setdefault("updated_at", row["opened_at"])
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            conn.execute(f"INSERT OR REPLACE INTO incidents ({cols}) VALUES ({marks})", tuple(row.values()))
        for n in data.get("work_notes", []):
            conn.execute(
                "INSERT INTO work_notes (incident_number, author_id, note_type, body, kb_article_id, outcome, thread_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                (n["incident_number"], n["author_id"], n["note_type"], n["body"], n.get("kb_article_id"), n.get("outcome"), n["created_at"]),
            )
        for e in data.get("escalations", []):
            conn.execute(
                "INSERT INTO escalations (incident_number, from_group, to_group, reason, reason_codes, escalated_by, thread_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                (e["incident_number"], e["from_group"], e["to_group"], e["reason"], json.dumps(e.get("reason_codes", [])), e["escalated_by"], e["created_at"]),
            )
    counts = {
        "users": len(data["users"]),
        "incidents": len(data["incidents"]),
        "work_notes": len(data.get("work_notes", [])),
        "escalations": len(data.get("escalations", [])),
    }
    log.info("seed loaded db=%s %s", repo.db_path, counts)
    return counts
