"""explore_data and query_tables: the agents' schema-aware, read-only look into the bank's databases.

query_data (agentcore.dataquery) reads one table from a short approved list. These two tools let the agent work the way
an analyst does: see what tables and columns exist in an approved database (explore_data: names and types only, from
agentcore/schema/*.json, built by tools/build_schema.py), then read up to three tables of one database joined on
columns it names (query_tables). The agent never writes SQL: it names tables, columns, joins and filters, and the SQL is
built here:

- only databases, tables and columns in the schema files; a personal column (name, ID number, e-mail, address, card
  number, PIN, OTP, ...) is not in them, so it cannot be named; a mobile-number column can be matched on, never shown;
  free-text bodies (request/response payloads, raw messages, XML, JSON) cannot be shown or filtered on;
- one database, one to three tables, equality joins (inner or left) that connect every table;
- read only (SELECT), at most 25 rows;
- a client's rows only through a match on an identifier the ticket gives (checked by the desk, expanded to every form
  the number may be stored in), within a time window; without a match, counts only, over at most two days, grouped
  by columns that are not identifiers;
- a time window on a column the agent names whenever a table is large or there is no match.

Values are written as escaped literals; names come only from the schema files and are checked as plain identifiers.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from .dataquery import (
    DEFAULT_DAYS, KINDS, LONG_NUMBER_RE, MAX_ROWS, NAME_RE, OPEN_WINDOW_DAYS, OPS, Identify, _as_text, _limited,
    _plain, _quoter, _scrub, _text, parse_moment,
)

log = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).with_name("schema")
MAX_TABLES, MAX_SHOWN_COLUMNS, MAX_MATCH_DAYS, PROTECTED_WINDOW_DAYS = 3, 30, 31, 2
BIG_TABLE_ROWS = 100_000  # a table this big (by the database's own estimate) is only read inside a time window
ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,11}$")
REF_RE = re.compile(r"^([a-z][a-z0-9_]{0,11})\.([A-Za-z_][A-Za-z0-9_$#]*)$")
# Free-text bodies can hold names and numbers no catalogue can list: never shown, never filtered on.
FREE_TEXT = re.compile(r"(?i)(^|_)(body|payload|raw|xml|json|stacktrace|statement_?data|request|response)(_?(body|data|text|message|xml|json))?$"
                       r"|(^|_)(request|response)_?(body|data|text|message|xml|json)")
# Secrets: a token, voucher, PIN, OTP or password column is never shown or filtered on (token_amount / token_units are not).
SECRET_COLUMN = re.compile(r"(?i)(^|_)(token|voucher|pin|otp|secret|password|passwd|cvv)$|token_?voucher|voucher")
FREE_TEXT_TYPES = re.compile(r"(?i)clob|blob|json|xml|bytea|longtext|mediumtext|longblob|varbinary|image|binary")
# Columns that hold an account, reference or number a count must not be grouped by (another client's identifier).
IDENT_LIKE = re.compile(r"(?i)acc(ount|t)|acnt|reference|(^|_)ref(_|$)|rrn|cif|msisdn|mobile|phone|(^|_)pan$|card|"
                        r"identification|beneficiary_?(acc|num)|(^|_)stan$|uuid|(^|_)id$|transactionid|trace")
TEMPORAL = re.compile(r"(?i)timestamp|date|time")
NOT_TEMPORAL = re.compile(r"(?i)interval|varchar|char|text|string")


def hidden(name: str, kind: str) -> bool:
    """A column the agent may not show, filter on or count by: free text, a secret, or a blob."""
    return bool(FREE_TEXT.search(name) or SECRET_COLUMN.search(name) or FREE_TEXT_TYPES.search(kind or ""))


def temporal(kind: str) -> bool:
    """Whether a column type is a real date/time (compared as one), not text holding a date (compared as text)."""
    return bool(TEMPORAL.search(kind or "")) and not NOT_TEMPORAL.search(kind or "")


@dataclass
class Table:
    database: str
    key: str  # schema.table (a MySQL table: the bare name)
    columns: dict[str, str]
    rows: Optional[int] = None
    about: str = ""
    match_only: list[str] = field(default_factory=list)  # mobile-number columns: matched on, never shown
    indexes: list[list[str]] = field(default_factory=list)  # each index's columns in order (empty: not known)

    @property
    def indexed(self) -> list[str]:
        """The columns that lead an index: a filter on one of them can use it."""
        return sorted({ix[0] for ix in self.indexes if ix})

    @property
    def sql_name(self) -> list[str]:
        return self.key.split(".")

    def date_columns(self) -> list[str]:
        return [c for c, k in self.columns.items() if temporal(k)]


@dataclass
class Database:
    name: str
    engine: str
    tables: dict[str, Table]
    db_id: Optional[int] = None
    aliases: list[str] = field(default_factory=list)  # other Metabase connections to the same database
    protected: bool = False  # the live Postilion switch database: read only on an index, in a short window


class Schema:
    """The approved databases' tables and columns (names and types only)."""

    def __init__(self, databases: dict[str, Database]) -> None:
        self.databases = databases  # lower-case name -> Database

    @classmethod
    def load(cls, path: Path = SCHEMA_DIR) -> "Schema":
        out: dict[str, Database] = {}
        for file in sorted(Path(path).glob("*.json")):
            raw = json.loads(file.read_text(encoding="utf-8"))
            name = str(raw["database"]).strip()
            tables = {}
            for key, t in raw["tables"].items():
                if not all(NAME_RE.match(p) for p in key.split(".")):
                    continue
                cols = {c: str(k) for c, k in (t.get("columns") or {}).items() if NAME_RE.match(c)}
                tables[key] = Table(name, key, cols, t.get("rows"), t.get("about", ""),
                                    [c for c in t.get("match_only", []) if NAME_RE.match(c)],
                                    [[c for c in ix if c in cols] for ix in t.get("indexes", []) if ix and ix[0] in cols])
            out[name.lower()] = Database(name, str(raw.get("engine") or "postgres"), tables,
                                         aliases=[str(a) for a in raw.get("aliases", [])],
                                         protected=bool(raw.get("protected")))
        return cls(out)

    def database(self, name: str) -> Database:
        wanted = str(name or "").strip().lower()
        db = self.databases.get(wanted) or next(
            (d for d in self.databases.values() if wanted in {a.lower() for a in d.aliases}), None)
        if db is None:
            raise ValueError(f"database must be one of {', '.join(d.name for d in self.databases.values())}")
        return db

    def table(self, db: Database, name: str) -> Table:
        """A table by its listed name, or by its bare name when only one table has it."""
        name = str(name or "").strip()
        hit = {k.lower(): t for k, t in db.tables.items()}.get(name.lower())
        if hit is None:
            bare = [t for k, t in db.tables.items() if k.split(".")[-1].lower() == name.lower()]
            hit = bare[0] if len(bare) == 1 else None
        if hit is None:
            close = [k for k in db.tables if name.lower() in k.lower()][:8]
            raise ValueError(f"{name!r} is not a table of {db.name}" + (f" (similar: {', '.join(close)})" if close else
                                                                       ": use explore_data to find it"))
        return hit

    def search(self, words: str, database: Optional[str] = None, limit: int = 12) -> list[tuple[int, Table]]:
        """Tables whose name or columns contain every word (a table-name hit ranks first)."""
        terms = [w for w in re.split(r"[\s,]+", words.lower()) if w]
        pool = [self.database(database)] if database else list(self.databases.values())
        found = []
        for db in pool:
            for t in db.tables.values():
                name = t.key.lower()
                cols = " ".join(t.columns).lower()
                if all(w in name or w in cols for w in terms):
                    score = sum(2 for w in terms if w in name) + sum(1 for w in terms if w in cols)
                    found.append((score, t))
        found.sort(key=lambda x: (-x[0], x[1].database, x[1].key))
        return found[:limit]


@dataclass
class PreparedJoin:
    database: Database
    sql: str
    mode: str
    labels: list[str]  # the shown columns, in order, as alias.column
    sources: list[str]
    match_columns: list[str]
    kind: Optional[str]
    since: Optional[datetime]
    until: Optional[datetime]
    limit: int


PROTECTED_NOTE = ("live production database: every table is read only on the first column of one of its indexes, "
                  f"within a window of at most {PROTECTED_WINDOW_DAYS} days")
EXPLORE_TOOL = {
    "name": "explore_data",
    "description": (
        "See what the bank's databases hold, before querying: names and column types only, never rows. With no "
        "arguments: the databases. With `database`: its tables (with an estimate of their rows). With `search` "
        "(words that appear in a table or column name, e.g. 'zesa token' or 'reversal'): the matching tables and "
        "their columns, in one database or all. With `database` and `table`: that table's columns and types, its date "
        "columns and any identifier-like columns. Use it to decide which tables to read and how they join "
        "(query_tables); personal columns are not listed at all."),
    "input_schema": {"type": "object", "properties": {
        "database": {"type": "string"},
        "search": {"type": "string", "description": "Words from a table or column name."},
        "table": {"type": "string", "description": "With database: show this table's columns."}}},
}


class SchemaQuery:
    def __init__(self, mb: Any, schema: Schema) -> None:
        self.mb, self.schema = mb, schema

    @classmethod
    def from_directory(cls, mb: Any, path: Optional[Path] = None) -> "SchemaQuery":
        """Resolve each database by name in Metabase; one that is not there is left out (logged)."""
        schema = Schema.load(path or SCHEMA_DIR)
        present = {str(d.get("name", "")).strip().lower(): d for d in mb.databases()}
        for key in list(schema.databases):
            db = present.get(key)
            if db is None:
                log.warning("query_tables: database %r is not in Metabase; left out", schema.databases[key].name)
                del schema.databases[key]
                continue
            schema.databases[key].db_id = int(db["id"])
            schema.databases[key].engine = str(db.get("engine") or schema.databases[key].engine)
        return cls(mb, schema)

    # ---- the tools ---------------------------------------------------------------------------------
    def tool_schemas(self) -> list[dict[str, Any]]:
        names = ", ".join(d.name for d in self.schema.databases.values())
        query = {
            "name": "query_tables",
            "description": (
                "Read one to three tables of one database, joined on columns you name (find them with explore_data). "
                "A client's rows are only returned matched on an identifier the ticket gives (account, mobile, "
                "reference/RRN or CIF, as <NUMBER_1> etc.): `match` names the column(s) of the tables that hold it, "
                "inside a time window (`time`). Without a match only counts come back (mode=count, grouped by "
                "`count_by`, at most two days): is a service failing for everyone? Columns are written as "
                "alias.column, where alias is the `as` you gave the table. Dates stored as text are compared as "
                "'YYYY-MM-DD HH:MM:SS' text. Joins are equality joins (inner, or left to find rows with no partner). "
                "Results come back with a query_id (Q1, Q2, ...): put it at the start of the evidence item it "
                f"supports. At most {MAX_ROWS} rows, newest first by the time column.\nDatabases: {names}."),
            "input_schema": {"type": "object", "properties": {
                "database": {"type": "string"},
                "tables": {"type": "array", "minItems": 1, "maxItems": MAX_TABLES, "items": {
                    "type": "object", "properties": {"table": {"type": "string"}, "as": {"type": "string"}},
                    "required": ["table", "as"]}},
                "join": {"type": "array", "description": "One entry per table after the first.", "items": {
                    "type": "object", "properties": {
                        "left": {"type": "string", "description": "alias.column"},
                        "right": {"type": "string", "description": "alias.column"},
                        "how": {"type": "string", "enum": ["inner", "left"]}}, "required": ["left", "right"]}},
                "match": {"type": "object", "properties": {
                    "columns": {"type": "array", "items": {"type": "string"},
                                "description": "alias.column holding the identifier (any of them may match)."},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "value": {"type": "string", "description": "The value from the ticket, e.g. <NUMBER_1>."}},
                    "required": ["columns", "kind", "value"]},
                "time": {"type": "object", "properties": {
                    "column": {"type": "string", "description": "alias.column"},
                    "since": {"type": "string", "description": "YYYY-MM-DD or YYYY-MM-DD HH:MM."},
                    "until": {"type": "string", "description": "YYYY-MM-DD (that whole day) or YYYY-MM-DD HH:MM."}},
                    "required": ["column"]},
                "where": {"type": "array", "items": {"type": "object", "properties": {
                    "column": {"type": "string", "description": "alias.column"}, "op": {"type": "string", "enum": list(OPS)},
                    "value": {"description": "A text or number; a list for in / not in."}}, "required": ["column", "op"]}},
                "columns": {"type": "array", "items": {"type": "string"},
                            "description": f"mode=rows: the alias.column values to show (at most {MAX_SHOWN_COLUMNS})."},
                "mode": {"type": "string", "enum": ["rows", "count"]},
                "count_by": {"type": "array", "items": {"type": "string"}, "description": "mode=count: alias.column to group by."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS}},
                "required": ["database", "tables"]},
        }
        return [EXPLORE_TOOL, query]

    # ---- explore_data ------------------------------------------------------------------------------
    def explore(self, request: dict[str, Any]) -> dict[str, Any]:
        database, search, table = (str(request.get(k) or "").strip() for k in ("database", "search", "table"))
        if table and not database:
            raise ValueError("table needs database")
        if not database and not search:
            return {"databases": [{"database": d.name, "engine": d.engine, "tables": len(d.tables),
                                   **({"also_known_as": d.aliases} if d.aliases else {}),
                                   **({"protected": PROTECTED_NOTE} if d.protected else {})}
                                  for d in self.schema.databases.values()]}
        if table:
            t = self.schema.table(self.schema.database(database), table)
            return self._describe(t, full=True)
        if search:
            hits = self.schema.search(search, database or None)
            return {"search": search, "tables": [self._describe(t, full=False) for _, t in hits],
                    "note": ("Showing the best matches: add words or a database to narrow it." if len(hits) == 12
                             else None) or ("Nothing matches: try other words, or list a database's tables." if not hits
                                            else None)}
        db = self.schema.database(database)
        names = sorted(db.tables)
        return {"database": db.name, "engine": db.engine, "tables": [
            f"{k} (~{db.tables[k].rows} rows)" if db.tables[k].rows is not None else k for k in names[:300]],
            "note": (f"{len(names)} tables: showing 300; use search to find others." if len(names) > 300 else None)}

    @staticmethod
    def _describe(t: Table, full: bool) -> dict[str, Any]:
        cap = 200 if full else 40
        out: dict[str, Any] = {"database": t.database, "table": t.key, "rows_estimate": t.rows,
                               "columns": [f"{c} {k}" + (" [cannot be shown or filtered]" if hidden(c, k) else "")
                                          for c, k in list(t.columns.items())[:cap]]}
        if len(t.columns) > cap:
            out["more_columns"] = len(t.columns) - cap
        if t.about:
            out["about"] = t.about
        dates = t.date_columns()
        if dates:
            out["date_columns"] = dates
        if full:
            ids = [c for c in t.columns if IDENT_LIKE.search(c)]
            if ids:
                out["identifier_like_columns"] = ids
            if t.match_only:
                out["match_only_columns"] = t.match_only  # a mobile can be matched on these; they are never shown
        if t.indexes:
            out["indexed_columns"] = t.indexed  # a large table answers quickly only when filtered on one of these
            if full:  # the order matters: an index is used through its first column, then the next
                out["indexes"] = [", ".join(ix) for ix in t.indexes]
        if t.rows is not None and t.rows >= BIG_TABLE_ROWS:
            out["large"] = "large table: a time window is required"
        return out

    # ---- query_tables ------------------------------------------------------------------------------
    def prepare(self, request: dict[str, Any], identify: Identify, now: Optional[datetime] = None,
                as_of: Optional[date] = None) -> PreparedJoin:
        db = self.schema.database(request.get("database"))
        if db.db_id is None:
            raise ValueError(f"{db.name} is not available")
        quote = _quoter(db.engine)
        raw_tables = request.get("tables")
        if not isinstance(raw_tables, list) or not 1 <= len(raw_tables) <= MAX_TABLES:
            raise ValueError(f"tables: one to {MAX_TABLES} tables, each {{table, as}}")
        by_alias: dict[str, Table] = {}
        for item in raw_tables:
            if not isinstance(item, dict):
                raise ValueError("each table is {table, as}")
            alias = str(item.get("as") or "")
            if not ALIAS_RE.match(alias) or alias in by_alias:
                raise ValueError(f"table alias {alias!r}: lower-case letters, digits or _, starting with a letter, "
                                 "and different for each table")
            by_alias[alias] = self.schema.table(db, item.get("table"))
        order = list(by_alias)

        def col(ref: Any, *, allow_match_only: bool = False, what: str = "column") -> tuple[str, str, str]:
            """(alias, column, SQL) for an alias.column reference, checked against the schema."""
            m = REF_RE.match(str(ref or "").strip())
            if not m or m[1] not in by_alias:
                raise ValueError(f"{what} {ref!r} must be alias.column with an alias of one of the tables "
                                 f"({', '.join(order)})")
            alias, name = m[1], m[2]
            t = by_alias[alias]
            hit = next((c for c in t.columns if c.lower() == name.lower()), None)
            if hit is None and allow_match_only:
                hit = next((c for c in t.match_only if c.lower() == name.lower()), None)
            if hit is None:
                raise ValueError(f"{alias}.{name}: not a column of {t.key} (see explore_data)")
            return alias, hit, f"{alias}.{quote(hit)}"

        # Joins: equality, every table connected to the first.
        joins = request.get("join") or []
        if not isinstance(joins, list):
            raise ValueError("join is a list of {left, right}")
        links: list[tuple[str, str, str, str]] = []  # (left alias, right alias, condition, how)
        join_refs: list[tuple[str, str]] = []
        for j in joins:
            if not isinstance(j, dict):
                raise ValueError("each join is {left, right, how}")
            la, lc, lsql = col(j.get("left"), what="join column")
            ra, rc, rsql = col(j.get("right"), what="join column")
            if la == ra:
                raise ValueError("a join connects two different tables")
            how = str(j.get("how") or "inner").lower()
            if how not in ("inner", "left"):
                raise ValueError("how is inner or left")
            links.append((la, ra, f"{lsql} = {rsql}", how))
            join_refs += [(la, lc), (ra, rc)]
        placed, from_parts = {order[0]}, [self._from(by_alias[order[0]], order[0], quote)]
        pending = list(links)
        while len(placed) < len(order):
            step = next((x for x in pending if (x[0] in placed) != (x[1] in placed)), None)
            if step is None:
                raise ValueError("every table after the first needs a join to a table already listed: "
                                 f"{', '.join(a for a in order if a not in placed)} has none")
            new = step[1] if step[0] in placed else step[0]
            conds = [x[2] for x in pending if {x[0], x[1]} <= placed | {new} and new in (x[0], x[1])]
            how = "LEFT JOIN" if step[3] == "left" and step[1] == new else "JOIN"  # left keeps the earlier table's rows
            from_parts.append(f"{how} {self._from(by_alias[new], new, quote)} ON {' AND '.join(conds)}")
            pending = [x for x in pending if x[2] not in conds]
            placed.add(new)
        for x in pending:  # extra joins between tables already placed become more conditions
            from_parts[-1] += f" AND {x[2]}"

        where: list[str] = []
        mode = str(request.get("mode") or "rows")
        if mode not in ("rows", "count"):
            raise ValueError("mode must be rows or count")

        # What to match on.
        match = request.get("match")
        match_columns: list[str] = []
        kind = None
        if match:
            if not isinstance(match, dict):
                raise ValueError("match is {columns, kind, value}")
            kind = str(match.get("kind") or "")
            if kind not in KINDS:
                raise ValueError(f"match kind must be one of {', '.join(KINDS)}")
            refs = match.get("columns") or []
            refs = [refs] if isinstance(refs, str) else list(refs)
            if not refs:
                raise ValueError("match.columns: name the column(s) that hold the identifier")
            conds = []
            value = str(match.get("value") or "").strip()
            if not value:
                raise ValueError("match.value is required")
            forms = [f for f in identify(kind, value) if f]
            if not forms:
                raise ValueError(f"match.value is not a valid {kind}")
            literals = ", ".join(_text(f, db.engine) for f in forms)
            for ref in refs:
                alias, name, sql = col(ref, allow_match_only=kind == "mobile", what="match column")
                conds.append(f"{sql} IN ({literals})")
                match_columns.append(f"{alias}.{name}")
            where.append("(" + " OR ".join(conds) + ")")
        elif mode != "count":
            raise ValueError("a client's rows are returned only matched on an identifier from the ticket: give `match`, "
                             "or use mode=count for counts only")

        # The time window.
        since = until = None
        time_ref: Optional[tuple[str, str]] = None
        tcfg = request.get("time")
        big = [by_alias[a].key for a in order if (by_alias[a].rows or 0) >= BIG_TABLE_ROWS]
        if tcfg:
            if not isinstance(tcfg, dict):
                raise ValueError("time is {column, since, until}")
            alias, name, tsql = col(tcfg.get("column"), what="time column")
            time_ref = (alias, name)
            kind_of = by_alias[alias].columns[name]
            ceiling = datetime.combine((as_of or (now or datetime.now()).date()) + timedelta(days=1), time())
            limit_days = MAX_MATCH_DAYS if match_columns else OPEN_WINDOW_DAYS
            if db.protected:
                limit_days = min(limit_days, PROTECTED_WINDOW_DAYS)
            until = min(parse_moment(tcfg.get("until"), end=True) or ceiling, ceiling)
            since = parse_moment(tcfg.get("since")) or until - timedelta(days=DEFAULT_DAYS if match_columns else 1)
            if since >= until:
                raise ValueError("since must be before until")
            if until - since > timedelta(days=limit_days):
                raise ValueError(f"the window may be at most {limit_days} day(s)"
                                 + ("" if match_columns else " without a match"))
            where.append(f"{tsql} >= {_moment(since, kind_of, db.engine)}")
            where.append(f"{tsql} < {_moment(until, kind_of, db.engine)}")
            time_sql = tsql
        else:
            time_sql = None
            if db.protected:
                raise ValueError(f"{db.name} is a live production database: give `time` (a date column and a window of at "
                                 f"most {PROTECTED_WINDOW_DAYS} days)")
            if not match_columns:
                raise ValueError("counts without a match need a time window: give `time`")
            if big:
                raise ValueError(f"{', '.join(big)} is a large table: give `time` (a date column and a window)")

        # Extra filters.
        filter_refs: list[tuple[str, str]] = []
        for f in request.get("where") or []:
            if not isinstance(f, dict):
                raise ValueError("each where item is {column, op, value}")
            alias, name, fsql = col(f.get("column"), what="filter column")
            filter_refs.append((alias, name))
            if hidden(name, by_alias[alias].columns[name]):
                raise ValueError(f"{alias}.{name} holds free text or a secret and cannot be filtered on")
            where.append(_condition(fsql, f, db.engine))

        # A large (or unmeasured) table answers quickly only on an indexed column: used in the window, the match or a
        # join. Without that the database scans millions of rows and times out, so say which columns to use.
        used: dict[str, set[str]] = {a: set() for a in order}
        for a, n in (join_refs + filter_refs + ([time_ref] if time_ref else [])
                     + [tuple(m.split(".", 1)) for m in match_columns]):
            used[a].add(n)
        for a in order:
            t = by_alias[a]
            if db.protected and not t.indexes:
                raise ValueError(f"{db.name} is a live production database and {t.key} has no known index: it cannot be "
                                 "read here")
            if t.indexed and (db.protected or t.rows is None or t.rows >= BIG_TABLE_ROWS) and not used[a] & set(t.indexed):
                raise ValueError(f"{t.key} is a large table and nothing in this query uses an index on it: filter, match, "
                                 f"join or window on the first column of one of its indexes, then the next ones "
                                 f"(indexes: {'; '.join(', '.join(ix) for ix in t.indexes[:4])})")
        limit = max(1, min(int(request.get("limit") or 10), MAX_ROWS))
        clause = " WHERE " + " AND ".join(where) if where else ""
        sources = [by_alias[a].key for a in order]
        if mode == "count":
            by = request.get("count_by") or []
            if not isinstance(by, list) or len(by) > 3:
                raise ValueError("count_by: up to three alias.column values")
            labels, parts = [], []
            for i, ref in enumerate(by):
                alias, name, sql = col(ref, what="count_by column")
                if IDENT_LIKE.search(name) or hidden(name, by_alias[alias].columns[name]):
                    raise ValueError(f"cannot count by {alias}.{name} (identifiers, free text and secrets are not allowed)")
                labels.append(f"{alias}.{name}")
                parts.append(f"{sql} AS c{i + 1}")
            parts.append("COUNT(*) AS n")
            if time_sql:
                parts += [f"MIN({time_sql}) AS first_seen", f"MAX({time_sql}) AS last_seen"]
            group = " GROUP BY " + ", ".join(f"c{i + 1}" if db.engine in ("postgres", "mysql") else p.rsplit(" AS ", 1)[0]
                                              for i, p in enumerate(parts[:len(by)])) if by else ""
            body = f"SELECT {', '.join(parts)} FROM {' '.join(from_parts)}{clause}{group}" + (" ORDER BY n DESC" if by else "")
        else:
            refs = request.get("columns") or []
            if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_SHOWN_COLUMNS:
                raise ValueError(f"columns: name 1 to {MAX_SHOWN_COLUMNS} alias.column values to show")
            labels, parts = [], []
            for i, ref in enumerate(refs):
                alias, name, sql = col(ref, what="column")
                if hidden(name, by_alias[alias].columns[name]):
                    raise ValueError(f"{alias}.{name} holds free text or a secret and cannot be shown")
                labels.append(f"{alias}.{name}")
                parts.append(f"{sql} AS c{i + 1}")
            body = f"SELECT {', '.join(parts)} FROM {' '.join(from_parts)}{clause}" + (
                f" ORDER BY {time_sql} DESC" if time_sql else "")
        return PreparedJoin(db, _limited(body, db.engine, limit + 1), mode, labels, sources, match_columns, kind,
                            since, until, limit)

    @staticmethod
    def _from(t: Table, alias: str, quote: Callable[[str], str]) -> str:
        return ".".join(quote(p) for p in t.sql_name) + f" {alias}"  # no AS: Oracle does not allow it for tables

    # ---- running it --------------------------------------------------------------------------------
    def run(self, prepared: PreparedJoin) -> dict[str, Any]:
        rows = self.mb.query(int(prepared.database.db_id), prepared.sql)
        more = len(rows) > prepared.limit
        names = [f"c{i + 1}" for i in range(len(prepared.labels))]
        shown = []
        for r in rows[:prepared.limit]:
            low = {str(k).lower(): v for k, v in r.items()}
            if prepared.mode == "count":
                row = {lab: _plain(low.get(f"c{i + 1}")) for i, lab in enumerate(prepared.labels)}
                row.update({k: _plain(low[k]) for k in ("n", "first_seen", "last_seen") if k in low})
            else:
                row = {lab: _plain(low.get(n)) for lab, n in zip(prepared.labels, names)}
            shown.append(row)
        if prepared.mode == "count" or not prepared.match_columns:  # other clients' data: no numbers at all
            shown = [{k: _scrub(v) for k, v in r.items()} for r in shown]
        out: dict[str, Any] = {"database": prepared.database.name, "tables": prepared.sources, "mode": prepared.mode,
                               "rows": shown, "rows_shown": len(shown), "more_rows": more}
        if prepared.match_columns:
            out["matched_on"] = {"columns": prepared.match_columns, "kind": prepared.kind}
        if prepared.since:
            out["window"] = {"from": prepared.since.isoformat(sep=" ", timespec="minutes"),
                             "to": prepared.until.isoformat(sep=" ", timespec="minutes") if prepared.until else None}
        if not shown:
            out["note"] = "Nothing found for this match and window."
        elif more:
            out["note"] = f"More rows exist: narrow the window or add filters (at most {prepared.limit} shown)."
        return out


def _moment(moment: datetime, kind: str, engine: str) -> str:
    """A time as a literal of the column's own type: Oracle compares a DATE column with a TIMESTAMP literal by converting
    the column, which stops an index on it being used, so a DATE column gets TO_DATE."""
    text = moment.strftime("%Y-%m-%d %H:%M:%S")
    if not temporal(kind) or engine == "mysql":
        return f"'{text}'"
    if engine == "sqlserver":
        return f"'{moment.strftime('%Y-%m-%dT%H:%M:%S')}'"
    if engine == "oracle" and kind.strip().lower() == "date":
        return f"TO_DATE('{text}', 'YYYY-MM-DD HH24:MI:SS')"
    return f"TIMESTAMP '{text}'"  # PostgreSQL, Oracle TIMESTAMP


def _condition(col: str, f: dict[str, Any], engine: str) -> str:
    op, value = str(f.get("op") or "").lower(), f.get("value")
    if op not in OPS:
        raise ValueError(f"op must be one of {', '.join(OPS)}")
    if op == "is empty":
        return f"({col} IS NULL OR {_as_text(col, engine)} = '')" if engine != "oracle" else f"{col} IS NULL"
    if op == "is not empty":
        return (f"({col} IS NOT NULL AND {_as_text(col, engine)} <> '')" if engine != "oracle"
                else f"{col} IS NOT NULL")
    values = value if isinstance(value, list) else [value]
    if op in ("in", "not in"):
        if not 1 <= len(values) <= 20:
            raise ValueError("in / not in take 1 to 20 values")
    elif isinstance(value, list) or value is None:
        raise ValueError(f"{op} takes one value")
    for v in values:
        if isinstance(v, (dict, list)) or v is None:
            raise ValueError("filter values are texts or numbers")
        if LONG_NUMBER_RE.search(str(v)):
            raise ValueError("account, mobile and reference numbers go in match.value, not in a filter")
    if op == "contains":
        fragment = re.sub(r"[%_]", "", str(value))[:60]
        return f"LOWER({_as_text(col, engine)}) LIKE {_text('%' + fragment.lower() + '%', engine)}"
    if op in ("in", "not in"):
        return f"{col} {op.upper()} ({', '.join(_text(str(v), engine) for v in values)})"
    return f"{col} {'<>' if op == '!=' else op} {_text(str(value), engine)}"


def run_tool(sq: SchemaQuery, name: str, request: dict[str, Any], identify: Identify,
             queries: dict[str, dict[str, Any]], as_of: Optional[date] = None) -> dict[str, Any]:
    """explore_data (read from the schema files, not recorded) or one query_tables call for a desk: built, run and
    recorded in `queries` under its query_id, like query_data. A result over other clients' data (counts) is marked
    `_no_follow_ups`."""
    if name == "explore_data":
        return sq.explore(request)
    prepared = sq.prepare(request, identify, as_of=as_of)
    out = sq.run(prepared)
    query_id = f"Q{len(queries) + 1}"
    queries[query_id] = {"source": f"{prepared.database.name}: {' + '.join(prepared.sources)}", "mode": prepared.mode,
                         "match_columns": prepared.match_columns, "kind": prepared.kind, "rows": out["rows_shown"],
                         "window": out.get("window")}
    out = {"query_id": query_id, **out}
    if prepared.mode == "count" or not prepared.match_columns:
        out["_no_follow_ups"] = True
    return out


def build_for_desk(mb: Any, enabled: bool = True, path: Optional[Path] = None) -> Optional[SchemaQuery]:
    """The desk's explore_data and query_tables, or None (switched off, or the schema files cannot be used)."""
    if not enabled:
        return None
    try:
        sq = SchemaQuery.from_directory(mb, path)
    except (OSError, ValueError, KeyError) as exc:
        log.warning("query_tables is off: the schema files could not be used (%s)", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - a Metabase error listing the databases
        log.warning("query_tables is off: %s", exc)
        return None
    return sq if sq.schema.databases else None
