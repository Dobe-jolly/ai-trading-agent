"""Try a note on one ticket before anyone switches it on ("Try it on this ticket" in the console).

The desk's own agent runs on that open ticket in shadow, with the desk's switched-on notes plus the draft, and the
result is kept next to the decision it re-checks so a person can compare the two. Nothing is written to osTicket,
nothing is e-mailed, no decision is added to the log and nothing is held for approval: the trial row (masked, like
the log) is all that is stored. It costs one ticket's worth of model calls and lookups, so it is rate-limited.

The console runs trials in a background thread with the desk's own builders (its CLI.build_runner in shadow), so
it needs the same Metabase and Claude settings as a pass. tools/demo_console.py swaps in a simulation.
"""
from __future__ import annotations

import importlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .knowledge import Draft, Item, used_ids

log = logging.getLogger(__name__)

TRIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    decision_id INTEGER,
    ticket_number TEXT NOT NULL,
    draft TEXT NOT NULL,               -- JSON of the draft note, masked
    status TEXT NOT NULL,              -- running | done | failed
    finished_at TEXT,
    error TEXT,
    result TEXT                        -- JSON, masked: what the agent would do now
);
CREATE INDEX IF NOT EXISTS idx_trials_decision ON knowledge_trials(decision_id, id);
"""
MAX_RUNNING = 2            # trials at once, per desk
PER_USER_PER_HOUR = 10     # each costs a ticket's worth of model calls
STALE_MINUTES = 20         # a trial still "running" after this was lost (console restarted)


class TrialError(RuntimeError):
    """Why a trial could not run, in words for the person who asked."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


class TrialStore:
    def __init__(self, state_db: Path) -> None:
        self.path = state_db
        with self._connect() as conn:
            conn.executescript(TRIAL_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def create(self, user: str, decision_id: Optional[int], ticket_number: str, draft: Draft) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO knowledge_trials (created_at, created_by, decision_id, ticket_number, draft, status) "
                "VALUES (?, ?, ?, ?, ?, 'running')",
                (now_iso(), user, decision_id, ticket_number, json.dumps(draft.masked().to_json())))
            return int(cursor.lastrowid)

    def finish(self, trial_id: int, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE knowledge_trials SET status = 'done', finished_at = ?, result = ? WHERE id = ?",
                         (now_iso(), json.dumps(result, default=str), trial_id))

    def fail(self, trial_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE knowledge_trials SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                         (now_iso(), error[:1000], trial_id))

    def get(self, trial_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM knowledge_trials WHERE id = ?", (trial_id,)).fetchone()
        return _shape(row) if row else None

    def for_decision(self, decision_id: int, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM knowledge_trials WHERE decision_id = ? ORDER BY id DESC LIMIT ?",
                                (decision_id, limit)).fetchall()
        return [_shape(r) for r in rows]

    def running(self) -> int:
        with self._connect() as conn:
            conn.execute("UPDATE knowledge_trials SET status = 'failed', finished_at = ?, error = ? "
                         "WHERE status = 'running' AND created_at < ?",
                         (now_iso(), "The trial stopped (the console restarted?). Try again.", _ago(STALE_MINUTES)))
            return int(conn.execute("SELECT COUNT(*) FROM knowledge_trials WHERE status = 'running'").fetchone()[0])

    def recent(self, user: str, minutes: int = 60) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM knowledge_trials WHERE created_by = ? AND created_at >= ?",
                                    (user, _ago(minutes))).fetchone()[0])


def _shape(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in ("draft", "result"):
        try:
            out[key] = json.loads(out[key]) if out.get(key) else None
        except (TypeError, ValueError):
            out[key] = None
    return out


Execute = Callable[[str, Item, Optional[dict[str, Any]]], dict[str, Any]]


class TrialRunner:
    """Starts trials for one desk. execute(ticket_number, draft_item, original_decision) -> result; the default
    runs the desk's real agent (run_with_desk)."""

    def __init__(self, desk_key: str, settings: Any, execute: Optional[Execute] = None,
                 max_running: int = MAX_RUNNING, per_user_per_hour: int = PER_USER_PER_HOUR) -> None:
        self.desk_key = desk_key
        self.s = settings
        self.store = TrialStore(settings.state_db)
        self.execute = execute or (lambda number, item, original: run_with_desk(desk_key, settings, number, item))
        self.max_running = max_running
        self.per_user_per_hour = per_user_per_hour
        self._lock = threading.Lock()

    def start(self, user: str, ticket_number: str, draft: Draft, decision_id: Optional[int],
              original: Optional[dict[str, Any]] = None, wait: bool = False) -> dict[str, Any]:
        with self._lock:
            if self.store.recent(user) >= self.per_user_per_hour:
                raise TrialError(f"You've tried {self.per_user_per_hour} notes in the last hour; each one costs a "
                                 "ticket's worth of model calls. Try again a little later.")
            if self.store.running() >= self.max_running:
                raise TrialError("Other trials are still running on this desk. Try again in a minute.")
            trial_id = self.store.create(user, decision_id, ticket_number, draft)
        item = Item.from_draft(draft, user, ticket_number)
        thread = threading.Thread(target=self._work, args=(trial_id, ticket_number, item, original),
                                  name=f"trial-{self.desk_key}-{trial_id}", daemon=True)
        thread.start()
        if wait:
            thread.join()
        return self.store.get(trial_id)  # type: ignore[return-value]

    def _work(self, trial_id: int, ticket_number: str, item: Item, original: Optional[dict[str, Any]]) -> None:
        try:
            result = self.execute(ticket_number, item, original)
        except TrialError as exc:
            self.store.fail(trial_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - shown to the person; details in the service log
            log.exception("Trial %d on #%s failed", trial_id, ticket_number)
            self.store.fail(trial_id, f"The re-check failed: {type(exc).__name__}: {str(exc)[:300]}")
        else:
            self.store.finish(trial_id, result)


def run_with_desk(desk_key: str, settings: Any, ticket_number: str, item: Item) -> dict[str, Any]:
    """The desk's own agent (its CLI's builders, in shadow) on one open ticket with its notes and the draft."""
    from .audit import AuditLog
    from .metabase import MetabaseError

    from .sdk_backend import has_model

    if not getattr(settings, "metabase_api_key", None) or not has_model(settings):
        raise TrialError("The console has no Metabase key or Claude credential (ANTHROPIC_API_KEY or "
                         "CLAUDE_CODE_OAUTH_TOKEN), so it can't re-check tickets. They are read from the same env "
                         "file as the desks.")
    cli = importlib.import_module(f"{desk_key}.__main__").CLI
    mb = cli.build_metabase(settings)
    audit = AuditLog(settings.state_db)
    try:
        return run_trial(cli.build_runner(settings, "shadow", mb, audit), ticket_number, item)
    except MetabaseError as exc:
        raise TrialError(f"A lookup failed while re-checking: {str(exc)[:200]}") from None
    finally:
        audit.close()
        mb.close()


def run_trial(runner: Any, ticket_number: str, item: Item) -> dict[str, Any]:
    """One open ticket through a desk runner's agent and guardrails, with the draft note. Writes nothing: no
    writer call, no decision row, no approval, no e-mail (the runner should be built in shadow anyway)."""
    from .loop import AgentError

    runner.load_knowledge(draft=item)
    tickets = runner.on_request(ticket_number)
    if not tickets:
        raise TrialError(f"Ticket #{ticket_number} isn't open on this desk now, so it can't be re-checked. A note "
                         "applies to open tickets; propose it and it is used from the next pass.")
    ticket = tickets[0]
    ticket.thread_id = runner.store.thread_ids([ticket]).get(ticket.number)
    runner.store.load_thread(ticket)
    runner.toolbox.as_of = None
    files = runner._read_attachments(ticket)
    try:
        result = runner.agent.decide(ticket)
    except AgentError as exc:
        raise TrialError(f"Claude couldn't be reached: {str(exc)[:200]}") from None
    fresh = runner.store.state(ticket)
    guard = runner.guard(ticket, result, fresh, live=False)
    posted = runner._posted_text(ticket, result, guard)
    return describe(runner.agent, result, guard, posted, files)


def describe(agent: Any, result: Any, guard: Any, posted: Optional[str], files: Any = None) -> dict[str, Any]:
    """What the agent would do now (masked), in the shape the console shows next to the original decision."""
    p, f = guard.proposed, guard.final
    mask = result.pseudo
    active = list(getattr(agent, "knowledge_ids", []) or [])
    texts = [p.reason, *(p.evidence or [])]
    draft_used = any("DRAFT" in (t or "") for t in texts) or any(
        isinstance(b, dict) and b.get("type") == "tool_use" and str(b.get("name", "")).startswith("k0_")
        for m in result.transcript for b in (m.get("content") if isinstance(m.get("content"), list) else []))
    return {
        "proposed_action": p.action,
        "final_action": f.action,
        "confidence": p.confidence,
        "category": p.category,
        "reason": mask.mask(p.reason or ""),
        "evidence": mask.mask_obj(list(p.evidence or [])),
        "reply_text": mask.mask(p.reply_text) if p.reply_text else p.reply_text,
        "would_post": result.pseudo.mask(posted) if posted else None,
        "guardrail_notes": list(guard.notes),
        "deferred": bool(guard.deferred),
        "tools_used": list(getattr(result.evidence, "tools_used", None) or []),
        "draft_used": draft_used,
        "knowledge_used": used_ids(result.transcript, texts, active),
        "prompt_version": getattr(agent, "prompt_version", None),
        "input_tokens": result.input_tokens + (getattr(files, "input_tokens", 0) or 0),
        "output_tokens": result.output_tokens + (getattr(files, "output_tokens", 0) or 0),
        "transcript": mask.mask_obj(result.transcript),
    }
