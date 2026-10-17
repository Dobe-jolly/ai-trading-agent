"""What a table's values look like, without showing any one client's data: counts of the values of categorical columns,
the shapes of identifier columns, and how many rows share each identifier. Aggregates run in the database (PostgreSQL);
no row is read here.

    python tools/table_profile.py --db "Ecocash Reg" --table public.ecocash_registration \
        --values status,service_type,currency_code --shapes account_id,msisdn,date_added \
        --per account_id,msisdn --out reports/ecocash_reg_profile.txt

--values  columns whose values are codes (status, type, currency): each value with its count; a value is shown only
          when at least --min-rows rows have it.
--shapes  identifier columns: each value with digits shown as 9 and letters as A (2637... becomes 9999...), so the
          format is visible but no number is.
--per     identifier columns: how many rows share one identifier (e.g. how many registrations one account has).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from agentcore.metabase import MetabaseClient, MetabaseError  # noqa: E402

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def ident(name: str) -> str:
    parts = name.split(".")
    if not 1 <= len(parts) <= 2 or not all(NAME.fullmatch(p) for p in parts):
        raise ValueError(f"not a table or column name: {name!r}")
    return ".".join(f'"{p}"' for p in parts)


def values_sql(table: str, column: str, min_rows: int) -> str:
    c = ident(column)
    return (f"SELECT {c}::text AS value, COUNT(*) AS n FROM {ident(table)} GROUP BY {c} "
            f"HAVING COUNT(*) >= {int(min_rows)} ORDER BY n DESC LIMIT 40")


def shapes_sql(table: str, column: str) -> str:
    c = ident(column)
    shape = f"regexp_replace(regexp_replace(COALESCE({c}::text, '(null)'), '[0-9]', '9', 'g'), '[A-Za-z]', 'A', 'g')"
    return f"SELECT {shape} AS value, COUNT(*) AS n FROM {ident(table)} GROUP BY 1 ORDER BY n DESC LIMIT 25"


def per_sql(table: str, column: str) -> str:
    c = ident(column)
    return (f"SELECT rows_each::text AS value, COUNT(*) AS n FROM (SELECT {c}, COUNT(*) AS rows_each "
            f"FROM {ident(table)} GROUP BY {c}) x GROUP BY rows_each ORDER BY rows_each LIMIT 25")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="Metabase database name")
    parser.add_argument("--table", required=True, help="schema.table")
    parser.add_argument("--values", default="", help="code columns, comma-separated")
    parser.add_argument("--shapes", default="", help="identifier columns, comma-separated")
    parser.add_argument("--per", default="", help="identifier columns: rows per identifier")
    parser.add_argument("--min-rows", type=int, default=3)
    parser.add_argument("--out", metavar="FILE")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    args = parser.parse_args(argv)
    cols = {k: [c.strip() for c in getattr(args, k).split(",") if c.strip()] for k in ("values", "shapes", "per")}
    for c in sum(cols.values(), []) + [args.table]:
        ident(c)
    load_dotenv(args.env_file, override=False)
    host, key = os.environ.get("METABASE_HOST"), os.environ.get("METABASE_API_KEY")
    if not host or not key:
        print("METABASE_HOST and METABASE_API_KEY must be set in .env")
        return 1
    mb = MetabaseClient(host, key, retry_count=1)
    lines = [f"{args.table} in {args.db!r} (counts and shapes only)"]
    try:
        db = mb.resolve(args.db)
        total = mb.query(db, f"SELECT COUNT(*) AS n FROM {ident(args.table)}")
        lines.append(f"rows: {total[0].get('n') if total else '?'}")
        for kind, make, title in (("values", lambda c: values_sql(args.table, c, args.min_rows), "values"),
                                  ("shapes", lambda c: shapes_sql(args.table, c), "shapes (9 = digit, A = letter)"),
                                  ("per", lambda c: per_sql(args.table, c), "rows per value: how many values have N rows")):
            for column in cols[kind]:
                lines.append(f"\n{column} — {title}")
                try:
                    for row in mb.query(db, make(column)):
                        lines.append(f"  {str(row.get('value'))[:60]!r}: {row.get('n')}")
                except MetabaseError as exc:
                    lines.append(f"  (not readable: {str(exc)[:120]})")
    except MetabaseError as exc:
        print(f"Metabase: {exc}")
        return 2
    finally:
        mb.close()
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
