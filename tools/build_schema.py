"""Build the schema catalogue the agents explore: agentcore/schema/<database>.json, one file per approved database.

    python tools/build_schema.py                       # from reports/catalogue (tools/metabase_names.py --catalogue)
    python tools/build_schema.py --oracle              # also read the Oracle schemas through STANDBY DB (names and types)
    python tools/build_schema.py --indexes             # also record each table's indexed columns (a few queries per database)
    python tools/build_schema.py --from other/dir --out other/schema

Tables, columns and types only: no rows are read. A column that holds personal details (by name or Metabase's
semantic type, as marked in the listing) is left out of the file, so the agents cannot name it; the exception is a
mobile-number column, kept in `match_only` so a ticket's mobile can be matched on it (it is never shown).

Every database the Metabase key can see is written. The five Oracle connections (Intellect CBS, STANDBY DB,
Intellect LMS, Intellect_netguard, Backup) reach one logical standby, so they are one file with aliases. The live
Postilion realtime databases are marked protected: query_tables reads them only on an indexed column and a short
window (a heavy read there is felt by the switch).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentcore.dataquery import NAME_RE, personal  # noqa: E402

# Every database Metabase can reach. The Oracle connections are one logical standby (checked: same instance, host and
# role), so STANDBY DB carries the others as aliases.
APPROVED = [
    "IB", "CONECT 2.0", "CONECT 2.0 CORPORATE", "CONECT 2.0 REG", "BILLERS", "Postilion Billers", "connectorservices",
    "VBS TRANSACTIONS", "POST OFFICE", "OLD POST OFFICE", "NMBBIDW", "Smartstream", "SMS", "SOCKET SMS DB",
    "Socket Wallet DB", "Ecocash erros", "Ecocash Reg", "intellect_errors", "Account cache", "Agency Banking",
    "AGENCY BANKING V2", "Batch Processing", "CARD ISSUANCE", "TRANSPORTER.", "RBZ_INTEGRATION",
    "Xplug Socket Notifications DB", "OSTICKETS_NEW", "PEX", "Postilion Realtime", "Realtime 144", "STANDBY DB",
]
ALIASES = {"STANDBY DB": ["Intellect CBS", "Intellect LMS", "Intellect_netguard", "Backup"]}
PROTECTED = {"Postilion Realtime", "Realtime 144"}  # the live Postilion switch database (and its restore copy)
ORACLE_OWNERS = ("NMBPRODIDC", "NMBPRODLMS")  # core banking and the loans system
ORACLE_NAME_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$#"
MOBILE = re.compile(r"(?i)msisdn|mobile|phone|(^|_)cell")
LINE = re.compile(r"^(?P<table>[^\s:\[{]+)(?:\s+\[rows ~(?P<rows>\d+)\])?(?:\s+\{(?P<about>.*?)\})?: (?P<cols>.*)$")


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()


def split_columns(text: str) -> list[tuple[str, str, bool]]:
    out = []
    for part in text.split(", "):
        flagged = part.endswith(" [personal]")
        part = part.removesuffix(" [personal]").strip()
        name, _, kind = part.partition(" ")
        if name:
            out.append((name, kind.strip(), flagged))
    return out


def table_entry(rows, about, columns: list[tuple[str, str, bool]]) -> dict:
    keep, match_only, dropped = {}, [], 0
    for name, kind, flagged in columns:
        if not NAME_RE.match(name):
            continue
        if flagged or personal({"name": name}):
            if MOBILE.search(name):
                match_only.append(name)
            else:
                dropped += 1
            continue
        keep[name] = kind.lower()
    entry: dict = {"columns": keep}
    if rows is not None:
        entry["rows"] = int(rows)
    if about:
        entry["about"] = about[:160]
    if match_only:
        entry["match_only"] = match_only
    if dropped:
        entry["personal_left_out"] = dropped
    return entry


def from_listing(path: Path) -> tuple[str, str, dict]:
    head = path.read_text(encoding="utf-8").splitlines()[0]
    m = re.match(r"# (?P<name>.*) \(id (?P<id>\d+), (?P<engine>\w+)\)", head)
    if not m:
        raise ValueError(f"{path.name}: not a catalogue listing")
    tables = {}
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        t = LINE.match(line)
        if not t or t["cols"].startswith("(columns not synced"):
            continue
        parts = t["table"].split(".", 1)
        if len(parts) != 2 or not all(NAME_RE.match(p) or p == "-" for p in parts):
            continue
        key = parts[1] if parts[0] == "-" else t["table"]  # MySQL has no schema: the bare table name
        tables[key] = table_entry(t["rows"], t["about"], split_columns(t["cols"]))
    return m["name"], m["engine"], tables


def _read_columns(rows, owner: str, names: list[str], gathered: dict[str, dict[str, str]]) -> None:
    listing = ", ".join(f"'{n}'" for n in names)
    for r in rows("SELECT table_name, column_name, data_type FROM all_tab_columns WHERE owner = "
                  f"'{owner}' AND table_name IN ({listing}) ORDER BY table_name, column_id"):
        gathered.setdefault(r["table_name"], {}).setdefault(r["column_name"], str(r["data_type"] or ""))


def oracle_tables(mb, db_id: int) -> dict:
    """Core banking / LMS schemas through the standby connection: columns for ORACLE_OWNERS (paged: Metabase returns
    at most 2000 rows a query). Only tables whose columns the login can see are listed: the others cannot be read."""
    from agentcore.dataquery import identifier_forms  # noqa: F401 - keeps the import graph honest for the tool
    out: dict = {}

    def rows(sql: str) -> list[dict]:
        return [{k.lower(): v for k, v in r.items()} for r in mb.query(db_id, sql)]

    owners = rows("SELECT owner, COUNT(*) n FROM all_tables WHERE owner NOT IN ('SYS','SYSTEM','XDB','MDSYS','CTXSYS',"
                  "'OLAPSYS','WMSYS','ORDSYS','DBSNMP','OUTLN','APPQOSSYS','AUDSYS','GSMADMIN_INTERNAL','LBACSYS',"
                  "'OJVMSYS','DVSYS') GROUP BY owner")
    counts = {}
    for owner in (o["owner"] for o in owners):
        for r in rows(f"SELECT table_name, num_rows FROM all_tables WHERE owner = '{owner}' AND ROWNUM <= 20000"):
            counts[(owner, r["table_name"])] = r["num_rows"]
    for owner in sorted({o for o, _ in counts}):
        if owner not in ORACLE_OWNERS:
            continue  # the other schemas hold tables our login cannot read: nothing to explore or query there
        gathered: dict[str, dict[str, str]] = {}
        # 1. the readable tables and how many columns each has (grouped, so a few thousand small rows, paged by first character)
        sizes: dict[str, int] = {}
        for first in ORACLE_NAME_CHARS:
            for r in rows("SELECT table_name, COUNT(*) AS n FROM all_tab_columns WHERE owner = "
                          f"'{owner}' AND table_name LIKE '{first}%' GROUP BY table_name"):
                sizes[r["table_name"]] = int(r["n"])
        # 2. their columns, in batches that stay under Metabase's 2000-row cap (a table wider than that is read alone)
        batch, load = [], 0
        for name in sorted(sizes):
            if not NAME_RE.match(name):
                continue
            if batch and load + sizes[name] > 1900:
                _read_columns(rows, owner, batch, gathered)
                batch, load = [], 0
            batch.append(name)
            load += sizes[name]
        if batch:
            _read_columns(rows, owner, batch, gathered)
        for table, cols in gathered.items():
            if NAME_RE.match(table):
                out[f"{owner}.{table}"] = table_entry(counts.get((owner, table)), "",
                                                      [(c, k, False) for c, k in cols.items()])
        print(f"  {owner}: {len(gathered)} tables with columns")
    return out


INDEX_SQL = {
    # one row per index column: table, schema, index, column, position
    "mysql": "SELECT table_name AS tbl, NULL AS sch, index_name AS idx, column_name AS col, seq_in_index AS pos "
             "FROM information_schema.statistics WHERE table_schema = DATABASE()",
    "postgres": "SELECT t.relname AS tbl, n.nspname AS sch, i.indexrelid::text AS idx, a.attname AS col, k.ord AS pos "
                "FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
                "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) "
                "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
                "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND k.attnum > 0",
    "sqlserver": "SELECT t.name AS tbl, s.name AS sch, i.name AS idx, c.name AS col, ic.key_ordinal AS pos FROM sys.indexes i "
                 "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.key_ordinal > 0 "
                 "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
                 "JOIN sys.tables t ON t.object_id = i.object_id JOIN sys.schemas s ON s.schema_id = t.schema_id",
}
MAX_INDEXES, MAX_INDEX_COLUMNS = 8, 6


def read_indexes(mb, db_id: int, engine: str, owners: tuple[str, ...] = ()) -> dict[str, list[list[str]]]:
    """table key -> its indexes, each the columns in index order (names only). Oracle: per owner, paged by table name."""
    parts: dict[str, dict[str, dict[int, str]]] = {}  # key -> index -> position -> column

    def add(r: dict) -> None:
        r = {k.lower(): v for k, v in r.items()}
        key = str(r["tbl"]) if engine == "mysql" else f"{r.get('sch')}.{r['tbl']}"
        parts.setdefault(key, {}).setdefault(str(r["idx"]), {})[int(r["pos"])] = str(r["col"])

    def paged(sql_for_prefix, prefix: str = "") -> None:
        batch = mb.query(db_id, sql_for_prefix(prefix))
        if len(batch) >= 1990 and len(prefix) < 3:  # Metabase's 2000-row cap: split this prefix by the next character
            for c in ORACLE_NAME_CHARS:
                paged(sql_for_prefix, prefix + c)
            return
        for r in batch:
            add(r)

    if engine == "oracle":
        for owner in owners or ORACLE_OWNERS:
            paged(lambda pre, o=owner: "SELECT table_name AS tbl, table_owner AS sch, index_name AS idx, "
                                       "column_name AS col, column_position AS pos FROM all_ind_columns WHERE "
                                       f"table_owner = '{o}' AND table_name LIKE '{pre}%'")
    elif engine in INDEX_SQL:
        for r in mb.query(db_id, INDEX_SQL[engine]):
            add(r)
    out: dict[str, list[list[str]]] = {}
    for key, indexes in parts.items():
        cols = [[c for _, c in sorted(ix.items())][:MAX_INDEX_COLUMNS] for ix in indexes.values()]
        out[key] = sorted(cols, key=lambda c: (len(c), c))[:MAX_INDEXES]
    return out


def add_indexes(mb, out_dir: Path) -> None:
    """Write each table's indexes (columns in order) into the schema files, so the agent can choose columns a large
    table answers quickly on (an unindexed scan of a table of millions of rows times out)."""
    present = {str(d.get("name", "")).strip().lower(): d for d in mb.databases()}
    for file in sorted(out_dir.glob("*.json")):
        raw = json.loads(file.read_text(encoding="utf-8"))
        db = present.get(str(raw["database"]).strip().lower())
        if db is None:
            continue
        try:
            owners = tuple(sorted({k.split(".")[0] for k in raw["tables"] if "." in k}))  # Oracle: the schemas in this file
            found = read_indexes(mb, int(db["id"]), str(db.get("engine") or raw["engine"]), owners)
        except Exception as exc:  # noqa: BLE001 - one database's index catalogue unreadable: leave it without
            print(f"{raw['database']}: indexes not read ({str(exc)[:80]})")
            continue
        n = 0
        for key, t in raw["tables"].items():
            t.pop("indexed", None)
            t.pop("indexes", None)
            hit = [[c for c in ix if c in t["columns"]] for ix in found.get(key, [])]
            hit = [ix for ix in hit if ix and ix[0] in t["columns"]]
            if hit:
                t["indexes"] = hit
                n += 1
        file.write_text(json.dumps(raw, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        print(f"{raw['database']}: indexes for {n} of {len(raw['tables'])} tables")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="src", default=str(ROOT / "reports" / "catalogue"))
    parser.add_argument("--out", default=str(ROOT / "agentcore" / "schema"))
    parser.add_argument("--oracle", action="store_true", help="read the Oracle schemas through STANDBY DB")
    parser.add_argument("--indexes", action="store_true", help="add each table's indexed columns to the files")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    args = parser.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = {n.strip().lower(): n for n in APPROVED}
    done = set()
    for path in sorted(Path(args.src).glob("*.txt")):
        if path.name == "index.txt":
            continue
        try:
            name, engine, tables = from_listing(path)
        except ValueError:
            continue
        if name.strip().lower() not in wanted or name.strip().lower() == "standby db" or not tables:
            continue
        doc = {"database": name.strip(), "engine": engine, "tables": tables}
        if name.strip() in PROTECTED:
            doc["protected"] = True
        (out_dir / f"{slug(name)}.json").write_text(json.dumps(doc, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        done.add(name.strip().lower())
        print(f"{name}: {len(tables)} tables, {sum(len(t['columns']) for t in tables.values())} columns")
    if args.oracle:
        from dotenv import load_dotenv
        from agentcore.metabase import MetabaseClient
        load_dotenv(args.env_file, override=False)
        mb = MetabaseClient(os.environ["METABASE_HOST"], os.environ["METABASE_API_KEY"], timeout=120, retry_count=2,
                            verify=os.environ.get("METABASE_VERIFY_TLS", "true").lower() not in ("false", "0", "no"))
        db_id = mb.resolve(os.environ.get("STANDBY_DB_NAME") or "STANDBY DB")
        tables = oracle_tables(mb, db_id)
        (out_dir / "standby_db.json").write_text(json.dumps(
            {"database": "STANDBY DB", "engine": "oracle", "aliases": ALIASES["STANDBY DB"], "tables": tables},
            separators=(",", ":"), sort_keys=True), encoding="utf-8")
        done.add("standby db")
        print(f"STANDBY DB: {len(tables)} tables")
    if args.indexes:
        from dotenv import load_dotenv
        from agentcore.metabase import MetabaseClient
        load_dotenv(args.env_file, override=False)
        mb = MetabaseClient(os.environ["METABASE_HOST"], os.environ["METABASE_API_KEY"], timeout=120, retry_count=2,
                            verify=os.environ.get("METABASE_VERIFY_TLS", "true").lower() not in ("false", "0", "no"))
        add_indexes(mb, out_dir)
    missing = [n for n in APPROVED if n.strip().lower() not in done]
    if missing:
        print("not written (no listing found): " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
