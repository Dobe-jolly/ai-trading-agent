"""Export Postilion office legs (dbo.post_tran_leg) to one tab-separated TXT per month and currency (--formats csv|both
for CSV as well; a txt-only export contains no .csv at all, not even the manifest).

    python tools/export_post_tran_leg.py --out reports/postilion_export_v2 --leg client --rrn
    python tools/export_post_tran_leg.py --leg all --rrn --leg-flag      # keep both legs, with a column that tells them apart
    python tools/export_post_tran_leg.py --start "2026-03-01 00:00:00" --end "2026-03-31 23:59:59" --out some/dir
    python tools/export_post_tran_leg.py --resume ...     # skip months already finished, redo only the unfinished
    python tools/export_post_tran_leg.py --workers 4 ...  # months downloaded at the same time (the database is the limit)
    python tools/export_post_tran_leg.py --relabel DIR    # re-apply tools/postilion_type_names.json to finished files

Columns, in order (the names are added by this tool from the codes; the rest come from the database):
tran_nr, tran_type, transaction_type, Authoriser_id (auth_id_rsp), State_code (rsp_code_rsp), message_type,
message_type_name, rsp_code_rsp, sink_node_name, source_node_name, STAN (system_trace_audit_nr), USD_currency_code
(tran_currency_code), tran_amount_req, [RRN], [tran_postilion_originated], pan, from_account_id, to_account_id,
datetime_req.

- tran_amount_req is the transaction amount in tran_currency_code, in the minor units Postilion stores (cents): divide by
  100 for dollars. It replaces settle_amount_req, which is in the settlement currency.
- transaction_type is the bank's own name for the transaction (the Postilion transaction-type map the desk agent uses, from
  tran type + message type + source node + sink node), for example "Zipit Send" or "Withdrawal - Us on Them". Where the bank's
  map has no entry it falls back to the plain name of the type code (Purchase, Cash withdrawal, Refund, ...) or, for a code
  nobody has named, "Type NN (unnamed)". message_type_name is the ISO 8583 name of the message type.
- --leg client keeps only the client-facing leg (tran_postilion_originated = 0): one row per transaction. --leg all keeps
  both legs of a transaction (each transaction normally has two, identical in nearly every column). --rrn adds the retrieval
  reference number (the same on both legs; it is the key to match a transaction in other systems, but it is not unique).
  --leg-flag adds tran_postilion_originated.
- One file per month and currency: USD (840) and ZWG (924). Legs with no or another currency (inquiries, ZAR, ...) are in no
  file.

The data goes from the database to the files and is never printed. It is read one day at a time through Metabase's CSV
download (all values cast to text in SQL so Metabase formats nothing). Each day is downloaded completely and compared with
a COUNT before any of it is written; a failed or short day (or 45 s of silence from Metabase) is retried from scratch, so a
day is never half in a file or in it twice. Files are written as .part and renamed when a month and currency is complete.
The run stops cleanly if free disk falls below --min-free-gb."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from agentcore.metabase import MetabaseClient  # noqa: E402
from failed_txn.sources import TRAN_TYPES, TXN_NAMES  # noqa: E402

CURRENCIES = {"840": "USD", "924": "ZWG"}

# ISO 8583 message types (the ones Postilion carries); 0100/0200/0220/0420/0600 are what this data holds.
MESSAGE_TYPE_NAMES = {
    "0100": "Authorization request", "0110": "Authorization response", "0120": "Authorization advice",
    "0130": "Authorization advice response", "0200": "Financial transaction request",
    "0210": "Financial transaction response", "0220": "Financial transaction advice",
    "0230": "Financial transaction advice response", "0400": "Reversal request", "0410": "Reversal response",
    "0420": "Reversal advice", "0430": "Reversal advice response", "0500": "Reconciliation request",
    "0510": "Reconciliation response", "0600": "Administrative request", "0610": "Administrative response",
    "0800": "Network management request", "0810": "Network management response",
}
# Plain names of the type codes the bank's own code already knows (failed_txn.sources); other codes are left unnamed
# rather than guessed.
TRAN_TYPE_NAMES = {**TRAN_TYPES, "30": "balance / available funds inquiry", "31": "balance / available funds inquiry"}


def message_type_name(message_type: str, names: dict | None = None) -> str:
    """The ISO 8583 name of a message type; a names file ({"message_types": {"0600": "..."}}) can add or override."""
    code = str(message_type or "").strip()
    code = code.zfill(4) if code.isdigit() else code
    given = (names or {}).get("message_types", {})
    return given.get(code) or MESSAGE_TYPE_NAMES.get(code, f"Message type {code or '(none)'} (unnamed)")


def transaction_type(tran_type: str, message_type: str, source: str, sink: str, names: dict | None = None) -> str:
    """The bank's name for the transaction, else a name from the names file for the type code ({"tran_types": {"32":
    "..."}}), else the plain name of the code, else 'Type NN (unnamed)'."""
    code = str(tran_type or "").strip()
    norm = code.zfill(2) if code.isdigit() else code
    try:
        key = (int(code), int(str(message_type).strip()), str(source or "").strip().lower(), str(sink or "").strip().lower())
    except ValueError:
        key = None
    if key in TXN_NAMES:
        return TXN_NAMES[key]
    given = (names or {}).get("tran_types", {}).get(norm) or (names or {}).get("tran_types", {}).get(code)
    if given:
        return given
    plain = TRAN_TYPE_NAMES.get(norm)
    return plain[:1].upper() + plain[1:] if plain else f"Type {code or '(none)'} (unnamed)"


@dataclass
class Spec:
    """Which rows and columns: built from the options, shared by the SQL, the header check and the output rows."""
    leg: str = "all"
    rrn: bool = False
    leg_flag: bool = False
    names: dict | None = None  # optional extra names, from the names file

    def __post_init__(self) -> None:
        sql_cols = [("tran_nr", "CAST(l.tran_nr AS varchar(30))"), ("tran_type", "l.tran_type"),
                    ("Authoriser_id", "l.auth_id_rsp"), ("State_code", "l.rsp_code_rsp"), ("message_type", "l.message_type"),
                    ("rsp_code_rsp", "l.rsp_code_rsp"), ("sink_node_name", "l.sink_node_name"),
                    ("source_node_name", "l.source_node_name"), ("STAN", "l.system_trace_audit_nr"),
                    ("USD_currency_code", "l.tran_currency_code"), ("tran_amount_req", "CAST(l.tran_amount_req AS varchar(40))")]
        if self.rrn:
            sql_cols.append(("RRN", "l.retrieval_reference_nr"))
        if self.leg_flag:
            sql_cols.append(("tran_postilion_originated", "l.tran_postilion_originated"))
        sql_cols += [("pan", "l.pan"), ("from_account_id", "l.from_account_id"), ("to_account_id", "l.to_account_id"),
                     ("datetime_req", "CONVERT(varchar(23), l.datetime_req, 121)")]
        self.sql_header = [n for n, _ in sql_cols]
        legs = " AND l.tran_postilion_originated = '0'" if self.leg == "client" else ""
        self.select = (f"SELECT {', '.join(f'{expr} AS {name}' for name, expr in sql_cols)}\n"
                       "FROM dbo.post_tran_leg l WITH (NOLOCK)\n"
                       f"WHERE {{window}} AND l.tran_currency_code IN ('840', '924'){legs}\n"
                       "ORDER BY l.datetime_req, l.tran_nr")
        self.count = ("SELECT l.tran_currency_code AS cur, COUNT(*) AS n FROM dbo.post_tran_leg l WITH (NOLOCK)\n"
                      f"WHERE {{window}} AND l.tran_currency_code IN ('840', '924'){legs} GROUP BY l.tran_currency_code")
        self.header = []
        for name in self.sql_header:
            self.header.append(name)
            if name == "tran_type":
                self.header.append("transaction_type")
            elif name == "message_type":
                self.header.append("message_type_name")
        at = {n: i for i, n in enumerate(self.sql_header)}
        self.cur_at = at["USD_currency_code"]
        self._tt, self._mt, self._src, self._snk = at["tran_type"], at["message_type"], at["source_node_name"], at["sink_node_name"]

    def out_row(self, row: list[str]) -> list[str]:
        """A database row with the two name columns inserted after tran_type and message_type."""
        name = transaction_type(row[self._tt], row[self._mt], row[self._src], row[self._snk], self.names)
        mname = message_type_name(row[self._mt], self.names)
        out = []
        for i, value in enumerate(row):
            out.append(value)
            if i == self._tt:
                out.append(name)
            elif i == self._mt:
                out.append(mname)
        return out


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
    """The files of one month and currency (TXT, CSV or both), written as .part files."""

    def __init__(self, folder: Path, month: str, cur: str, header: list[str], formats: tuple[str, ...] = ("txt",)) -> None:
        self.formats = formats
        self.base = folder / f"post_tran_leg_{month}_{CURRENCIES[cur]}"
        self.paths = [self.base.with_suffix(f".{f}") for f in formats]
        self.files = [open(p.with_name(p.name + ".part"), "w", encoding="utf-8", newline="") for p in self.paths]
        self.writers = [csv.writer(fh) if f == "csv" else
                        csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\", lineterminator="\r\n")
                        for f, fh in zip(formats, self.files)]
        for w in self.writers:
            w.writerow(header)
        self.rows = 0

    def write(self, row: list[str]) -> None:
        for f, w in zip(self.formats, self.writers):
            w.writerow(row if f == "csv" else [c.replace("\t", " ").replace("\r", " ").replace("\n", " ") for c in row])
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
        """Give up on this month: close and delete the half-written .part files."""
        for f, p in zip(self.files, self.paths):
            f.close()
            p.with_name(p.name + ".part").unlink(missing_ok=True)


STREAM_TIMEOUT = 45.0  # seconds without a byte from Metabase before a day's download is abandoned and retried
# (a healthy day arrives in 7-15 s; Metabase sometimes goes silent on one request, so waiting longer only wastes time)
RETRY_WAITS = (5, 15, 30, 60, 120)  # seconds before each retry of a day that failed (a reset, a 404/502 from the proxy, ...)


def stream_rows(mb: MetabaseClient, db_id: int, sql: str, sql_header: list[str]):
    """The CSV download of a native query, row by row (header checked), values as text."""
    payload = {"query": json.dumps({"database": db_id, "type": "native", "native": {"query": sql}, "parameters": []}),
               "format_rows": "false"}
    # the client's default is JSON; the CSV download takes a form
    with mb._http.stream("POST", "/api/dataset/csv", data=payload, timeout=STREAM_TIMEOUT,
                         headers={"Content-Type": "application/x-www-form-urlencoded"}) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"Metabase answered {resp.status_code}: {resp.read()[:200].decode('utf-8', 'replace')}")
        reader = csv.reader(ln for ln in resp.iter_lines())
        header = next(reader, None)
        if header and header[0].startswith("﻿"):
            header[0] = header[0][1:]
        if header != sql_header:
            raise RuntimeError(f"unexpected columns from Metabase: {header}")
        for row in reader:
            yield row


def fetch_day(mb: MetabaseClient, db_id: int, spec: Spec, window: str, label: str,
              sleep: Callable[[float], None] = time.sleep) -> dict[str, list[list[str]]]:
    """One day's output rows by currency, downloaded completely and checked against the database's own count. A failed or
    short download is retried from scratch, so a day is never half in a file and never in it twice."""
    last: Exception | None = None
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            expected = {str(r.get("cur") or r.get("CUR")): int(r.get("n") or r.get("N"))
                        for r in mb.query(db_id, spec.count.format(window=window))}
            got: dict[str, list[list[str]]] = {c: [] for c in CURRENCIES}
            for row in stream_rows(mb, db_id, spec.select.format(window=window), spec.sql_header):
                got[row[spec.cur_at]].append(spec.out_row(row))
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
            sleep(RETRY_WAITS[attempt])
    raise RuntimeError(f"{label}: gave up after {len(RETRY_WAITS) + 1} attempts: {last}")


RELABEL_COLUMNS = ("tran_type", "message_type", "source_node_name", "sink_node_name", "transaction_type", "message_type_name")


def relabel_file(path: Path, names: dict | None) -> tuple[int, int]:
    """Recompute transaction_type and message_type_name in a finished TXT or CSV from the codes in each row. Every other
    byte of every row is kept. Returns (rows, rows whose names changed). The file is replaced only when complete; if
    anything goes wrong the temporary file is removed and the original is left as it was."""
    tmp = path.with_name(path.name + ".relabel")
    try:
        rows, changed = _relabel_into(path, tmp, names)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)
    return rows, changed


def _relabel_into(path: Path, tmp: Path, names: dict | None) -> tuple[int, int]:
    is_csv = path.suffix == ".csv"
    rows = changed = 0
    with open(path, encoding="utf-8", newline="") as src, open(tmp, "w", encoding="utf-8", newline="") as dst:
        if is_csv:
            reader, writer = csv.reader(src), csv.writer(dst)
            header = next(reader)
            lines = reader
        else:
            header = src.readline().rstrip("\r\n").split("\t")
            lines = (ln.rstrip("\r\n").split("\t") for ln in src)
        missing = [c for c in RELABEL_COLUMNS if c not in header]
        if missing:
            raise ValueError(f"{path.name}: no column {', '.join(missing)}: not a file this tool wrote")
        at = {c: header.index(c) for c in RELABEL_COLUMNS}
        if is_csv:
            writer.writerow(header)
        else:
            dst.write("\t".join(header) + "\r\n")
        for parts in lines:
            new_t = transaction_type(parts[at["tran_type"]], parts[at["message_type"]], parts[at["source_node_name"]],
                                     parts[at["sink_node_name"]], names)
            new_m = message_type_name(parts[at["message_type"]], names)
            if (new_t, new_m) != (parts[at["transaction_type"]], parts[at["message_type_name"]]):
                changed += 1
                parts[at["transaction_type"]], parts[at["message_type_name"]] = new_t, new_m
            if is_csv:
                writer.writerow(parts)
            else:
                dst.write("\t".join(parts) + "\r\n")
            rows += 1
    return rows, changed


def relabel_folder(folder: Path, names: dict | None) -> int:
    """Apply the names file to every finished export file in a folder and refresh the manifest's sizes."""
    files = sorted(p for p in folder.iterdir() if p.name.startswith("post_tran_leg_") and p.suffix in (".txt", ".csv"))
    if not files:
        print(f"no post_tran_leg_* files in {folder}")
        return 1
    total = changed_total = 0
    for p in files:
        rows, changed = relabel_file(p, names)
        total += rows
        changed_total += changed
        print(f"{p.name}: {rows:,} rows, {changed:,} relabelled", flush=True)
    manifest = folder / "manifest.txt"
    if manifest.exists():
        with open(manifest, encoding="utf-8", newline="") as f:
            old = {r["file"]: r for r in csv.DictReader(f, delimiter="\t")}
        with open(manifest, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter="\t", lineterminator="\r\n")
            w.writerow(["file", "rows", "bytes"])
            for p in files:
                w.writerow([p.name, old.get(p.name, {}).get("rows", ""), p.stat().st_size])
    print(f"relabelled {changed_total:,} of {total:,} rows in {len(files)} files")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--relabel", metavar="DIR", help="re-apply the names file to the finished files in DIR (no export, no database)")
    p.add_argument("--out", default=str(ROOT / "reports" / "postilion_export_v2"))
    p.add_argument("--start", default="2026-01-01 00:01:00")
    p.add_argument("--end", default="2026-10-15 23:59:59")
    p.add_argument("--leg", choices=("all", "client"), default="all", help="client: only tran_postilion_originated = 0")
    p.add_argument("--rrn", action="store_true", help="add the retrieval reference number as RRN")
    p.add_argument("--leg-flag", action="store_true", help="add tran_postilion_originated")
    p.add_argument("--formats", choices=("txt", "csv", "both"), default="txt",
                   help="txt (default): tab-separated .txt files only, no .csv; csv; or both")
    p.add_argument("--names", default=str(ROOT / "tools" / "postilion_type_names.json"),
                   help='JSON with extra names: {"tran_types": {"32": "..."}, "message_types": {"0600": "..."}}')
    p.add_argument("--workers", type=int, default=1,
                   help="months downloaded at the same time (default 1: one after another); each worker is one stream of "
                        "queries on the database, so a few is plenty")
    p.add_argument("--resume", action="store_true", help="skip months and currencies whose files are complete")
    p.add_argument("--min-free-gb", type=float, default=4.0, help="stop cleanly if free disk falls below this")
    p.add_argument("--env-file", default=str(ROOT / ".env"))
    args = p.parse_args(argv)

    names = json.loads(Path(args.names).read_text(encoding="utf-8")) if Path(args.names).exists() else None
    if args.relabel:
        return relabel_folder(Path(args.relabel), names)
    spec = Spec(args.leg, args.rrn, args.leg_flag, names)
    load_dotenv(args.env_file, override=False)
    mb = MetabaseClient(os.environ["METABASE_HOST"], os.environ["METABASE_API_KEY"], timeout=290, retry_count=2,
                        verify=os.environ.get("METABASE_VERIFY_TLS", "true").lower() not in ("false", "0", "no"))
    db_id = mb.resolve(os.environ.get("POST_OFFICE_DB_NAME") or "POST OFFICE")
    start, end = when(args.start), when(args.end)
    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob("*.part"):  # a stopped run's half files are never kept
        stale.unlink()
    formats = ("txt", "csv") if args.formats == "both" else (args.formats,)
    manifest = folder / "manifest.txt"  # tab-separated, like the data files: no .csv appears in a txt-only export
    done: dict[str, list] = {}
    if args.resume and manifest.exists():
        with open(manifest, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f, delimiter="\t"):
                done[r["file"]] = [r["file"], r["rows"], r["bytes"]]
    months: dict[str, list] = {}
    for label, window, month in day_windows(start, end):
        months.setdefault(month, []).append((label, window))
    t0 = time.time()
    lock = threading.Lock()  # the manifest, the screen and the disk check are shared by the workers
    stop = threading.Event()  # set when the disk runs low: every worker abandons its month

    def say(text: str) -> None:
        with lock:
            print(text, flush=True)

    def process_month(month: str, days: list) -> str:
        todo = [c for c in CURRENCIES
                if not (args.resume and all(f"post_tran_leg_{month}_{CURRENCIES[c]}.{fmt}" in done for fmt in formats))]
        if not todo:
            say(f"{month}: already complete, skipped")
            return "skipped"
        outs = {c: Out(folder, month, c, spec.header, formats) for c in todo}
        mt = time.time()
        try:
            for label, window in days:
                free = shutil.disk_usage(folder).free / 1e9
                if free < args.min_free_gb or stop.is_set():
                    if not stop.is_set():
                        say(f"STOPPED: only {free:.1f} GB free (limit {args.min_free_gb} GB) at {month} {label}")
                    stop.set()
                    for o in outs.values():
                        o.abandon()
                    return "stopped"
                by_cur = fetch_day(mb, db_id, spec, window, label)  # the whole day, checked, before any of it is written
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
            say(f"{month} {CURRENCIES[cur]}: {o.rows:,} rows")
        with lock:
            done.update({r["file"]: [r["file"], r["rows"], r["bytes"]] for r in new})
            with open(manifest, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f, delimiter="\t", lineterminator="\r\n")
                w.writerow(["file", "rows", "bytes"])
                w.writerows(sorted(done.values()))
        say(f"{month} done in {time.time() - mt:.0f}s ({time.time() - t0:.0f}s so far); free {shutil.disk_usage(folder).free / 1e9:.1f} GB")
        return "done"

    failed: dict[str, str] = {}
    if args.workers <= 1:
        for month, days in months.items():
            try:
                process_month(month, days)
            except Exception as exc:  # noqa: BLE001 - reported below; the other months still run
                failed[month] = str(exc)[:300]
                say(f"FAILED {month}: {failed[month]}")
            if stop.is_set():
                break
    else:
        with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="month") as pool:
            futures = {pool.submit(process_month, month, days): month for month, days in months.items()}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001 - one month failing does not stop the others
                    failed[futures[fut]] = str(exc)[:300]
                    say(f"FAILED {futures[fut]}: {failed[futures[fut]]}")
    if stop.is_set():
        return 2
    if failed:
        say(f"finished with {len(failed)} failed month(s): {', '.join(sorted(failed))} (run again with --resume to redo them)")
        return 1
    say(f"finished: {len(done)} files listed in {manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
