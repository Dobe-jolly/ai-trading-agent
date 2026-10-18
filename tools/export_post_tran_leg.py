"""Export Postilion office legs (dbo.post_tran_leg) to one CSV and one TXT per month and currency.

    python tools/export_post_tran_leg.py --out reports/postilion_export                  # 2026-01-01 .. 2026-10-15
    python tools/export_post_tran_leg.py --start "2026-03-01 00:00:00" --end "2026-03-31 23:59:59" --out some/dir
    python tools/export_post_tran_leg.py --resume ...     # skip months already finished, redo only the unfinished

Columns (in order): tran_nr, tran_type, Authoriser_id (auth_id_rsp), State_code (rsp_code_rsp), message_type,
rsp_code_rsp, sink_node_name, source_node_name, STAN (system_trace_audit_nr), USD_currency_code (tran_currency_code),
tran_amount_req, pan, from_account_id, to_account_id, datetime_req. tran_amount_req is the transaction amount in
tran_currency_code, in the minor units Postilion stores (cents): divide by 100 for dollars. It replaces settle_amount_req,
which is in the settlement currency and differs from the transaction currency on some legs.

One file per month and currency: USD (840) and ZWG (924). Legs with no or another currency (inquiries, ZAR, ...) are not in
any file; the manifest counts them. TXT is tab-separated; CSV is comma-separated; both have a header row and the same rows.

The data goes from the database to the files and is never printed. It is read one day at a time through Metabase's CSV
download (all values cast to text in SQL so Metabase formats nothing), so a day is about 100,000 rows and the 2,000-row
limit of ordinary queries does not apply. Each day's row count is checked against a COUNT before it is kept. Files are
written as .part and renamed when a month and currency is complete, so a stopped run leaves no half file that looks done.
The run stops cleanly if free disk falls below --min-free-gb."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from agentcore.metabase import MetabaseClient  # noqa: E402

CURRENCIES = {"840": "USD", "924": "ZWG"}
HEADER = ["tran_nr", "tran_type", "Authoriser_id", "State_code", "message_type", "rsp_code_rsp", "sink_node_name",
          "source_node_name", "STAN", "USD_currency_code", "tran_amount_req", "pan", "from_account_id", "to_account_id",
          "datetime_req"]
SELECT = """SELECT CAST(l.tran_nr AS varchar(30)) AS tran_nr, l.tran_type AS tran_type, l.auth_id_rsp AS Authoriser_id,
  l.rsp_code_rsp AS State_code, l.message_type AS message_type, l.rsp_code_rsp AS rsp_code_rsp,
  l.sink_node_name AS sink_node_name, l.source_node_name AS source_node_name, l.system_trace_audit_nr AS STAN,
  l.tran_currency_code AS USD_currency_code, CAST(l.tran_amount_req AS varchar(40)) AS tran_amount_req, l.pan AS pan,
  l.from_account_id AS from_account_id, l.to_account_id AS to_account_id,
  CONVERT(varchar(23), l.datetime_req, 121) AS datetime_req
FROM dbo.post_tran_leg l WITH (NOLOCK)
WHERE {window} AND l.tran_currency_code IN ('840', '924')
ORDER BY l.datetime_req, l.tran_nr"""
COUNT = """SELECT l.tran_currency_code AS cur, COUNT(*) AS n FROM dbo.post_tran_leg l WITH (NOLOCK)
WHERE {window} AND l.tran_currency_code IN ('840', '924') GROUP BY l.tran_currency_code"""
CUR_AT = HEADER.index("USD_currency_code")


def when(text: str) -> datetime:
    return datetime.strptime(text.strip()[:19], "%Y-%m-%d %H:%M:%S")


def day_windows(start: datetime, end: datetime):
    """(label, SQL window, month key) for each day from start to end; the last slice includes `end` itself."""
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        lo = max(day, start)
        nxt = day + timedelta(days=1)
        if nxt > end:
            window = f"l.datetime_req >= '{lo:%Y-%m-%d %H:%M:%S}.000' AND l.datetime_req <= '{end:%Y-%m-%d %H:%M:%S}.000'"
        else:
            window = f"l.datetime_req >= '{lo:%Y-%m-%d %H:%M:%S}.000' AND l.datetime_req < '{nxt:%Y-%m-%d %H:%M:%S}.000'"
        yield f"{day:%Y-%m-%d}", window, f"{day:%Y-%m}"
        day = nxt


class Out:
    """The CSV and TXT of one month and currency, written as .part files."""

    def __init__(self, folder: Path, month: str, cur: str) -> None:
        self.base = folder / f"post_tran_leg_{month}_{CURRENCIES[cur]}"
        self.paths = [self.base.with_suffix(".csv"), self.base.with_suffix(".txt")]
        self.files = [open(p.with_name(p.name + ".part"), "w", encoding="utf-8", newline="") for p in self.paths]
        self.csv = csv.writer(self.files[0])
        self.txt = csv.writer(self.files[1], delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\", lineterminator="\r\n")
        self.csv.writerow(HEADER)
        self.txt.writerow(HEADER)
        self.rows = 0

    def write(self, row: list[str]) -> None:
        self.csv.writerow(row)
        self.txt.writerow([c.replace("\t", " ").replace("\r", " ").replace("\n", " ") for c in row])
        self.rows += 1

    def finish(self) -> list[tuple[str, int]]:
        sizes = []
        for f, p in zip(self.files, self.paths):
            f.close()
            part = p.with_name(p.name + ".part")
            part.replace(p)
            sizes.append((p.name, p.stat().st_size))
        return sizes

    def abandon(self) -> None:
        for f, p in zip(self.files, self.paths):
            f.close()


def stream_rows(mb: MetabaseClient, db_id: int, sql: str):
    """The CSV download of a native query, row by row (header checked), values as text."""
    payload = {"query": json.dumps({"database": db_id, "type": "native", "native": {"query": sql}, "parameters": []}),
               "format_rows": "false"}
    # the client's default is JSON; the CSV download takes a form
    with mb._http.stream("POST", "/api/dataset/csv", data=payload, timeout=STREAM_TIMEOUT,
                         headers={"Content-Type": "application/x-www-form-urlencoded"}) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"Metabase answered {resp.status_code}: {resp.read()[:200].decode('utf-8', 'replace')}")
        lines = (ln for ln in resp.iter_lines())
        reader = csv.reader(lines)
        header = next(reader, None)
        if header and header[0].startswith("﻿"):
            header[0] = header[0][1:]
        if header != HEADER:
            raise RuntimeError(f"unexpected columns from Metabase: {header}")
        for row in reader:
            yield row


STREAM_TIMEOUT = 45.0  # seconds without a byte from Metabase before a day's download is abandoned and retried
# (a healthy day arrives in 7-15 s; Metabase sometimes goes silent on one request, so waiting longer only wastes time)
RETRY_WAITS = (5, 15, 30, 60, 120)  # seconds before each retry of a day that failed (a reset, a 404/502 from the proxy, ...)


def fetch_day(mb: MetabaseClient, db_id: int, window: str, label: str) -> dict[str, list[list[str]]]:
    """One day's rows by currency, downloaded completely and checked against the database's own count. A failed or
    short download is retried from scratch, so a day is never half in a file and never in it twice."""
    last: Exception | None = None
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            expected = {str(r.get("cur") or r.get("CUR")): int(r.get("n") or r.get("N"))
                        for r in mb.query(db_id, COUNT.format(window=window))}
            got: dict[str, list[list[str]]] = {c: [] for c in CURRENCIES}
            for row in stream_rows(mb, db_id, SELECT.format(window=window)):
                got[row[CUR_AT]].append(row)
            for cur in CURRENCIES:
                if len(got[cur]) != expected.get(cur, 0):
                    raise RuntimeError(f"{len(got[cur])} {CURRENCIES[cur]} rows downloaded, the database counts "
                                       f"{expected.get(cur, 0)}")
            return got
        except Exception as exc:  # noqa: BLE001 - any failure of this day is retried, then reported with the day
            last = exc
            if attempt == len(RETRY_WAITS):
                break
            print(f"{label}: attempt {attempt + 1} failed ({str(exc)[:120]}); retrying in {RETRY_WAITS[attempt]}s", flush=True)
            time.sleep(RETRY_WAITS[attempt])
    raise RuntimeError(f"{label}: gave up after {len(RETRY_WAITS) + 1} attempts: {last}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=str(ROOT / "reports" / "postilion_export"))
    p.add_argument("--start", default="2026-01-01 00:01:00")
    p.add_argument("--end", default="2026-10-15 23:59:59")
    p.add_argument("--resume", action="store_true", help="skip months and currencies whose files are complete")
    p.add_argument("--min-free-gb", type=float, default=4.0, help="stop cleanly if free disk falls below this")
    p.add_argument("--env-file", default=str(ROOT / ".env"))
    args = p.parse_args(argv)

    load_dotenv(args.env_file, override=False)
    mb = MetabaseClient(os.environ["METABASE_HOST"], os.environ["METABASE_API_KEY"], timeout=290, retry_count=2,
                        verify=os.environ.get("METABASE_VERIFY_TLS", "true").lower() not in ("false", "0", "no"))
    db_id = mb.resolve(os.environ.get("POST_OFFICE_DB_NAME") or "POST OFFICE")
    start, end = when(args.start), when(args.end)
    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob("*.part"):  # a stopped run's half files are never kept
        stale.unlink()
    manifest = folder / "manifest.csv"
    done: dict[str, list] = {}
    if args.resume and manifest.exists():
        with open(manifest, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                done[r["file"]] = [r["file"], r["rows"], r["bytes"]]
    months: dict[str, list] = {}
    for label, window, month in day_windows(start, end):
        months.setdefault(month, []).append((label, window))
    t0 = time.time()
    for month, days in months.items():
        todo = [c for c in CURRENCIES if not (args.resume and f"post_tran_leg_{month}_{CURRENCIES[c]}.csv" in done
                                              and f"post_tran_leg_{month}_{CURRENCIES[c]}.txt" in done)]
        if not todo:
            print(f"{month}: already complete, skipped", flush=True)
            continue
        outs = {c: Out(folder, month, c) for c in todo}
        mt = time.time()
        try:
            for label, window in days:
                free = shutil.disk_usage(folder).free / 1e9
                if free < args.min_free_gb:
                    print(f"STOPPED: only {free:.1f} GB free (limit {args.min_free_gb} GB) at {label}", flush=True)
                    for o in outs.values():
                        o.abandon()
                    return 2
                by_cur = fetch_day(mb, db_id, window, label)  # the whole day, checked, before any of it is written
                for cur, rows in by_cur.items():
                    if cur in outs:
                        for row in rows:
                            outs[cur].write(row)
        except BaseException:
            for o in outs.values():
                o.abandon()
            raise
        new = []
        for cur, o in outs.items():
            for (name, size) in o.finish():
                new.append({"file": name, "rows": o.rows, "bytes": size})
            print(f"{month} {CURRENCIES[cur]}: {o.rows:,} rows", flush=True)
        done.update({r["file"]: [r["file"], r["rows"], r["bytes"]] for r in new})
        with open(manifest, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["file", "rows", "bytes"])
            w.writerows(sorted(done.values()))
        print(f"{month} done in {time.time() - mt:.0f}s ({time.time() - t0:.0f}s so far); free {shutil.disk_usage(folder).free / 1e9:.1f} GB",
              flush=True)
    print(f"finished: {len(done)} files listed in {manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
