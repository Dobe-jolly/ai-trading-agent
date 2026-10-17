"""query_data: the agents' general, read-only look into the bank's databases, over an approved catalogue.

The agent decides what to look at; this module decides how it is asked. The model never writes SQL: it names a
source from the catalogue (agentcore/data_catalogue.json), what to match on, a time window, filters and the columns
it wants, and the SQL is built here:

- only the databases, tables and columns the catalogue lists (no personal columns: the catalogue is checked on load);
- one table per query, read only (SELECT), at most 25 rows;
- matched on an identifier the ticket gives (account, mobile, reference/RRN, CIF), checked by the desk and expanded to
  every form the number may be stored in; or, without a match, counts only (a service-wide view: is everyone failing?)
  grouped by non-identifier columns over at most two days; or an "open" table that holds no client data;
- a time window on the table's time column, never longer than the table allows.

Values are written into the SQL as escaped literals (as agentcore.sql explains); names come only from the catalogue.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

from .metabase import MetabaseClient

log = logging.getLogger(__name__)

CATALOGUE_PATH = Path(__file__).with_name("data_catalogue.json")
KINDS = ("account", "mobile", "reference", "cif")
MAX_ROWS, DEFAULT_ROWS = 25, 10
OPEN_WINDOW_DAYS = 2  # counts without a match: a service-wide view over a short window
DEFAULT_DAYS = 7
OPS = ("=", "!=", "<", "<=", ">", ">=", "in", "not in", "contains", "is empty", "is not empty")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")
LONG_NUMBER_RE = re.compile(r"\d{8,}")  # account, mobile and RRN-like values only go in through `match`

# Personal columns, by name or by Metabase's semantic type (shared with tools/metabase_names.py --catalogue).
PERSONAL_NAME = re.compile(r"(?i)national|id_?no|id_?num|idnumber|passport|birth|(^|_)dob|mobile|phone|msisdn|"
                           r"(^|_)cell|e_?mail|addr|street|imei|(^|_)otp|(^|_)pin(_?(no|code))?$|password|passwd|secret|"
                           r"(^|_)(new_|old_)?pan$|card_?number|cvv|track_?\d")
NAMES = re.compile(r"(?i)name")  # a "name" column is a person's name unless it names a thing:
THING_NAME = re.compile(r"(?i)(table|column|schema|branch|bank|product|merchant|file|service|field|pgm|program|report|job|"
                        r"host|server|terminal|currency|country|db|database|object|class|type|category|limit|param|key|"
                        r"event|status|batch|queue|module|app|device|network|channel|institution|scheme|biller|sink|"
                        r"source|node|interface|process|task|provider)_?name")
PERSONAL_TYPES = {"type/Name", "type/Email", "type/Address", "type/Phone", "type/Birthdate", "type/City", "type/ZipCode"}


def personal(column: dict) -> bool:
    """Whether a column holds personal details, by its name or Metabase's semantic type."""
    name = str(column.get("name", ""))
    return bool(PERSONAL_NAME.search(name)) or (bool(NAMES.search(name)) and not THING_NAME.search(name)) \
        or column.get("semantic_type") in PERSONAL_TYPES


class CatalogueError(ValueError):
    pass


@dataclass
class Source:
    key: str
    database: str
    table: str
    about: str
    columns: list[str]
    identifiers: dict[str, str] = field(default_factory=dict)  # column -> kind
    desks: list[str] = field(default_factory=list)
    time_column: Optional[str] = None
    time_type: str = "timestamp"  # timestamp | text (ISO text, compared as text)
    max_days: int = 31
    order_by: Optional[str] = None
    computed: dict[str, str] = field(default_factory=dict)  # name -> SQL expression (written by the catalogue)
    open: bool = False
    # Identifiers reached through another table: name -> {column, table, key, lookup}: the row's `column` is the
    # `key` of the rows of `table` whose `lookup` matches (e.g. sms_message.recipient_id = sms_recipient.id where
    # sms_recipient.mobile_number = the ticket's mobile). The name is listed in identifiers with its kind.
    via: dict[str, dict[str, str]] = field(default_factory=dict)
    db_id: Optional[int] = None
    engine: str = "postgres"

    def describe(self) -> str:
        ids = ", ".join(f"{c} ({k})" for c, k in self.identifiers.items()) or "none: open table, no client data"
        when = f"{self.time_column}, at most {self.max_days} days" if self.time_column else "no time column"
        cols = ", ".join(self.columns + list(self.computed))
        return f"- {self.key} [{self.database}]: {self.about} Match on: {ids}. Time: {when}. Columns: {cols}."


def load_catalogue(path: Path = CATALOGUE_PATH) -> dict[str, Source]:
    """The approved sources, checked: names safe to quote, identifiers among the columns, no personal column shown
    (a mobile may be a match column; it is masked in results)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for key, raw in (data.get("sources") or {}).items():
        src = Source(key=key, database=raw["database"], table=raw["table"], about=raw.get("about", ""),
                     columns=list(raw.get("columns") or []), identifiers=dict(raw.get("identifiers") or {}),
                     desks=list(raw.get("desks") or []), order_by=raw.get("order_by"),
                     computed=dict(raw.get("computed") or {}), open=bool(raw.get("open")),
                     via={k: dict(v) for k, v in (raw.get("via") or {}).items()})
        if raw.get("time"):
            src.time_column, src.time_type = raw["time"]["column"], raw["time"].get("type", "timestamp")
            src.max_days = int(raw["time"].get("max_days", 31))
        problems = [p for p in src.table.split(".") if not NAME_RE.match(p)]
        problems += [c for c in src.columns + list(src.computed) if not NAME_RE.match(c)]
        problems += [f"{c} (identifier not in columns or via)" for c in src.identifiers
                     if c not in src.columns and c not in src.via]
        for name, v in src.via.items():
            if name not in src.identifiers or set(v) != {"column", "table", "key", "lookup"}:
                problems.append(f"via {name}: needs column, table, key and lookup, and a kind in identifiers")
                continue
            problems += [f"via {name}: {x}" for x in [v["column"], v["key"], v["lookup"], *v["table"].split(".")]
                         if not NAME_RE.match(x)]
        problems += [f"{c} (kind {k})" for c, k in src.identifiers.items() if k not in KINDS]
        problems += [f"{c} (personal)" for c in src.columns if personal({"name": c}) and src.identifiers.get(c) != "mobile"]
        for extra in (src.time_column, src.order_by):
            if extra and not NAME_RE.match(extra):
                problems.append(extra)
        if src.time_type not in ("timestamp", "text"):
            problems.append(f"time type {src.time_type}")
        if not src.identifiers and not src.open:
            problems.append("no identifiers and not open")
        if problems:
            raise CatalogueError(f"catalogue source {key}: " + ", ".join(problems))
        out[key] = src
    return out


def parse_moment(value: Any, end: bool = False) -> Optional[datetime]:
    """YYYY-MM-DD or YYYY-MM-DD HH:MM; a date alone means the start of that day (or, for `until`, its end)."""
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{text!r} is not a date (YYYY-MM-DD or YYYY-MM-DD HH:MM)") from None
    return datetime.combine(day + timedelta(days=1) if end else day, time())


@dataclass
class Prepared:
    source: Source
    sql: str
    mode: str  # rows | count
    match_columns: list[str]
    kind: Optional[str]
    since: Optional[datetime]
    until: Optional[datetime]
    limit: int


Identify = Callable[[str, str], list]  # (kind, value) -> the forms to match; raises ValueError when not allowed


class DataQuery:
    def __init__(self, mb: Any, sources: dict[str, Source], desk: Optional[str] = None) -> None:
        self.mb = mb
        self.sources = {k: s for k, s in sources.items() if desk is None or not s.desks or desk in s.desks}

    @classmethod
    def from_catalogue(cls, mb: MetabaseClient, desk: str, path: Path = CATALOGUE_PATH) -> "DataQuery":
        """Resolve each source's database by name; a source whose database is not in Metabase is left out (logged)."""
        sources = load_catalogue(path)
        engines = {str(d.get("name", "")).strip().lower(): d for d in mb.databases()}
        kept = {}
        for key, src in sources.items():
            db = engines.get(src.database.strip().lower())
            if db is None:
                log.warning("query_data: database %r of source %s is not in Metabase; left out", src.database, key)
                continue
            src.db_id, src.engine = int(db["id"]), str(db.get("engine") or "postgres")
            kept[key] = src
        return cls(mb, kept, desk)

    # ---- the tool --------------------------------------------------------------------------------
    def tool_schema(self) -> dict[str, Any]:
        listing = "\n".join(s.describe() for s in self.sources.values())
        return {
            "name": "query_data",
            "description": (
                "Read-only look into the bank's other databases, beyond the fixed lookups: middleware logs, biller "
                "responses, error logs, reversals, card requests, limits. Use it when the fixed lookups do not explain "
                "the ticket, or to see what a system recorded about this client's attempt. Each call reads one source: "
                "match it on an identifier the ticket gives (account, mobile, reference/RRN or CIF, as <NUMBER_1> "
                "etc.), within a time window; or, without a match, mode=count gives counts only (grouped by "
                "count_by), over at most two days, to see whether a service is failing for everyone. Results come "
                "back with a query_id (Q1, Q2, ...): put it at the start of the evidence item it supports (e.g. "
                "'Q2: ZESA purchase of 20.00 on ... failed: ...'). Your instructions say when a finding may close a "
                f"ticket. At most {MAX_ROWS} rows; newest first.\nSources:\n{listing}"),
            "input_schema": {"type": "object", "properties": {
                "source": {"type": "string", "enum": list(self.sources)},
                "match_column": {"type": "array", "items": {"type": "string"},
                                 "description": "Identifier column(s) of the source to match (one kind; any of them may match)."},
                "match_value": {"type": "string", "description": "The value from the ticket, e.g. <NUMBER_1>."},
                "since": {"type": "string", "description": "Start, YYYY-MM-DD or YYYY-MM-DD HH:MM."},
                "until": {"type": "string", "description": "End, YYYY-MM-DD (that whole day) or YYYY-MM-DD HH:MM."},
                "where": {"type": "array", "description": "Extra filters on the source's columns (not identifiers).",
                          "items": {"type": "object", "properties": {
                              "column": {"type": "string"}, "op": {"type": "string", "enum": list(OPS)},
                              "value": {"description": "A text or number; a list for in / not in."}},
                              "required": ["column", "op"]}},
                "columns": {"type": "array", "items": {"type": "string"}, "description": "Columns to show (default: all)."},
                "mode": {"type": "string", "enum": ["rows", "count"], "description": "rows (default) or count."},
                "count_by": {"type": "array", "items": {"type": "string"},
                             "description": "mode=count: the columns to count by (not identifiers)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
            }, "required": ["source"]},
        }

    # ---- building the query ----------------------------------------------------------------------
    def prepare(self, request: dict[str, Any], identify: Identify, now: Optional[datetime] = None,
                as_of: Optional[date] = None) -> Prepared:
        src = self.sources.get(str(request.get("source") or ""))
        if src is None:
            raise ValueError(f"source must be one of {', '.join(self.sources)}")
        mode = str(request.get("mode") or "rows")
        if mode not in ("rows", "count"):
            raise ValueError("mode must be rows or count")
        quote = _quoter(src.engine)
        where: list[str] = []

        # What to match on: an identifier the ticket gives, in every form it may be stored in.
        match_columns = request.get("match_column") or []
        match_columns = [match_columns] if isinstance(match_columns, str) else list(match_columns)
        kind = None
        if match_columns:
            unknown = [c for c in match_columns if c not in src.identifiers]
            if unknown:
                raise ValueError(f"{', '.join(unknown)}: not an identifier of {src.key} "
                                 f"({', '.join(src.identifiers) or 'it has none'})")
            kinds = {src.identifiers[c] for c in match_columns}
            if len(kinds) > 1:
                raise ValueError("match columns must hold one kind of value (all accounts, all mobiles, ...)")
            kind = kinds.pop()
            value = str(request.get("match_value") or "").strip()
            if not value:
                raise ValueError("match_value is required with match_column")
            forms = [f for f in identify(kind, value) if f]
            if not forms:
                raise ValueError(f"match_value is not a valid {kind}")
            literals = ", ".join(_text(f, src.engine) for f in forms)
            where.append("(" + " OR ".join(_match(src, c, literals, quote) for c in match_columns) + ")")
        elif request.get("match_value"):
            raise ValueError("match_value needs match_column")
        elif not src.open and mode != "count":
            raise ValueError(f"{src.key} holds client data: match it on {', '.join(src.identifiers)} with a value from "
                             "the ticket, or use mode=count for counts only")

        # The window on the table's time column.
        since, until = parse_moment(request.get("since")), parse_moment(request.get("until"), end=True)
        if src.time_column:
            ceiling = datetime.combine((as_of or (now or datetime.now()).date()) + timedelta(days=1), time())
            limit_days = src.max_days if (match_columns or src.open) else min(src.max_days, OPEN_WINDOW_DAYS)
            until = min(until or ceiling, ceiling)
            since = since or until - timedelta(days=min(limit_days, DEFAULT_DAYS if match_columns else 1))
            if since >= until:
                raise ValueError("since must be before until")
            if until - since > timedelta(days=limit_days):
                raise ValueError(f"the window may be at most {limit_days} day(s) on {src.key}"
                                 + ("" if match_columns or src.open else " without a match"))
            where.append(f"{quote(src.time_column)} >= {_moment(since, src)}")
            where.append(f"{quote(src.time_column)} < {_moment(until, src)}")
        elif since or until:
            raise ValueError(f"{src.key} has no time column: leave since and until out")
        elif not match_columns and not src.open:
            raise ValueError(f"{src.key} has no time column, so it can only be read matched on an identifier")

        # Filters on ordinary columns.
        for f in request.get("where") or []:
            where.append(self._filter(src, f, quote))

        limit = max(1, min(int(request.get("limit") or DEFAULT_ROWS), MAX_ROWS))
        table = ".".join(quote(p) for p in src.table.split("."))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        if mode == "count":
            by = list(request.get("count_by") or [])
            bad = [c for c in by if c not in src.columns or c in src.identifiers]
            if bad:
                raise ValueError(f"cannot count by {', '.join(bad)} (identifiers and unknown columns are not allowed)")
            parts = [quote(c) for c in by] + ["COUNT(*) AS n"]
            if src.time_column:
                parts += [f"MIN({quote(src.time_column)}) AS first_seen", f"MAX({quote(src.time_column)}) AS last_seen"]
            group = (" GROUP BY " + ", ".join(quote(c) for c in by)) if by else ""
            body = f"SELECT {', '.join(parts)} FROM {table}{clause}{group}" + (" ORDER BY n DESC" if by else "")
        else:
            wanted = list(request.get("columns") or (src.columns + list(src.computed)))
            bad = [c for c in wanted if c not in src.columns and c not in src.computed]
            if bad:
                raise ValueError(f"{', '.join(bad)}: not a column of {src.key}")
            parts = [f"{src.computed[c]} AS {quote(c)}" if c in src.computed else quote(c) for c in wanted]
            order = src.order_by or src.time_column
            body = f"SELECT {', '.join(parts)} FROM {table}{clause}" + (f" ORDER BY {quote(order)} DESC" if order else "")
        return Prepared(src, _limited(body, src.engine, limit + 1), mode, match_columns, kind, since, until, limit)

    @staticmethod
    def _filter(src: Source, f: Any, quote: Callable[[str], str]) -> str:
        if not isinstance(f, dict):
            raise ValueError("each where item is {column, op, value}")
        column, op, value = str(f.get("column") or ""), str(f.get("op") or "").lower(), f.get("value")
        if column in src.identifiers:
            raise ValueError(f"{column} is an identifier: use match_column and match_value for it")
        if column not in src.columns:
            raise ValueError(f"{column}: not a column of {src.key}")
        if op not in OPS:
            raise ValueError(f"op must be one of {', '.join(OPS)}")
        col = quote(column)
        if op == "is empty":
            return f"({col} IS NULL OR {_as_text(col, src.engine)} = '')" if src.engine != "oracle" else f"{col} IS NULL"
        if op == "is not empty":
            return (f"({col} IS NOT NULL AND {_as_text(col, src.engine)} <> '')" if src.engine != "oracle"
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
                raise ValueError("account, mobile and reference numbers go in match_value, not in a filter")
        if op == "contains":
            fragment = re.sub(r"[%_]", "", str(value))[:60]
            return f"LOWER({_as_text(col, src.engine)}) LIKE {_text('%' + fragment.lower() + '%', src.engine)}"
        if op in ("in", "not in"):
            return f"{col} {op.upper()} ({', '.join(_text(str(v), src.engine) for v in values)})"
        return f"{col} {'<>' if op == '!=' else op} {_text(str(value), src.engine)}"

    def probe(self, src: Source) -> Optional[str]:
        """Whether the source's table and columns exist as the catalogue says (no rows are read): None, or the error."""
        quote = _quoter(src.engine)
        parts = [quote(c) for c in src.columns] + [f"{e} AS {quote(c)}" for c, e in src.computed.items()]
        table = ".".join(quote(p) for p in src.table.split("."))
        try:
            self.mb.query(int(src.db_id), f"SELECT {', '.join(parts)} FROM {table} WHERE 1 = 0")
        except Exception as exc:  # noqa: BLE001 - reported per source by the check command
            return str(exc)[:200]
        return None

    # ---- running it ------------------------------------------------------------------------------
    def run(self, prepared: Prepared) -> dict[str, Any]:
        src = prepared.source
        rows = self.mb.query(int(src.db_id), prepared.sql) if src.db_id is not None else []
        more = len(rows) > prepared.limit
        shown = [{str(k).lower(): _plain(v) for k, v in r.items()} for r in rows[:prepared.limit]]
        if prepared.mode == "count" or not prepared.match_columns:  # other clients' data: no numbers at all
            shown = [{k: _scrub(v) for k, v in r.items()} for r in shown]
        out: dict[str, Any] = {"source": src.key, "mode": prepared.mode, "rows": shown, "rows_shown": len(shown),
                               "more_rows": more}
        if prepared.match_columns:
            out["matched_on"] = {"columns": prepared.match_columns, "kind": prepared.kind}
        if prepared.since:
            out["window"] = {"from": prepared.since.isoformat(sep=" ", timespec="minutes"),
                             "to": prepared.until.isoformat(sep=" ", timespec="minutes") if prepared.until else None}
        if not shown:
            out["note"] = "Nothing found in this source for this match and window."
        elif more:
            out["note"] = f"More rows exist: narrow the window or add filters (at most {prepared.limit} shown)."
        return out


def _match(src: Source, name: str, literals: str, quote: Callable[[str], str]) -> str:
    """The condition for one identifier: the column itself, or the column through its lookup table."""
    v = src.via.get(name)
    if v is None:
        return f"{quote(name)} IN ({literals})"
    table = ".".join(quote(p) for p in v["table"].split("."))
    return f"{quote(v['column'])} IN (SELECT {quote(v['key'])} FROM {table} WHERE {quote(v['lookup'])} IN ({literals}))"


def _scrub(value: Any) -> Any:
    """Long numbers in a value from a count or an open table (another client's account or mobile in an error text)."""
    return LONG_NUMBER_RE.sub("[number]", value) if isinstance(value, str) else value


def identifier_forms(kind: str, raw: str) -> list[str]:
    """The forms a ticket's value may be stored in: an account as given, without leading zeros and zero-padded to
    14; a mobile as 2637..., 07... and 7...; a reference as given and in capitals; a CIF as given and without
    leading zeros. [] when the value is not one of these."""
    text = str(raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if kind == "account":
        key = digits.lstrip("0")
        return sorted({digits, key, key.zfill(14)}) if 6 <= len(key) <= 20 else []
    if kind == "mobile":
        nine = digits[3:] if digits.startswith("263") else digits.lstrip("0")
        return ["263" + nine, "0" + nine, nine] if len(nine) == 9 and nine.startswith("7") else []
    if kind == "reference":
        compact = re.sub(r"[\s-]", "", text)
        return sorted({compact, compact.upper()}) if re.fullmatch(r"[A-Za-z0-9]{6,32}", compact) else []
    if kind == "cif":
        key = digits.lstrip("0")
        return sorted({digits, key}) if text.isdigit() and 4 <= len(key) <= 12 else []
    return []


def run_tool(data: "DataQuery", request: dict[str, Any], identify: Identify, queries: dict[str, dict[str, Any]],
             as_of: Optional[date] = None) -> dict[str, Any]:
    """One query_data call for a desk: built, run, and recorded in `queries` under its query_id (Q1, Q2, ...). A
    result over other clients' data (counts, open tables) is marked `_no_follow_ups`: the desk must not let numbers
    in it be searched with."""
    prepared = data.prepare(request, identify, as_of=as_of)
    out = data.run(prepared)
    query_id = f"Q{len(queries) + 1}"
    queries[query_id] = {"source": prepared.source.key, "mode": prepared.mode, "match_columns": prepared.match_columns,
                         "kind": prepared.kind, "rows": out["rows_shown"], "window": out.get("window")}
    out = {"query_id": query_id, **out}
    if prepared.mode == "count" or not prepared.match_columns:
        out["_no_follow_ups"] = True
    return out


def build_for_desk(mb: Any, desk: str, enabled: bool = True, path: Optional[Path] = None) -> Optional["DataQuery"]:
    """The desk's query_data, or None (switched off, or the catalogue cannot be used: logged, the desk runs on)."""
    if not enabled:
        return None
    try:
        data = DataQuery.from_catalogue(mb, desk, **({"path": path} if path else {}))
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        log.warning("query_data is off for %s: the data catalogue could not be used (%s)", desk, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - a Metabase error listing the databases
        log.warning("query_data is off for %s: %s", desk, exc)
        return None
    return data if data.sources else None


def check_lines(data: Optional["DataQuery"], enabled: bool = True) -> list[str]:
    """What the check command prints: each source's table and columns tried without reading rows."""
    if data is None:
        return ["[query_data] off" + ("" if enabled else " (QUERY_DATA=false)")]
    out = []
    for key, src in data.sources.items():
        problem = data.probe(src)
        out.append(f"[query_data] {key} ({src.database} {src.table}): " + ("ok" if problem is None
                                                                          else f"NOT USABLE: {problem}"))
    return out


def _quoter(engine: str) -> Callable[[str], str]:
    if engine == "mysql":
        return lambda n: f"`{n}`"
    if engine == "sqlserver":
        return lambda n: f"[{n}]"
    return lambda n: f'"{n}"'


def _as_text(column: str, engine: str) -> str:
    return f"CAST({column} AS CHAR)" if engine == "mysql" else f"CAST({column} AS VARCHAR(4000))"


def _text(value: str, engine: str) -> str:
    value = str(value)
    if "\x00" in value or len(value) > 120:
        raise ValueError("filter values are at most 120 characters")
    if engine == "mysql":
        value = value.replace("\\", "\\\\")
    return "'" + value.replace("'", "''") + "'"


def _moment(moment: datetime, src: Source) -> str:
    text = moment.strftime("%Y-%m-%d %H:%M:%S")
    if src.time_type == "text" or src.engine == "mysql":
        return f"'{text}'"
    if src.engine == "sqlserver":
        return f"'{moment.strftime('%Y-%m-%dT%H:%M:%S')}'"
    return f"TIMESTAMP '{text}'"  # PostgreSQL, Oracle


def _limited(body: str, engine: str, n: int) -> str:
    if engine == "sqlserver":
        return body.replace("SELECT ", f"SELECT TOP {int(n)} ", 1)
    if engine == "oracle":
        return f"SELECT * FROM ({body}) WHERE ROWNUM <= {int(n)}"
    return f"{body} LIMIT {int(n)}"


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "..."
    return value
