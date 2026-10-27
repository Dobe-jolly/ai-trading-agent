"""The case store: past tickets a desk can read beside a new one (docs/retrieval.md, sections 1 and 1a).

One case per closed ticket, in the desk's state database next to the decisions log. Text is masked one way before it
gets here (agentcore.cases.mask). A case is found by exact tags first (a log signature, a response code, the channel,
the reading of the transaction) and by words second, and newer cases rank higher. Cutoffs an Admin records ("EcoCash
API v3 went live on 2026-09-02") leave out what came before a change.

A case is a hint for the model, never evidence: nothing here is read by a guardrail.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from ..pii import TOKEN_RE

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    desk TEXT NOT NULL,
    source TEXT NOT NULL,              -- agent (the agent closed it) | staff (a person resolved it)
    source_key TEXT NOT NULL UNIQUE,   -- ticket:<ticket_id>
    source_ticket TEXT,                -- ticket number: for the console, never sent to the model
    topic TEXT,
    closed_on TEXT NOT NULL,           -- YYYY-MM-DD
    action TEXT,
    category TEXT,
    asked TEXT NOT NULL,               -- masked one way (agentcore.cases.mask), like found and resolution
    found TEXT,
    resolution TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active | suppressed
    suppressed_reason TEXT,
    suppressed_by TEXT,                -- AUTO (reopened, reviewed incorrect) or the Admin who did it
    suppressed_at TEXT,
    built_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_desk ON cases(desk, status, closed_on);
CREATE TABLE IF NOT EXISTS case_tags (
    case_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                -- signature | code | channel | reading | action
    value TEXT NOT NULL,
    PRIMARY KEY (case_id, kind, value)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_case_tags_value ON case_tags(kind, value);
CREATE TABLE IF NOT EXISTS case_cutoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    desk TEXT NOT NULL,
    kind TEXT,                         -- NULL: every case on the desk; else only cases with this tag
    value TEXT,
    since TEXT NOT NULL,               -- cases closed before this day are left out
    note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS cases_fts USING fts5(asked, found, resolution);
"""

AUTO = "AUTO"
TAG_KINDS = ("signature", "code", "channel", "reading", "action")
TAG_WEIGHTS = {"signature": 3.0, "code": 2.0, "channel": 1.0, "reading": 1.0, "action": 0.5}
TEXT_WEIGHT = 1.5           # the best full-text match counts as much as this
HALF_LIFE_DAYS = 90         # a case's score halves every this many days
MAX_AGE_DAYS = 365          # older cases are never offered
FTS_CANDIDATES = 50
TAG_RE = re.compile(r"^[a-z0-9._/ -]{1,40}$")
WORD_RE = re.compile(r"^[a-z][a-z'&-]{2,29}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_tag(kind: str, value: Any) -> Optional[str]:
    """The stored form of a tag, or None when it can't be one (wrong kind, too long, or anything like an identifier)."""
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip().lower()
    if kind not in TAG_KINDS or not TAG_RE.fullmatch(text) or re.search(r"\d{8,}", text) or TOKEN_RE.search(text):
        return None
    return text


@dataclass
class CaseDraft:
    """A case as the builder made it (agentcore.cases.build). problem: why it must not be offered (reopened, reviewed
    incorrect); a new case with a problem is not stored, a stored one is suppressed."""
    source: str
    ticket_id: int
    source_ticket: Optional[str]
    topic: Optional[str]
    closed_on: str
    asked: str
    found: str = ""
    resolution: str = ""
    action: Optional[str] = None
    category: Optional[str] = None
    tags: set[tuple[str, str]] = field(default_factory=set)
    problem: Optional[str] = None

    @property
    def source_key(self) -> str:
        return f"ticket:{self.ticket_id}"


class CaseStore:
    def __init__(self, db: sqlite3.Connection, desk: str) -> None:
        self.db = db
        self.desk = desk
        db.executescript(SCHEMA)

    # ---- writing ------------------------------------------------------------------------------------------------
    def keys(self) -> dict[str, int]:
        """source_key -> case id, for this desk."""
        return {r[0]: int(r[1]) for r in self.db.execute("SELECT source_key, id FROM cases WHERE desk = ?", (self.desk,))}

    def save(self, draft: CaseDraft) -> str:
        """Add or refresh a case. Returns added | updated | suppressed | skipped. A case an Admin suppressed stays so."""
        row = self.db.execute("SELECT id, status, suppressed_by FROM cases WHERE source_key = ?",
                              (draft.source_key,)).fetchone()
        if row is None and draft.problem:
            return "skipped"
        values = (draft.source, draft.source_ticket, draft.topic, draft.closed_on, draft.action, draft.category,
                  draft.asked, draft.found or None, draft.resolution or None, now_iso())
        if row is None:
            cursor = self.db.execute(
                "INSERT INTO cases (desk, source_key, source, source_ticket, topic, closed_on, action, category, asked, "
                "found, resolution, built_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.desk, draft.source_key) + values)
            case_id, outcome = int(cursor.lastrowid), "added"
        else:
            case_id, outcome = int(row["id"]), "updated"
            self.db.execute("UPDATE cases SET source = ?, source_ticket = ?, topic = ?, closed_on = ?, action = ?, "
                            "category = ?, asked = ?, found = ?, resolution = ?, built_at = ? WHERE id = ?",
                            values + (case_id,))
            if row["suppressed_by"] in (None, AUTO):
                self._set_auto_status(case_id, draft.problem)
                if draft.problem:
                    outcome = "suppressed"
        self.db.execute("DELETE FROM case_tags WHERE case_id = ?", (case_id,))
        self.db.executemany("INSERT OR IGNORE INTO case_tags (case_id, kind, value) VALUES (?, ?, ?)",
                            [(case_id, kind, value) for kind, value in self._clean_tags(draft.tags)])
        self.db.execute("DELETE FROM cases_fts WHERE rowid = ?", (case_id,))
        self.db.execute("INSERT INTO cases_fts (rowid, asked, found, resolution) VALUES (?, ?, ?, ?)",
                        (case_id, draft.asked, draft.found or "", draft.resolution or ""))
        self.db.commit()
        return outcome

    def mark_problem(self, source_key: str, problem: Optional[str]) -> Optional[str]:
        """Re-check a stored case without rebuilding its text. Returns suppressed | restored | None (no change)."""
        row = self.db.execute("SELECT id, status, suppressed_by FROM cases WHERE source_key = ?", (source_key,)).fetchone()
        if row is None or row["suppressed_by"] not in (None, AUTO):
            return None
        before = row["status"]
        self._set_auto_status(int(row["id"]), problem)
        self.db.commit()
        after = "suppressed" if problem else "active"
        return None if before == after else ("suppressed" if problem else "restored")

    def _set_auto_status(self, case_id: int, problem: Optional[str]) -> None:
        if problem:
            self.db.execute("UPDATE cases SET status = 'suppressed', suppressed_reason = ?, suppressed_by = ?, "
                            "suppressed_at = COALESCE(suppressed_at, ?) WHERE id = ?", (problem, AUTO, now_iso(), case_id))
        else:
            self.db.execute("UPDATE cases SET status = 'active', suppressed_reason = NULL, suppressed_by = NULL, "
                            "suppressed_at = NULL WHERE id = ?", (case_id,))

    @staticmethod
    def _clean_tags(tags: Iterable[tuple[str, Any]]) -> list[tuple[str, str]]:
        out = []
        for kind, value in tags:
            cleaned = clean_tag(kind, value)
            if cleaned:
                out.append((kind, cleaned))
        return sorted(set(out))

    def suppress(self, case_id: int, by: str, reason: str) -> None:
        """An Admin takes a case out (wrong, misleading, or a name got through). Rebuilds leave it out."""
        if not by or by == AUTO or not reason.strip():
            raise ValueError("Say who is suppressing the case and why.")
        cursor = self.db.execute("UPDATE cases SET status = 'suppressed', suppressed_reason = ?, suppressed_by = ?, "
                                 "suppressed_at = ? WHERE id = ? AND desk = ?",
                                 (reason.strip()[:300], by, now_iso(), case_id, self.desk))
        if cursor.rowcount == 0:
            raise LookupError(f"No case C{case_id} on this desk.")
        self.db.commit()

    def add_cutoff(self, since: date, note: str, by: str, kind: Optional[str] = None, value: Optional[str] = None) -> int:
        """Leave out cases closed before since: every case on the desk, or only those with the tag kind=value."""
        if not note.strip() or not by:
            raise ValueError("Say what changed and who is recording it.")
        if (kind is None) != (value is None):
            raise ValueError("Give both a tag kind and a value, or neither.")
        tag = clean_tag(kind, value) if kind else None
        if kind and tag is None:
            raise ValueError(f"{kind}={value!r} is not a tag a case can carry.")
        cursor = self.db.execute("INSERT INTO case_cutoffs (desk, kind, value, since, note, created_by, created_at) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (self.desk, kind, tag, since.isoformat(), note.strip()[:300], by, now_iso()))
        self.db.commit()
        return int(cursor.lastrowid)

    def cutoffs(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM case_cutoffs WHERE desk = ? ORDER BY since DESC, id DESC",
                               (self.desk,)).fetchall()

    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT source || ':' || status, COUNT(*) FROM cases WHERE desk = ? GROUP BY 1",
                               (self.desk,)).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    # ---- finding ------------------------------------------------------------------------------------------------
    def search(self, words: Iterable[str] = (), tags: Optional[dict[str, Any]] = None, limit: int = 5,
               today: Optional[date] = None, half_life_days: int = HALF_LIFE_DAYS,
               max_age_days: int = MAX_AGE_DAYS, before: Optional[date] = None,
               exclude_ticket_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Active cases on this desk, best first: weighted tag matches plus the full-text rank, times recency.
        before: only cases closed before this day (replays); exclude_ticket_id: never the ticket being decided."""
        today = today or date.today()
        wanted = self._clean_tags((tags or {}).items())
        words = [w.lower() for w in words if WORD_RE.fullmatch(str(w).lower())]
        matched: dict[int, list[str]] = {}
        tag_score: dict[int, float] = {}
        for kind, value in wanted:
            for (case_id,) in self.db.execute("SELECT case_id FROM case_tags WHERE kind = ? AND value = ?", (kind, value)):
                matched.setdefault(case_id, []).append(kind)
                tag_score[case_id] = tag_score.get(case_id, 0.0) + TAG_WEIGHTS[kind]
        text_score: dict[int, float] = {}
        if words:
            query = " OR ".join(f'"{w}"' for w in words)  # plain words only (WORD_RE), so quoting is enough
            rows = self.db.execute("SELECT rowid, bm25(cases_fts) FROM cases_fts WHERE cases_fts MATCH ? "
                                   "ORDER BY bm25(cases_fts) LIMIT ?", (query, FTS_CANDIDATES)).fetchall()
            best = min((r[1] for r in rows), default=0.0)  # bm25 is negative: lower is better
            for case_id, rank in rows:
                text_score[case_id] = TEXT_WEIGHT * (rank / best if best else 1.0)
        ids = set(tag_score) | set(text_score)
        if not ids:
            return []
        oldest = (today - timedelta(days=max_age_days)).isoformat()
        marks = ", ".join("?" for _ in ids)
        rows = self.db.execute(f"SELECT * FROM cases WHERE id IN ({marks}) AND desk = ? AND status = 'active' "
                               "AND closed_on >= ? AND closed_on < ? AND source_key != ?",
                               (*ids, self.desk, oldest, (before or date.max).isoformat(),
                                f"ticket:{exclude_ticket_id}")).fetchall()
        cut = self._cut_ids([int(r["id"]) for r in rows])
        scored = []
        for row in rows:
            case_id = int(row["id"])
            if case_id in cut:
                continue
            age = max((today - date.fromisoformat(row["closed_on"])).days, 0)
            score = (tag_score.get(case_id, 0.0) + text_score.get(case_id, 0.0)) * 0.5 ** (age / half_life_days)
            kinds = sorted(set(matched.get(case_id, [])), key=TAG_KINDS.index)
            scored.append((score, case_id, row, kinds + (["words"] if case_id in text_score else [])))
        scored.sort(key=lambda s: (-s[0], -s[1]))
        return [self._view(row, kinds, score) for score, _, row, kinds in scored[:limit]]

    def _cut_ids(self, ids: list[int]) -> set[int]:
        """Cases a cutoff leaves out: closed before a desk-wide cutoff, or before a cutoff on a tag they carry."""
        if not ids:
            return set()
        marks = ", ".join("?" for _ in ids)
        rows = self.db.execute(
            f"SELECT c.id FROM cases c JOIN case_cutoffs k ON k.desk = c.desk AND c.closed_on < k.since "
            f"WHERE c.id IN ({marks}) AND (k.kind IS NULL OR EXISTS (SELECT 1 FROM case_tags t WHERE t.case_id = c.id "
            f"AND t.kind = k.kind AND t.value = k.value))", ids).fetchall()
        return {int(r[0]) for r in rows}

    @staticmethod
    def _view(row: sqlite3.Row, matched: list[str], score: float) -> dict[str, Any]:
        """What the model reads: no ticket number, masked text, why it was offered."""
        return {
            "case": f"C{row['id']}",
            "source": row["source"],
            "closed": row["closed_on"],
            "topic": row["topic"],
            "matched": matched,
            "what_was_asked": row["asked"],
            "what_was_found": row["found"] or "",
            "how_it_was_resolved": row["resolution"] or "",
            "outcome": ("closed by the agent, not reopened" if row["source"] == "agent"
                        else "resolved by staff, not reopened"),
            "score": round(score, 3),
        }
