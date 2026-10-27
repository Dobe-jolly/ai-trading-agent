"""Local SQLite audit log of every decision. Text is stored masked (no customer numbers)."""
from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    model TEXT NOT NULL,
    tickets_seen INTEGER DEFAULT 0,
    decisions INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    ticket_id INTEGER NOT NULL,
    ticket_number TEXT,
    last_entry_id INTEGER,
    last_message_id INTEGER,
    proposed_action TEXT,
    final_action TEXT,
    confidence REAL,
    category TEXT,
    reason TEXT,
    evidence TEXT,
    account_statuses TEXT,
    reply_text TEXT,
    template_reply TEXT,
    actual_reply TEXT,
    guardrail_notes TEXT,
    tools_used TEXT,
    executed TEXT,
    deferred INTEGER DEFAULT 0,
    error TEXT,
    transcript TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    facts TEXT,
    branch TEXT,
    attachments TEXT,
    approval TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ticket ON decisions(ticket_id, id);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    verdict TEXT NOT NULL,
    expected_action TEXT,
    comment TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_decision ON reviews(decision_id, id);
CREATE TABLE IF NOT EXISTS control_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS live (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    run_id TEXT,
    state TEXT,
    started_at TEXT,
    updated_at TEXT,
    ticket_number TEXT,
    help_topic TEXT,
    steps TEXT
);
CREATE TABLE IF NOT EXISTS queue_state (
    ticket_id INTEGER PRIMARY KEY,
    ticket_number TEXT,
    help_topic TEXT,
    opened TEXT,
    run_id TEXT,
    updated_at TEXT,
    state TEXT,
    detail TEXT,
    decision_id INTEGER
);
"""

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    decision_id INTEGER,
    ticket_id INTEGER,
    ticket_number TEXT,
    kind TEXT NOT NULL,
    channel TEXT NOT NULL,
    recipient TEXT,
    cc TEXT,
    subject TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    sent_at TEXT,
    UNIQUE (decision_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status, id);
"""
SCHEMA += OUTBOX_SCHEMA

EXPORT_COLUMNS = [
    "created_at", "mode", "ticket_number", "ticket_id", "proposed_action", "final_action", "confidence",
    "category", "account_statuses", "reason", "evidence", "reply_text", "template_reply", "actual_reply",
    "guardrail_notes", "tools_used", "executed", "deferred", "error", "input_tokens", "output_tokens",
]
JSON_COLUMNS = ("evidence", "account_statuses", "guardrail_notes", "tools_used", "executed", "transcript", "facts",
                "attachments", "knowledge_active", "knowledge_used", "cases_seen", "cases_used")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuditLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=15, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # WAL lets the portal read while a run is writing.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=15000")
        self._db.executescript(SCHEMA)
        from .approval import SCHEMA as APPROVAL_SCHEMA  # the console reads each held reply's approval

        self._db.executescript(APPROVAL_SCHEMA)
        # Columns added after a log may already exist.
        have = {r[1] for r in self._db.execute("PRAGMA table_info(decisions)").fetchall()}
        # prompt_version: hash of the system prompt (with staff notes) and tools that produced the decision;
        # knowledge_active / knowledge_used: the notes that were on, and the ones it used (agentcore.knowledge);
        # reopened_at / outcome_checked_at: whether a ticket the agent closed was reopened (the quality signal);
        # cases_seen / cases_used: past cases find_past_cases showed, and the ones the decision cited (agentcore.cases).
        for column in ("facts", "branch", "attachments", "approval", "prompt_version", "knowledge_active",
                       "knowledge_used", "reopened_at", "outcome_checked_at", "cases_seen", "cases_used"):
            if column not in have:
                self._db.execute(f"ALTER TABLE decisions ADD COLUMN {column} TEXT")
        self._db.commit()

    @property
    def db(self) -> sqlite3.Connection:
        return self._db

    def close(self) -> None:
        self._db.close()

    def start_run(self, mode: str, model: str) -> str:
        run_id = uuid.uuid4().hex[:12]
        self._db.execute("INSERT INTO runs (run_id, started_at, mode, model) VALUES (?, ?, ?, ?)",
                         (run_id, now_iso(), mode, model))
        self._db.commit()
        return run_id

    def finish_run(self, run_id: str, seen: int, decided: int, errors: int) -> None:
        self._db.execute("UPDATE runs SET finished_at = ?, tickets_seen = ?, decisions = ?, errors = ? WHERE run_id = ?",
                         (now_iso(), seen, decided, errors, run_id))
        self._db.commit()

    def record(self, **row: Any) -> int:
        for key in JSON_COLUMNS:
            if key in row and row[key] is not None and not isinstance(row[key], str):
                row[key] = json.dumps(row[key], default=str, ensure_ascii=False)
        row.setdefault("created_at", now_iso())
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cursor = self._db.execute(f"INSERT INTO decisions ({columns}) VALUES ({marks})", tuple(row.values()))
        self._db.commit()
        return int(cursor.lastrowid)

    # ---- live progress (what the agent is doing right now; masked text only) --------------
    def live_start(self, run_id: str) -> None:
        now = now_iso()
        self._db.execute("INSERT OR REPLACE INTO live (id, run_id, state, started_at, updated_at, ticket_number, help_topic, "
                         "steps) VALUES (1, ?, 'running', ?, ?, NULL, NULL, '[]')", (run_id, now, now))
        self._db.commit()

    def live_ticket(self, number: str, help_topic: str) -> None:
        self._db.execute("UPDATE live SET ticket_number = ?, help_topic = ?, steps = '[]', updated_at = ? WHERE id = 1",
                         (number, help_topic, now_iso()))
        self._db.commit()

    def live_step(self, text: str) -> None:
        row = self._db.execute("SELECT steps FROM live WHERE id = 1").fetchone()
        if row is None:
            return
        steps = json.loads(row["steps"] or "[]")[-14:] + [{"at": now_iso(), "text": text[:300]}]
        self._db.execute("UPDATE live SET steps = ?, updated_at = ? WHERE id = 1", (json.dumps(steps), now_iso()))
        self._db.commit()

    def live_end(self) -> None:
        self._db.execute("UPDATE live SET state = 'idle', ticket_number = NULL, help_topic = NULL, updated_at = ? "
                         "WHERE id = 1", (now_iso(),))
        self._db.commit()

    # ---- live queue (what the last full run saw) ------------------------------------
    def replace_queue(self, run_id: str, items: list[dict[str, Any]]) -> None:
        now = now_iso()
        with self._db:
            self._db.execute("DELETE FROM queue_state")
            self._db.executemany(
                "INSERT INTO queue_state (ticket_id, ticket_number, help_topic, opened, run_id, updated_at, state, detail, "
                "decision_id) VALUES (:ticket_id, :ticket_number, :help_topic, :opened, :run_id, :updated_at, :state, "
                ":detail, :decision_id)",
                [{**{"detail": None, "decision_id": None}, **item, "run_id": run_id, "updated_at": now} for item in items],
            )

    # ---- reviews and operator events -----------------------------------------------
    def add_review(self, decision_id: int, reviewer: str, verdict: str,
                   expected_action: Optional[str], comment: Optional[str]) -> dict[str, Any]:
        created = now_iso()
        cursor = self._db.execute(
            "INSERT INTO reviews (decision_id, created_at, reviewer, verdict, expected_action, comment) VALUES (?, ?, ?, ?, ?, ?)",
            (decision_id, created, reviewer, verdict, expected_action, comment))
        self._db.commit()
        return {"id": cursor.lastrowid, "decision_id": decision_id, "created_at": created, "reviewer": reviewer,
                "verdict": verdict, "expected_action": expected_action, "comment": comment}

    def add_control_event(self, actor: str, action: str, detail: str = "") -> None:
        self._db.execute("INSERT INTO control_events (created_at, actor, action, detail) VALUES (?, ?, ?, ?)",
                         (now_iso(), actor, action, detail))
        self._db.commit()

    def last_for_ticket(self, ticket_id: int, mode: Optional[str] = None) -> Optional[sqlite3.Row]:
        """Latest real decision: not a deferral, and not a failure that changed nothing."""
        sql = ("SELECT * FROM decisions WHERE ticket_id = ? AND deferred = 0 "
               "AND (error IS NULL OR (executed IS NOT NULL AND executed != '[]'))")
        params: list[Any] = [ticket_id]
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        return self._db.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()

    def latest_per_ticket(self, since: str, mode: str) -> list[sqlite3.Row]:
        return list(self._db.execute(
            "SELECT d.* FROM decisions d JOIN (SELECT ticket_id, MAX(id) AS id FROM decisions "
            "WHERE created_at >= ? AND mode = ? AND error IS NULL AND deferred = 0 GROUP BY ticket_id) last "
            "ON last.id = d.id ORDER BY d.id", (since, mode)).fetchall())

    def export_csv(self, out: Path, since: str, mode: Optional[str] = None) -> int:
        sql = f"SELECT {', '.join(EXPORT_COLUMNS)} FROM decisions WHERE created_at >= ?"
        params: list[Any] = [since]
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        rows = self._db.execute(sql + " ORDER BY id", params).fetchall()
        with out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(EXPORT_COLUMNS)
            writer.writerows([tuple(r) for r in rows])
        return len(rows)


def is_stale(created_at: str, hours: float) -> bool:
    try:
        when = datetime.fromisoformat(created_at)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - when > timedelta(hours=hours)
