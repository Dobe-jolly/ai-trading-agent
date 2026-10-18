"""Monthly TXT exports of the VBS, Zimpost and Agency Banking transaction tables, through Metabase.

One tab-separated file per month and currency, e.g. vbs_2026-01_USD.txt. Columns are the ones in the schema catalogue
(personal columns are already left out of it). Every month is checked against the database's own count per currency; a
failed or short download is retried from scratch, so a month is never half in a file. Finished files are not redone
with --resume.

  python tools/export_tables.py                         # all three sources, 2026-01-01 to 2026-10-15
  python tools/export_tables.py --source zimpost --resume
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# the Postilion exporter already has the streaming download and its retry waits
_spec = importlib.util.spec_from_file_location("export_post_tran_leg", ROOT / "tools" / "export_post_tran_leg.py")
ex = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("export_post_tran_leg", ex)
_spec.loader.exec_module(ex)

# name -> (Metabase database, schema file, table, date column, ordering column)
SOURCES = {
    "vbs": ("VBS TRANSACTIONS", "vbs_transactions.json", "public.cash_transaction", "date_created", "id"),
    "zimpost": ("Agency Banking", "agency_banking.json", "public.wallet_transaction", "date_created", "id"),
    "agencybanking": ("AGENCY BANKING V2", "agency_banking_v2.json", "public.wallet_transaction", "date_created", "id"),
}
CURRENCY_NAMES = {"ZIG": "ZWG"}  # the bank calls it ZWG; the source tables say ZIG
STREAM_TIMEOUT = 120.0


def columns_of(schema_file: str, table: str) -> list[str]:
    d = json.loads((ROOT / "agentcore" / "schema" / schema_file).read_text(encoding="utf-8"))
    return list(d["tables"][table]["columns"])


def month_windows(start: date, end: date):
    """(month key, lower bound, exclusive upper bound) as text dates; the last month stops after `end`."""
    first = start.replace(day=1)
    while first <= end:
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        lo = max(first, start)
        hi = min(nxt, end + timedelta(days=1))
        yield f"{first:%Y-%m}", f"{lo:%Y-%m-%d}", f"{hi:%Y-%m-%d}"
        first = nxt


def currency_label(value: str) -> str:
    v = (value or "").strip().upper()
    return CURRENCY_NAMES.get(v, v) if v else "NONE"


class Source:
    def __init__(self, name: str) -> None:
        self.name = name
        self.database, schema_file, self.table, self.date_col, self.order_col = SOURCES[name]
        self.columns = columns_of(schema_file, self.table)
        self.cur_at = self.columns.index("currency")

    def where(self, lo: str, hi: str) -> str:
        return f"{self.date_col} >= '{lo}' AND {self.date_col} < '{hi}'"

    def select(self, lo: str, hi: str) -> str:
        cols = ", ".join(f'"{c}"' for c in self.columns)
        return f"SELECT {cols} FROM {self.table} WHERE {self.where(lo, hi)} ORDER BY {self.date_col}, {self.order_col}"

    def count(self, lo: str, hi: str) -> str:
        return (f"SELECT COALESCE(NULLIF(TRIM(currency), ''), 'NONE') AS cur, COUNT(*) AS n FROM {self.table} "
                f"WHERE {self.where(lo, hi)} GROUP BY 1")


def fetch_month(mb, db_id: int, src: Source, lo: str, hi: str, label: str,
                sleep=time.sleep) -> dict[str, list[list[str]]]:
    """One month's rows by currency label, checked against the database's count."""
    last: Exception | None = None
    for attempt in range(len(ex.RETRY_WAITS) + 1):
        try:
            expected: dict[str, int] = {}
            for r in mb.query(db_id, src.count(lo, hi)):
                k = currency_label(str(r.get("cur") or r.get("CUR")))
                expected[k] = expected.get(k, 0) + int(r.get("n") or r.get("N"))
            got: dict[str, list[list[str]]] = {}
            for row in ex.stream_rows(mb, db_id, src.select(lo, hi), src.columns):
                got.setdefault(currency_label(row[src.cur_at]), []).append(row)
            if {k: len(v) for k, v in got.items()} != expected:
                raise RuntimeError(f"downloaded { {k: len(v) for k, v in got.items()} }, the database counts {expected}")
            return got
        except Exception as exc:  # noqa: BLE001 - any failure of this month is retried, then reported with the month
            last = exc
            if attempt == len(ex.RETRY_WAITS):
                break
            print(f"{label}: attempt {attempt + 1} failed ({str(exc)[:120]}); retrying in {ex.RETRY_WAITS[attempt]}s", flush=True)
            sleep(ex.RETRY_WAITS[attempt])
    raise RuntimeError(f"{label}: gave up after {len(ex.RETRY_WAITS) + 1} attempts: {last}")


def write_file(path: Path, header: list[str], rows: list[list[str]]) -> int:
    part = path.with_name(path.name + ".part")
    with open(part, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\", lineterminator="\r\n")
        w.writerow(header)
        for row in rows:
            w.writerow([c.replace("\t", " ").replace("\r", " ").replace("\n", " ") for c in row])
    part.replace(path)
    return path.stat().st_size


def run_source(mb, name: str, out: Path, start: date, end: date, resume: bool) -> int:
    src = Source(name)
    db_id = mb.resolve(src.database)
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "manifest.txt"
    if not manifest.exists():
        manifest.write_text("file\trows\tbytes\r\n", encoding="utf-8")
    failures = 0
    for month, lo, hi in month_windows(start, end):
        label = f"{name} {month}"
        if resume and list(out.glob(f"{name}_{month}_*.txt")):
            print(f"{label}: already done, skipped", flush=True)
            continue
        t0 = time.time()
        try:
            got = fetch_month(mb, db_id, src, lo, hi, label)
        except RuntimeError as exc:
            print(f"FAILED {exc}", flush=True)
            failures += 1
            continue
        lines = []
        for cur, rows in sorted(got.items()):
            path = out / f"{name}_{month}_{cur}.txt"
            size = write_file(path, src.columns, rows)
            lines.append(f"{path.name}\t{len(rows)}\t{size}\r\n")
        with open(manifest, "a", encoding="utf-8", newline="") as fh:
            fh.writelines(lines)
        print(f"{label}: done in {time.time() - t0:.0f}s, " + ", ".join(f"{c} {len(r):,}" for c, r in sorted(got.items())), flush=True)
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=sorted(SOURCES) + ["all"], default="all")
    ap.add_argument("--out", default=str(ROOT / "reports" / "tables_export"))
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-10-15")
    ap.add_argument("--resume", action="store_true", help="skip months that already have files")
    a = ap.parse_args(argv)
    from dotenv import load_dotenv
    from agentcore.metabase import MetabaseClient
    load_dotenv(ROOT / ".env")
    ex.STREAM_TIMEOUT = STREAM_TIMEOUT
    mb = MetabaseClient(os.environ["METABASE_HOST"], os.environ["METABASE_API_KEY"], timeout=250, retry_count=1,
                        verify=os.environ.get("METABASE_VERIFY_TLS", "true").lower() not in ("false", "0", "no"))
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    failures = 0
    for name in (sorted(SOURCES) if a.source == "all" else [a.source]):
        failures += run_source(mb, name, Path(a.out) / name, start, end, a.resume)
    print(f"finished, {failures} failed months", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
