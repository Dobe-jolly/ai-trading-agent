"""What ICT staff teach a desk, from the ticket review step in the console.

A note is written by a person: the logic for a kind of ticket, what a code means, how to read a log, which table
holds what. It can carry a lookup: a read-only query the person wrote, with {{slots}} for values from the ticket.
Anyone signed in may propose one (and try it on the ticket first: see agentcore.trial); an Admin who did not write
it switches it on. Only then does the desk use it on future tickets.

How a switched-on note reaches the model (Claude proposes, code decides):
- The note goes in the system prompt, after the desk's own prompt, with its id (K12) so decisions can cite it.
- A lookup becomes a tool named k<id>_<title>. The model supplies only values: masked tokens it was given on this
  ticket (<NUMBER_1>, <MOBILE_2>), dates, short codes, amounts. Metabase binds them into the query (template tags),
  so model text never becomes SQL. Rows are capped and masked before the model sees them.
- Nothing here can loosen the desk's guardrails, its approvals or its modes: they stay in code.

Every decision records which notes were on and which it used (decisions.knowledge_active / knowledge_used), and
the prompt version that produced it, so a bad decision can be traced to a note and that note switched off.

Text is masked when it is saved (account, card and phone numbers become tokens), like everything in the log.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from .pii import TOKEN_RE, Pseudonymizer

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL,              -- proposed | active | rejected | retired | withdrawn
    title TEXT NOT NULL,
    body TEXT NOT NULL,                -- masked
    example TEXT,                      -- masked: log lines or rows pasted with the note
    topic TEXT,                        -- help topic (and its subtopics) it applies to; NULL for every ticket
    query_db TEXT,                     -- Metabase database name
    query_sql TEXT,
    query_slots TEXT,                  -- JSON {slot: kind}
    query_about TEXT,                  -- what the lookup returns and when to use it (the tool's description)
    tool_name TEXT,
    source_decision_id INTEGER,
    source_ticket TEXT,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    retired_by TEXT,
    retired_at TEXT,
    retire_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_knowledge_status ON knowledge(status, id);
"""

STATUSES = ("proposed", "active", "rejected", "retired", "withdrawn")
# What a slot may hold, and how the model must supply it. Identifiers only as tokens issued on this ticket
# (provenance: the model cannot look up a customer who is not on the ticket or in an earlier result).
SLOT_KINDS: dict[str, dict[str, Any]] = {
    "account": {"tokens": ("NUMBER",), "label": "Account number",
                "hint": "An account number from this ticket, as its masked value, for example <NUMBER_1>."},
    "mobile": {"tokens": ("MOBILE",), "label": "Mobile number",
               "hint": "A mobile number from this ticket, as its masked value, for example <MOBILE_1>."},
    "national_id": {"tokens": ("NATIONAL_ID",), "label": "National ID",
                    "hint": "A national ID from this ticket, as its masked value, for example <NATIONAL_ID_1>."},
    "number": {"tokens": ("NUMBER", "MOBILE"), "label": "Any long number",
               "hint": "A long number from this ticket (account, card, reference), as its masked value."},
    "code": {"tokens": None, "label": "Short code or reference",
             "hint": "A short code or reference from the ticket or a lookup: letters, digits, . _ / - (up to 40)."},
    "date": {"tokens": None, "label": "Date", "hint": "A date as YYYY-MM-DD (at most two years back)."},
    "amount": {"tokens": None, "label": "Amount", "hint": "An amount such as 150 or 1500.00."},
}
SLOT_RE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]{0,30})\s*\}\}")
CODE_RE = re.compile(r"^[A-Za-z0-9._/-]{1,40}$")
AMOUNT_RE = re.compile(r"^\d{1,12}(?:\.\d{1,2})?$")
# Statements and functions a read-only lookup never needs. Checked outside string literals and comments, on a
# query that must be one statement starting with SELECT or WITH. The Metabase account's own rights are the real
# limit; this keeps an honest mistake (or a pasted UPDATE) from ever being switched on.
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|upsert|drop|alter|create|truncate|grant|revoke|exec|execute|call|into|declare|"
    r"waitfor|shutdown|kill|backup|restore|attach|detach|pragma|copy|openrowset|opendatasource|openquery|bulk|"
    r"sleep|benchmark|pg_sleep|outfile|dumpfile|load_file|dblink|lo_import|lo_export|pg_read_file|pg_ls_dir|"
    r"pg_terminate_backend|pg_cancel_backend|set_config|sys_eval|sys_exec|xp_\w+|sp_\w+|dbms_\w+|utl_\w+)\b",
    re.I)
ROW_LIMITS = (re.compile(r"\bTOP\s*\(?\s*(\d+)", re.I), re.compile(r"\bLIMIT\s+(\d+)", re.I),
              re.compile(r"\bFETCH\s+(?:FIRST|NEXT)\s+(\d+)\s+ROWS?\s+ONLY", re.I),
              re.compile(r"\bROWNUM\s*<=?\s*(\d+)", re.I))
MAX_QUERY_ROWS = 200          # the most a query may ask its database for
ROWS_TO_MODEL = 30            # the most rows the model sees from one lookup
MAX_RESULT_CHARS = 6000
DEFAULT_PROMPT_CHARS = 16000  # notes beyond this are left out of the prompt (newest approved first)
EXAMPLE_CHARS_IN_PROMPT = 1500


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class KnowledgeError(ValueError):
    """Something a person must fix (shown to them as is)."""


@dataclass
class Draft:
    title: str
    body: str
    example: Optional[str] = None
    topic: Optional[str] = None
    query_db: Optional[str] = None
    query_sql: Optional[str] = None
    query_slots: dict[str, str] = field(default_factory=dict)
    query_about: Optional[str] = None

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> "Draft":
        query = body.get("query") or None
        if query is not None and not isinstance(query, dict):
            raise KnowledgeError("query must be an object with database, sql, slots and about.")
        slots = (query or {}).get("slots") or {}
        if not isinstance(slots, dict):
            raise KnowledgeError("query slots must map each {{slot}} to a kind.")
        return cls(
            title=str(body.get("title") or "").strip(),
            body=str(body.get("body") or "").strip(),
            example=(str(body.get("example") or "").strip() or None),
            topic=(str(body.get("topic") or "").strip() or None),
            query_db=(str((query or {}).get("database") or "").strip() or None),
            query_sql=(str((query or {}).get("sql") or "").strip() or None),
            query_slots={str(k).strip(): str(v).strip() for k, v in slots.items()},
            query_about=(str((query or {}).get("about") or "").strip() or None),
        )

    @property
    def has_query(self) -> bool:
        return bool(self.query_sql)

    def problems(self) -> list[str]:
        out = []
        if not 4 <= len(self.title) <= 120:
            out.append("Give it a short title (4 to 120 characters).")
        if not 20 <= len(self.body) <= 4000:
            out.append("Write what the agent should know in 20 to 4,000 characters.")
        if self.example and len(self.example) > 6000:
            out.append("Keep the pasted logs or data under 6,000 characters (the lines that matter).")
        if self.topic and len(self.topic) > 200:
            out.append("The help topic is too long.")
        if self.has_query or self.query_db or self.query_about or self.query_slots:
            if not self.query_db or len(self.query_db) > 100:
                out.append("Say which Metabase database the query runs on.")
            if not self.query_about or not 40 <= len(self.query_about) <= 600:
                out.append("Describe what the query returns and when to use it (40 to 600 characters); the agent "
                           "reads this to decide when to run it.")
            out += query_problems(self.query_sql or "", self.query_slots)
        return out

    def masked(self) -> "Draft":
        """Text with account, card and phone numbers masked (as stored and as sent to the model)."""
        pseudo = Pseudonymizer()
        return Draft(self.title and pseudo.mask(self.title), pseudo.mask(self.body),
                     pseudo.mask(self.example) if self.example else None, self.topic, self.query_db,
                     self.query_sql, dict(self.query_slots),
                     pseudo.mask(self.query_about) if self.query_about else None)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"title": self.title, "body": self.body, "example": self.example, "topic": self.topic}
        if self.has_query:
            out["query"] = {"database": self.query_db, "sql": self.query_sql, "slots": self.query_slots,
                            "about": self.query_about}
        return out


def _strip_sql(sql: str) -> str:
    """The query without comments and string literals (what keywords and semicolons are checked on)."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def query_problems(sql: str, slots: dict[str, str]) -> list[str]:
    """Why a query can't be a lookup (empty when it can)."""
    out: list[str] = []
    if not sql.strip():
        return ["Paste the query (one SELECT)."]
    if len(sql) > 4000:
        out.append("Keep the query under 4,000 characters.")
    bare = _strip_sql(sql).strip()
    if re.search(r"/\*|\*/", bare) or bare.count("'") % 2:
        out.append("The query has an unclosed comment or quote.")
    if ";" in bare.rstrip().rstrip(";"):
        out.append("One statement only (no ; in the middle).")
    if not re.match(r"^(select|with)\b", bare, re.I):
        out.append("A lookup must start with SELECT (or WITH ... SELECT): it can only read.")
    bad = sorted({m.group(1).upper() for m in FORBIDDEN.finditer(bare)})
    if bad:
        out.append(f"A lookup can only read, so it can't use {', '.join(bad)}.")
    if "[[" in sql or "]]" in sql:
        out.append("Optional parts ([[ ... ]]) aren't supported; give every slot a value.")
    if re.search(r"\d{9,}", sql):
        out.append("Don't put account, card or phone numbers in the query; use a {{slot}} and the agent fills it "
                   "from the ticket.")
    limits = [int(m.group(1)) for rx in ROW_LIMITS for m in rx.finditer(bare)]
    if not limits:
        out.append(f"Add a row limit (TOP 50, LIMIT 50 or FETCH FIRST 50 ROWS ONLY), at most {MAX_QUERY_ROWS}.")
    elif max(limits) > MAX_QUERY_ROWS:
        out.append(f"Ask for at most {MAX_QUERY_ROWS} rows.")
    used = {m.group(1) for m in SLOT_RE.finditer(bare)}
    in_quotes = {m.group(1) for m in SLOT_RE.finditer(sql)} - used
    if in_quotes:
        out.append(f"Put {', '.join('{{' + s + '}}' for s in sorted(in_quotes))} outside quotes; Metabase adds them.")
    for name in sorted(used - set(slots)):
        out.append(f"Say what kind of value {{{{{name}}}}} takes.")
    for name in sorted(set(slots) - used - in_quotes):
        out.append(f"{name} isn't used in the query; remove it or add {{{{{name}}}}}.")
    for name, kind in sorted(slots.items()):
        if kind not in SLOT_KINDS:
            out.append(f"{name}: kind must be one of {', '.join(SLOT_KINDS)}.")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,30}", name):
            out.append(f"{name!r} isn't a valid slot name (letters, digits and _).")
    return out


def tool_name_for(item_id: int, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40].strip("_") or "lookup"
    return f"k{item_id}_{slug}"


@dataclass
class Item:
    """A note as the agent uses it (masked text)."""
    id: int
    title: str
    body: str
    example: Optional[str] = None
    topic: Optional[str] = None
    query_db: Optional[str] = None
    query_sql: Optional[str] = None
    query_slots: dict[str, str] = field(default_factory=dict)
    query_about: Optional[str] = None
    tool_name: Optional[str] = None
    created_by: str = ""
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    source_ticket: Optional[str] = None
    draft: bool = False

    @property
    def label(self) -> str:
        return "DRAFT" if self.draft else f"K{self.id}"

    @classmethod
    def from_row(cls, row: Any) -> "Item":
        return cls(id=int(row["id"]), title=row["title"], body=row["body"], example=row["example"],
                   topic=row["topic"], query_db=row["query_db"], query_sql=row["query_sql"],
                   query_slots=json.loads(row["query_slots"] or "{}"), query_about=row["query_about"],
                   tool_name=row["tool_name"], created_by=row["created_by"], decided_by=row["decided_by"],
                   decided_at=row["decided_at"], source_ticket=row["source_ticket"])

    @classmethod
    def from_draft(cls, draft: Draft, by: str, source_ticket: Optional[str]) -> "Item":
        """A note being tried on one ticket before anyone switched it on (agentcore.trial)."""
        d = draft.masked()
        return cls(id=0, title=d.title, body=d.body, example=d.example, topic=d.topic, query_db=d.query_db,
                   query_sql=d.query_sql, query_slots=d.query_slots, query_about=d.query_about,
                   tool_name="k0_draft_lookup" if d.has_query else None, created_by=by,
                   source_ticket=source_ticket, draft=True)


class KnowledgeStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        db.executescript(SCHEMA)

    def get(self, item_id: int) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM knowledge WHERE id = ?", (item_id,)).fetchone()

    def propose(self, draft: Draft, by: str, source_decision_id: Optional[int] = None,
                source_ticket: Optional[str] = None) -> sqlite3.Row:
        problems = draft.problems()
        if problems:
            raise KnowledgeError(" ".join(problems))
        d = draft.masked()
        cursor = self.db.execute(
            "INSERT INTO knowledge (created_at, created_by, status, title, body, example, topic, query_db, query_sql, "
            "query_slots, query_about, source_decision_id, source_ticket) "
            "VALUES (?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), by, d.title, d.body, d.example, d.topic, d.query_db, d.query_sql,
             json.dumps(d.query_slots) if d.has_query else None, d.query_about, source_decision_id, source_ticket))
        item_id = int(cursor.lastrowid)
        if d.has_query:
            self.db.execute("UPDATE knowledge SET tool_name = ? WHERE id = ?", (tool_name_for(item_id, d.title), item_id))
        self.db.commit()
        return self.get(item_id)  # type: ignore[return-value]

    def decide(self, item_id: int, verdict: str, user: str, role: str, note: Optional[str] = None) -> sqlite3.Row:
        """approve / reject (an Admin who did not write it), retire (an Admin), withdraw (the person who wrote it)."""
        row = self.get(item_id)
        if row is None:
            raise LookupError("No note with that id.")
        note = (note or "").strip()[:500] or None
        status = row["status"]
        if verdict in ("approve", "reject"):
            if role != "admin":
                raise PermissionError("Only an Admin can switch a note on or turn it down.")
            if user == row["created_by"]:
                raise PermissionError("Another Admin must approve this: whoever teaches the agent can't also switch "
                                      "the note on (maker-checker).")
            if status != "proposed":
                raise ValueError(f"This note is {status}, not waiting for approval.")
            if verdict == "approve":  # re-check the query: the rules may have tightened since it was proposed
                problems = (query_problems(row["query_sql"], json.loads(row["query_slots"] or "{}"))
                            if row["query_sql"] else [])
                if problems:
                    raise KnowledgeError("The query can't be switched on: " + " ".join(problems))
            self.db.execute("UPDATE knowledge SET status = ?, decided_by = ?, decided_at = ?, decision_note = ? "
                            "WHERE id = ?", ("active" if verdict == "approve" else "rejected", user, now_iso(), note,
                                             item_id))
        elif verdict == "retire":
            if role != "admin":
                raise PermissionError("Only an Admin can switch a note off.")
            if status != "active":
                raise ValueError(f"This note is {status}, not in use.")
            self.db.execute("UPDATE knowledge SET status = 'retired', retired_by = ?, retired_at = ?, retire_note = ? "
                            "WHERE id = ?", (user, now_iso(), note, item_id))
        elif verdict == "withdraw":
            if user != row["created_by"]:
                raise PermissionError("Only the person who wrote it can withdraw a note.")
            if status != "proposed":
                raise ValueError(f"This note is {status}; only a note waiting for approval can be withdrawn.")
            self.db.execute("UPDATE knowledge SET status = 'withdrawn', retired_by = ?, retired_at = ?, "
                            "retire_note = ? WHERE id = ?", (user, now_iso(), note, item_id))
        else:
            raise ValueError("verdict must be approve, reject, retire or withdraw.")
        self.db.commit()
        return self.get(item_id)  # type: ignore[return-value]

    def active(self) -> list[Item]:
        """Switched-on notes, newest approval first."""
        rows = self.db.execute("SELECT * FROM knowledge WHERE status = 'active' "
                               "ORDER BY decided_at DESC, id DESC").fetchall()
        return [Item.from_row(r) for r in rows]

    def listing(self) -> list[dict[str, Any]]:
        """Every note with how often decisions used it (for the console)."""
        uses = _uses(self.db)
        out = []
        for row in self.db.execute("SELECT * FROM knowledge ORDER BY id DESC").fetchall():
            item = dict(row)
            item["query_slots"] = json.loads(row["query_slots"] or "{}")
            used = uses.get(int(row["id"]), (0, None))
            item["uses"], item["last_used_at"] = used
            out.append(item)
        return out

    def pending(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM knowledge WHERE status = 'proposed'").fetchone()[0])


def _uses(db: sqlite3.Connection) -> dict[int, tuple[int, Optional[str]]]:
    """How many decisions used each note, and when last (decisions.knowledge_used is a JSON list of ids)."""
    have = {r[1] for r in db.execute("PRAGMA table_info(decisions)").fetchall()}
    if "knowledge_used" not in have:
        return {}
    out: dict[int, tuple[int, Optional[str]]] = {}
    for row in db.execute("SELECT created_at, knowledge_used FROM decisions WHERE knowledge_used IS NOT NULL "
                          "AND knowledge_used != '[]'").fetchall():
        try:
            ids = json.loads(row["knowledge_used"])
        except (TypeError, ValueError):
            continue
        for i in ids if isinstance(ids, list) else []:
            n, last = out.get(int(i), (0, None))
            out[int(i)] = (n + 1, max(filter(None, (last, row["created_at"]))))
    return out


# ---- what the model gets ------------------------------------------------------------------------------------
PREAMBLE = """## What ICT staff have taught this desk
Each note below was written by ICT staff and switched on by an Admin (a DRAFT note is being tried on this one
ticket). Use a note when it fits the ticket. The lookups and this desk's rules still decide: a note never changes
what you may post or when a person must approve. If a note conflicts with the evidence, follow the evidence and say
so in your reason. When a note shapes your decision, put its id (for example K12) in your evidence. Lookups named
k<id>_... were written by staff for these notes; use them the same way as your other lookups."""


def prompt_block(items: list[Item], max_chars: int = DEFAULT_PROMPT_CHARS) -> tuple[str, list[Item], list[Item]]:
    """(text for the system prompt, notes included, notes left out because the prompt budget ran out).
    A draft note always goes first (it is what is being tried)."""
    ordered = sorted(items, key=lambda i: not i.draft)
    parts, included, left_out, size = [PREAMBLE], [], [], len(PREAMBLE)
    for item in ordered:
        text = _note_text(item)
        if size + len(text) > max_chars and not item.draft:
            left_out.append(item)
            continue
        parts.append(text)
        included.append(item)
        size += len(text)
    return ("\n\n".join(parts) if included else ""), included, left_out


def _note_text(item: Item) -> str:
    who = (f"Being tried by {item.created_by} on ticket #{item.source_ticket}" if item.draft else
           f"Taught by {item.created_by}" + (f" from ticket #{item.source_ticket}" if item.source_ticket else "")
           + (f"; switched on by {item.decided_by} on {item.decided_at[:10]}" if item.decided_by and item.decided_at else ""))
    lines = [f"[{item.label}] {item.title}",
             f"Applies to: {'help topic ' + item.topic + ' (and its subtopics)' if item.topic else 'every ticket on this desk'}",
             who, item.body]
    if item.example:
        example = item.example if len(item.example) <= EXAMPLE_CHARS_IN_PROMPT else item.example[:EXAMPLE_CHARS_IN_PROMPT] + " ..."
        lines.append("Example the teacher gave (logs or rows, masked):\n" + example)
    if item.tool_name:
        lines.append(f"Lookup for this note: {item.tool_name}")
    return "\n".join(lines)


def version_of(system: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str:
    """A short hash of exactly what instructs the model: the system prompt (with notes) and the tool definitions."""
    text = json.dumps({"system": [b.get("text") for b in system], "tools": tools}, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def used_ids(transcript: list[dict[str, Any]], texts: list[Optional[str]], known: list[int]) -> list[int]:
    """Notes a decision used: it ran a note's lookup, or cited K<id> in its reason or evidence."""
    found: set[int] = set()
    for message in transcript or []:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                m = re.match(r"^k(\d+)_", str(block.get("name") or ""))
                if m:
                    found.add(int(m.group(1)))
    for text in texts:
        found.update(int(n) for n in re.findall(r"\bK(\d+)\b", text or ""))
    return sorted(i for i in found if i in known)


class KnowledgeTools:
    """The lookups that switched-on notes carry, as tools for the model."""

    def __init__(self, items: list[Item], metabase: Any, today: Optional[date] = None) -> None:
        self.items = {i.tool_name: i for i in items if i.query_sql and i.tool_name}
        self.mb = metabase
        self._today = today
        self._db_ids: dict[str, int] = {}

    def __bool__(self) -> bool:
        return bool(self.items)

    def handles(self, name: str) -> bool:
        return name in self.items

    def schemas(self) -> list[dict[str, Any]]:
        out = []
        for name, item in self.items.items():
            props = {slot: {"type": "string", "description": SLOT_KINDS[kind]["hint"]}
                     for slot, kind in item.query_slots.items()}
            out.append({
                "name": name,
                "description": (f"{item.query_about} Returns at most {ROWS_TO_MODEL} rows. Written by ICT staff for "
                                f"note {item.label} ({item.title})."),
                "input_schema": {"type": "object", "properties": props, "required": list(props),
                                 "additionalProperties": False},
            })
        return out

    def run(self, name: str, args: dict[str, Any], pseudo: Pseudonymizer, evidence: Any = None,
            ticket: Any = None) -> tuple[str, bool]:
        item = self.items[name]
        try:
            params = {slot: self._value(slot, kind, args.get(slot), pseudo) for slot, kind in item.query_slots.items()}
        except KnowledgeError as exc:
            return f"Invalid input: {exc}", True
        if self.mb is None:
            return "Lookup unavailable: no Metabase connection here.", True
        tools_used = getattr(evidence, "tools_used", None)
        if isinstance(tools_used, list):
            tools_used.append(name)
        try:
            db_id = self._db_ids.get(item.query_db or "")
            if db_id is None:
                db_id = self._db_ids[item.query_db or ""] = self.mb.resolve(item.query_db or "")
            rows = self.mb.query_bound(db_id, item.query_sql or "", params)
        except Exception as exc:  # noqa: BLE001 - a failed lookup is information for the model, never a crash
            return pseudo.mask(f"Lookup failed (data source error): {str(exc)[:300]}. Decide without it if you "
                               "must, and say so.")[:800], True
        data = {
            "source": f"note {item.label}: {item.title}",
            "retrieved_at": now_iso(),
            "row_count": len(rows),
            "rows": [{k: _plain(v) for k, v in r.items()} for r in rows[:ROWS_TO_MODEL]],
            "truncated": len(rows) > ROWS_TO_MODEL,
        }
        text = json.dumps(pseudo.mask_obj(data), default=str, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + " ...[truncated]"
        return text, False

    def _value(self, slot: str, kind: str, raw: Any, pseudo: Pseudonymizer) -> tuple[str, Any]:
        """(Metabase tag type, value) for one slot, or KnowledgeError with what the model must fix."""
        value = str(raw if raw is not None else "").strip()
        spec = SLOT_KINDS[kind]
        if not value:
            raise KnowledgeError(f"{slot} is required. {spec['hint']}")
        if spec["tokens"]:
            if not TOKEN_RE.fullmatch(value):
                raise KnowledgeError(f"{slot}: give the masked value from this ticket, like "
                                     f"<{spec['tokens'][0]}_1>, not a number.")
            if pseudo.unknown_tokens(value) or value[1:].split("_")[0] not in spec["tokens"]:
                raise KnowledgeError(f"{slot}: {value} is not on this ticket or in an earlier result "
                                     f"({spec['label'].lower()} expected).")
            return "text", pseudo.unmask(value)
        if kind == "date":
            try:
                day = date.fromisoformat(value)
            except ValueError:
                raise KnowledgeError(f"{slot} must be a date as YYYY-MM-DD.") from None
            today = self._today or date.today()
            if not today - timedelta(days=730) <= day <= today + timedelta(days=1):
                raise KnowledgeError(f"{slot}: {value} is outside the last two years.")
            return "date", day.isoformat()
        if kind == "amount":
            if not AMOUNT_RE.fullmatch(value.replace(",", "")):
                raise KnowledgeError(f"{slot} must be an amount such as 150 or 1500.00.")
            try:
                return "number", Decimal(value.replace(",", ""))
            except InvalidOperation:
                raise KnowledgeError(f"{slot} must be an amount such as 150 or 1500.00.") from None
        if not CODE_RE.fullmatch(value) or re.search(r"\d{9,}", value) or TOKEN_RE.search(value):
            raise KnowledgeError(f"{slot}: {spec['hint']}")
        return "text", value


def _plain(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class GuidedToolbox:
    """A desk's own toolbox plus extra lookups: its notes' (KnowledgeTools) and past cases (agentcore.cases.tools).
    Each extra has handles, schemas and run(name, args, pseudo, evidence, ticket). Everything else (as_of, sources)
    is the desk's toolbox."""

    def __init__(self, base: Any, *extras: Any) -> None:
        self._base = base
        self._extras = [e for e in extras if e]

    def schemas(self) -> list[dict[str, Any]]:
        return self._base.schemas() + [s for extra in self._extras for s in extra.schemas()]

    def run(self, name: str, args: dict[str, Any], pseudo: Pseudonymizer, evidence: Any, ticket: Any) -> tuple[str, bool]:
        for extra in self._extras:
            if extra.handles(name):
                return extra.run(name, args or {}, pseudo, evidence, ticket)
        return self._base.run(name, args, pseudo, evidence, ticket)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._base, item)


def new_tag_id() -> str:
    return str(uuid.uuid4())
