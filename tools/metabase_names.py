"""List what the agents' Metabase key can see, to fill the *_DB_NAME and table settings in .env.

    python tools/metabase_names.py                            # every database: id, name, engine
    python tools/metabase_names.py --tables "CONECT 2.0 REG"  # one database's tables
    python tools/metabase_names.py --find eftc_ecocash        # which databases have a table with this in its name
    python tools/metabase_names.py --columns "Postilion Realtime" --table post_tran --table post_tran_leg
    python tools/metabase_names.py --columns "Postilion Realtime" --like rsp --like response --out reports/cols.txt
    python tools/metabase_names.py --catalogue reports/catalogue   # every database, table and column, one file each

Tables and columns are read from the database's own catalogue (information_schema, or ALL_TABLES and
ALL_TAB_COLUMNS on Oracle), so what Metabase has not synced is listed too. Names and types only: no rows are read.

--catalogue writes what Metabase has synced for every database (one call per database, no row cap): an index and one
file per database with a line per table and its columns. Columns that hold personal details (names, ID numbers,
phones, e-mails, addresses, by name or by Metabase's semantic type) are marked [personal], so the agents are never
given them.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from agentcore.dataquery import personal  # noqa: E402  (one definition of a personal column, shared with query_data)
from agentcore.metabase import MetabaseClient, MetabaseError  # noqa: E402

ORACLE_SYSTEM = ("'SYS', 'SYSTEM', 'XDB', 'MDSYS', 'CTXSYS', 'OLAPSYS', 'WMSYS', 'ORDSYS', 'DBSNMP', 'OUTLN', "
                 "'APPQOSSYS', 'AUDSYS', 'GSMADMIN_INTERNAL', 'LBACSYS', 'OJVMSYS', 'DVSYS'")
OTHER_SYSTEM = "('pg_catalog', 'information_schema', 'sys', 'mysql', 'performance_schema')"
CATALOGUE = {
    "oracle": f"SELECT owner AS table_schema, table_name FROM all_tables WHERE owner NOT IN ({ORACLE_SYSTEM}) "
              "AND ROWNUM <= 5000 ORDER BY owner, table_name",
    "default": "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN "
               f"{OTHER_SYSTEM} ORDER BY table_schema, table_name",
}
COLUMNS = {
    "oracle": ("SELECT owner AS table_schema, table_name, column_name, data_type, data_length AS max_len "
               f"FROM all_tab_columns WHERE owner NOT IN ({ORACLE_SYSTEM})", "ORDER BY owner, table_name, column_id"),
    "default": ("SELECT TABLE_SCHEMA AS table_schema, TABLE_NAME AS table_name, COLUMN_NAME AS column_name, "
                "DATA_TYPE AS data_type, CHARACTER_MAXIMUM_LENGTH AS max_len FROM information_schema.columns "
                f"WHERE TABLE_SCHEMA NOT IN {OTHER_SYSTEM}", "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"),
}
ROW_CAP = 2000  # Metabase returns at most this many rows to /api/dataset


def _get(row: dict, key: str):
    return row.get(key, row.get(key.upper()))


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def tables(mb: MetabaseClient, db: dict) -> list[tuple[str, str]]:
    sql = CATALOGUE.get(str(db.get("engine")), CATALOGUE["default"])
    try:
        rows = mb.query(int(db["id"]), sql)
    except MetabaseError:
        rows = []
    if rows:
        return [(str(_get(r, "table_schema") or "-"), str(_get(r, "table_name"))) for r in rows]
    meta = mb._request("GET", f"/api/database/{db['id']}/metadata")  # fall back to what Metabase has synced
    return sorted((str(t.get("schema") or "-"), str(t.get("name"))) for t in meta.get("tables", []))


def columns_sql(engine: str, table_names: list[str], likes: list[str]) -> str:
    select, order = COLUMNS.get(engine, COLUMNS["default"])
    column, table = ("column_name", "table_name") if engine == "oracle" else ("COLUMN_NAME", "TABLE_NAME")
    where = []
    if table_names:
        where.append(f"LOWER({table}) IN ({', '.join(_quote(t.lower()) for t in table_names)})")
    if likes:
        where.append("(" + " OR ".join(f"LOWER({column}) LIKE {_quote('%' + s.lower() + '%')}" for s in likes) + ")")
    return " ".join([select] + [f"AND {w}" for w in where] + [order])


def columns(mb: MetabaseClient, db: dict, table_names: list[str], likes: list[str]
            ) -> list[tuple[str, str, str, str]]:
    """(schema.table, column, type, max length) for the tables and name fragments asked for."""
    try:
        rows = mb.query(int(db["id"]), columns_sql(str(db.get("engine")), table_names, likes))
    except MetabaseError:
        rows = []
    if rows:
        return [(f"{_get(r, 'table_schema') or '-'}.{_get(r, 'table_name')}", str(_get(r, "column_name")),
                 str(_get(r, "data_type") or ""), "" if _get(r, "max_len") is None else str(_get(r, "max_len")))
                for r in rows]
    meta = mb._request("GET", f"/api/database/{db['id']}/metadata")  # fall back to what Metabase has synced
    wanted, found = {t.lower() for t in table_names}, []
    for t in meta.get("tables", []):
        if wanted and str(t.get("name", "")).lower() not in wanted:
            continue
        for f in t.get("fields", []):
            name = str(f.get("name", ""))
            if likes and not any(s.lower() in name.lower() for s in likes):
                continue
            found.append((f"{t.get('schema') or '-'}.{t.get('name')}", name, str(f.get("database_type") or ""), ""))
    return found


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower() or "database"


def catalogue(mb: MetabaseClient, databases: list[dict], out_dir: Path, say) -> None:
    """One file per database (tables and columns Metabase has synced, names and types only) and an index."""
    out_dir.mkdir(parents=True, exist_ok=True)
    index = ["id     database  [engine]  tables  columns  personal-columns  file"]
    for db in databases:
        if db.get("engine") == "h2":  # Metabase's own sample database
            continue
        name = str(db.get("name"))
        try:
            meta = mb._request("GET", f"/api/database/{db['id']}/metadata?include_hidden=true")
            synced = meta.get("tables", []) if isinstance(meta, dict) else []
        except MetabaseError as exc:  # large Oracle databases time out here: their table names from the catalogue
            log_error = str(exc)[:80]
            try:
                names = [(s, t) for s, t in tables(mb, db)]
            except MetabaseError:
                names = []
            if not names:
                index.append(f"{db.get('id'):>5}  {name}  [{db.get('engine')}]  not readable: {log_error}")
                say(f"{name}: not readable")
                continue
            file = out_dir / f"{db.get('id')}-{_slug(name)}.txt"
            file.write_text("\n".join([f"# {name} (id {db.get('id')}, {db.get('engine')}): table names only "
                                       f"(Metabase's metadata timed out; use --columns for a table's columns)", ""]
                                      + [f"{s}.{t}" for s, t in names]) + "\n", encoding="utf-8")
            index.append(f"{db.get('id'):>5}  {name}  [{db.get('engine')}]  {len(names)}  -  -  {file.name}")
            say(f"{name}: {len(names)} table name(s) (metadata timed out)")
            continue
        lines, n_cols, n_personal = [f"# {name} (id {db.get('id')}, {db.get('engine')})",
                                     "# schema.table  [rows ~N]  {description}: column type, ... ([personal] = never "
                                     "given to the agents)", ""], 0, 0
        for t in sorted(synced, key=lambda t: (str(t.get("schema") or ""), str(t.get("name") or ""))):
            cols = []
            for f in sorted(t.get("fields", []), key=lambda f: f.get("position") or 0):
                mark = " [personal]" if personal(f) else ""
                n_personal += bool(mark)
                cols.append(f"{f.get('name')} {f.get('database_type') or f.get('base_type') or ''}{mark}".strip())
            n_cols += len(cols)
            rows = t.get("estimated_row_count")
            about = (t.get("description") or "").replace("\n", " ").strip()
            lines.append(f"{t.get('schema') or '-'}.{t.get('name')}" + (f"  [rows ~{rows}]" if rows is not None else "")
                         + (f"  {{{about[:160]}}}" if about else "") + ": " + ", ".join(cols))
        if not synced:  # nothing synced: at least the table names from the database's own catalogue
            names = tables(mb, db)
            lines += [f"{schema}.{table}: (columns not synced in Metabase)" for schema, table in names]
            synced = names
        file = out_dir / f"{db.get('id')}-{_slug(name)}.txt"
        file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        index.append(f"{db.get('id'):>5}  {name}  [{db.get('engine')}]  {len(synced)}  {n_cols}  {n_personal}  {file.name}")
        say(f"{name}: {len(synced)} table(s), {n_cols} column(s)")
    (out_dir / "index.txt").write_text("\n".join(index) + "\n", encoding="utf-8")
    say(f"Index written to {out_dir / 'index.txt'}")


def find_database(databases: list[dict], name: str) -> Optional[dict]:
    wanted = name.strip().lower()
    return next((d for d in databases if str(d.get("name", "")).strip().lower() == wanted), None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tables", metavar="DATABASE", help="list this database's tables")
    group.add_argument("--find", metavar="TEXT", help="find tables whose name contains this, in every database")
    group.add_argument("--columns", metavar="DATABASE", help="list columns in this database (use --table / --like)")
    group.add_argument("--catalogue", metavar="DIR", help="write every database's tables and columns to this folder")
    parser.add_argument("--table", action="append", default=[], help="with --columns: only this table (repeatable)")
    parser.add_argument("--like", action="append", default=[],
                        help="with --columns: only columns whose name contains this (repeatable)")
    parser.add_argument("--out", metavar="FILE", help="also write the listing to this file (UTF-8)")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    args = parser.parse_args(argv)
    if (args.table or args.like) and not args.columns:
        parser.error("--table and --like go with --columns")
    load_dotenv(args.env_file, override=False)
    host, key = os.environ.get("METABASE_HOST"), os.environ.get("METABASE_API_KEY")
    if not host or not key:
        print("METABASE_HOST and METABASE_API_KEY must be set in .env")
        return 1
    lines: list[str] = []

    def say(text: str) -> None:
        print(text)
        lines.append(text)

    mb = MetabaseClient(host, key, retry_count=1)
    try:
        databases = sorted(mb.databases(), key=lambda d: str(d.get("name", "")).lower())
        if args.tables or args.columns:
            db = find_database(databases, args.tables or args.columns)
            if db is None:
                print(f"No database named {(args.tables or args.columns)!r}")
                return 1
        if args.tables:
            found = tables(mb, db)
            for schema, name in found:
                say(f"{schema}.{name}")
            say(f"{len(found)} table(s) in {db.get('name')!r}")
        elif args.columns:
            found = columns(mb, db, args.table, args.like)
            width = max((len(c) for _, c, _, _ in found), default=10)
            current = None
            for table, column, kind, size in found:
                if table != current:
                    say(f"\n{table}")
                    current = table
                say(f"  {column:<{width}}  {kind}{f'({size})' if size and size != '-1' else ''}")
            tables_seen = len({t for t, _, _, _ in found})
            say(f"\n{len(found)} column(s) in {tables_seen} table(s) of {db.get('name')!r}"
                + (f" (Metabase stops at {ROW_CAP} rows: narrow with --table or --like)" if len(found) >= ROW_CAP else ""))
            missing = sorted({t.lower() for t in args.table} - {t.split(".")[-1].lower() for t, _, _, _ in found})
            if missing and not args.like:
                say(f"Not found: {', '.join(missing)}")
        elif args.catalogue:
            catalogue(mb, databases, Path(args.catalogue), say)
        elif args.find:
            needle = args.find.lower()
            hits = 0
            for db in databases:
                if db.get("engine") == "h2":
                    continue
                try:
                    found = [(s, n) for s, n in tables(mb, db) if needle in n.lower()]
                except MetabaseError as exc:
                    say(f"  ({db.get('name')}: not readable: {str(exc)[:80]})")
                    continue
                for schema, name in found:
                    say(f"{db.get('name')}  ->  {schema}.{name}")
                    hits += 1
            say(f"{hits} table(s) with {args.find!r} in the name")
        else:
            for d in databases:
                say(f"{d.get('id'):>5}  {d.get('name')}  [{d.get('engine')}]")
    except MetabaseError as exc:
        print(f"Metabase: {exc}")
        return 2
    finally:
        mb.close()
        if args.out and lines:
            with contextlib.suppress(OSError):
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"Written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
