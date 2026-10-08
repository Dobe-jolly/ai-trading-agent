"""Approval of replies the agent reached off the standard path.

APPROVAL_MODE decides which replies wait for a person in the console before they are posted (live mode):
- off: none; the agent acts on its own (the rules in guardrails.py still apply).
- unscripted (default): replies that are not the agent's standard outcome. That is when the model says
  it reasoned beyond the standard outcome, or when its action differs from the one the standard outcome
  gives (each agent defines its standard outcome: the SMS check statuses, the EcoCash standard reply).
- all: every reply and close.

Escalations are internal notes and never wait. A waiting reply is stored with the real values it needs
(the text, and the token map so an edited reply can be filled in); both are erased once it is posted,
rejected or dropped, as the e-mail outbox does with message bodies.

The agent carries out the verdict on its next pass: an approved reply is posted (and the ticket closed
when that was the action) if the ticket has not changed; a rejected one becomes an escalation note with
the reviewer's comment. If the ticket changes before anyone decides, the waiting reply is dropped and
the agent decides again.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .decision import REPLY_ACTIONS, REPLY_AND_CLOSE, REQUEST_INFO, Decision

MODES = ("off", "unscripted", "all")
OPEN_STATES = ("pending", "approved", "rejected")

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    ticket_id INTEGER NOT NULL,
    ticket_number TEXT,
    last_entry_id INTEGER,
    action TEXT NOT NULL,
    why TEXT,
    reply_masked TEXT,
    reply_text TEXT,
    token_map TEXT,
    status TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    comment TEXT,
    edited INTEGER NOT NULL DEFAULT 0,
    finished_at TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_ticket ON approvals(ticket_id, id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def approval_reason(setting: str, decision: Decision, expected: Optional[str], *,
                    beyond: str = "the agent reasoned beyond the standard outcome",
                    leads: str = "the standard outcome is") -> Optional[str]:
    """Why this (guarded, final) decision must wait for a person, or None. expected is the action the agent's
    standard outcome gives; beyond and leads word the reasons for this agent."""
    if setting == "off" or decision.action not in REPLY_ACTIONS:
        return None
    if setting == "all":
        return "every reply waits for approval (APPROVAL_MODE=all)"
    reasons = []
    if decision.standard is False:
        reasons.append(beyond)
    if expected and expected != decision.action:
        reasons.append(f"{leads} {expected.replace('_', ' ')}, not {decision.action.replace('_', ' ')}")
    return "; ".join(reasons) or None


class ApprovalStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        db.executescript(SCHEMA)
        db.commit()

    def create(self, *, decision_id: int, ticket_id: int, ticket_number: str, last_entry_id: Optional[int],
               action: str, why: str, reply_masked: str, reply_text: str, token_map: dict[str, str]) -> int:
        cur = self.db.execute(
            "INSERT INTO approvals (decision_id, created_at, ticket_id, ticket_number, last_entry_id, action, why, "
            "reply_masked, reply_text, token_map, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (decision_id, now_iso(), ticket_id, ticket_number, last_entry_id, action, why, reply_masked, reply_text,
             json.dumps(token_map)))
        self.db.commit()
        return int(cur.lastrowid)

    def open_for(self, ticket_id: int) -> Optional[sqlite3.Row]:
        return self.db.execute(
            f"SELECT * FROM approvals WHERE ticket_id = ? AND status IN ({','.join('?' * len(OPEN_STATES))}) "
            "ORDER BY id DESC LIMIT 1", (ticket_id, *OPEN_STATES)).fetchone()

    def get(self, approval_id: int) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()

    def decide(self, approval_id: int, verdict: str, user: str, comment: Optional[str],
               edited_reply: Optional[str] = None) -> sqlite3.Row:
        """approve / reject a pending reply. Raises ValueError when it is no longer pending."""
        row = self.get(approval_id)
        if row is None:
            raise LookupError("No approval with that id.")
        if row["status"] != "pending":
            raise ValueError(f"This reply is already {row['status']}.")
        status = {"approve": "approved", "reject": "rejected"}[verdict]
        edited = edited_reply is not None and edited_reply.strip() != (row["reply_masked"] or "").strip()
        self.db.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, comment = ?, edited = ?, "
            "reply_masked = ? WHERE id = ? AND status = 'pending'",
            (status, user, now_iso(), (comment or "").strip()[:1000] or None, int(edited),
             edited_reply.strip() if edited else row["reply_masked"], approval_id))
        self.db.commit()
        return self.get(approval_id)

    def finish(self, approval_id: int, status: str, result: str) -> None:
        """Close it for good and erase the real values."""
        self.db.execute("UPDATE approvals SET status = ?, finished_at = ?, result = ?, reply_text = NULL, "
                        "token_map = NULL WHERE id = ?", (status, now_iso(), result[:500], approval_id))
        self.db.commit()

    def drop_missing(self, open_ticket_ids: Iterable[int]) -> int:
        """Waiting replies on tickets that are no longer open (closed by a person) are dropped."""
        ids = set(open_ticket_ids)
        rows = self.db.execute(
            f"SELECT id, ticket_id FROM approvals WHERE status IN ({','.join('?' * len(OPEN_STATES))})",
            OPEN_STATES).fetchall()
        dropped = 0
        for row in rows:
            if row["ticket_id"] not in ids:
                self.finish(row["id"], "dropped", "the ticket is no longer open")
                dropped += 1
        return dropped

    def pending_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0])


def token_map(row: sqlite3.Row) -> dict[str, str]:
    try:
        return json.loads(row["token_map"] or "{}")
    except (TypeError, ValueError):
        return {}


def summary(row: sqlite3.Row) -> dict[str, Any]:
    """For the console: never the real text or the token map."""
    keys = ("id", "decision_id", "created_at", "ticket_id", "ticket_number", "action", "why", "reply_masked", "status",
            "decided_by", "decided_at", "comment", "edited", "finished_at", "result")
    return {k: row[k] for k in keys}


__all__ = ["ApprovalStore", "MODES", "approval_reason", "summary", "token_map"]
