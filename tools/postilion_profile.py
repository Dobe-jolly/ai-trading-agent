"""What Postilion puts in its response fields for failed transactions, without showing any one client's data.

    python tools/postilion_profile.py --out reports/postilion_profile.txt
    python tools/postilion_profile.py --codes 61,65 --per-code 80 --out reports/postilion_limits.txt

Reads a random sample of client-facing legs (post_tran_leg, tran_postilion_originated = '0') per response code from
the POST OFFICE database (POST_OFFICE_DB_NAME) and summarises, per response code:

- extended_rsp_code and additional_rsp_data: the values and how often they occur;
- structured_data_rsp: which keys it holds (Postilion's length-prefixed format, or XML tags), how often each is
  present, and the shape of its values (length, digits or text).

A value is printed only when it is the same for at least --min-clients different accounts (so it is a message or a
code, not one client's details), never for keys that hold a person's details (names, ID, mobile, e-mail, references),
and runs of 7 or more digits are replaced even then. Accounts are compared by a
checksum computed in the database: account numbers, card numbers and names never leave it.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from agentcore.metabase import MetabaseClient, MetabaseError  # noqa: E402
from failed_txn.sources import parse_structured  # noqa: E402

LONG_DIGITS = re.compile(r"\d{7,}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# Keys that hold a person's details: their values are never printed, however many accounts share them.
PERSONAL = re.compile(r"(?i)name|national|\bid$|mobile|phone|msisdn|e-?mail|address|reference|refcode|account|"
                      r"\bpan\b|birth|dob")
METADATA = "Postilion:MetaData"  # Postilion's own bookkeeping: a '1' for each key present
XML = "<xml>"  # stands for an XML value: its tags are listed, its text is not kept
LEAF = re.compile(r"<([A-Za-z][\w]*)\b[^>]*>([^<]*)</\1>")
XML_TAG = re.compile(r"<([A-Za-z_][\w:.-]*)[\s/>]")
ROW_CAP = 2000  # Metabase returns at most this many rows to /api/dataset


def sample_sql(hours: int, per_code: int, codes: list[str]) -> str:
    wanted = (f"rsp_code_rsp IN ({', '.join(repr(c) for c in codes)})" if codes else "rsp_code_rsp <> '00'")
    return (
        "WITH legs AS (\n"
        "  SELECT rsp_code_rsp, sink_node_name, extended_rsp_code, additional_rsp_data, structured_data_rsp,\n"
        "         CHECKSUM(ISNULL(from_account_id, ''), ISNULL(to_account_id, '')) AS who,\n"
        "         ROW_NUMBER() OVER (PARTITION BY rsp_code_rsp ORDER BY NEWID()) AS n\n"
        "  FROM post_tran_leg WITH (NOLOCK)\n"
        f"  WHERE datetime_req >= DATEADD(hour, -{int(hours)}, GETDATE()) AND tran_postilion_originated = '0'\n"
        f"    AND {wanted}\n"
        ")\n"
        "SELECT rsp_code_rsp, sink_node_name, extended_rsp_code, additional_rsp_data, structured_data_rsp, who\n"
        f"FROM legs WHERE n <= {int(per_code)} ORDER BY rsp_code_rsp")


# ---- Postilion structured data -----------------------------------------------------------------------------
def keys_of(text: str) -> dict[str, str]:
    """Keys and values in a structured_data_rsp value: one level of nested structured data is opened as
    'outer.inner'; XML is listed by its tags (values not kept)."""
    text = (text or "").strip()
    if not text:
        return {}
    parsed = parse_structured(text)
    if parsed is None:
        if text.startswith("<"):
            return {f"<{tag}>": "" for tag in dict.fromkeys(XML_TAG.findall(text))}
        return {"(not in Postilion's key format)": text}
    out: dict[str, str] = {}
    for key, value in parsed.items():
        if key == METADATA:
            continue
        inner = parse_structured(value) if value[:1].isdigit() else None
        if inner:
            out.update({f"{key}.{k}": v for k, v in inner.items()})
        elif value.lstrip().startswith("<"):
            out[key] = XML
            out.update(xml_leaves(key, value))
        else:
            out[key] = value
    return out


def xml_leaves(key: str, xml: str) -> dict[str, str]:
    """Leaf elements of an XML value as 'key<Tag>' (the 2nd, 3rd ... of a tag as 'key<Tag>[2]'), with their text;
    container elements as 'key<Tag>' with no value."""
    out: dict[str, str] = {}
    seen: dict[str, int] = {}
    for tag, text in LEAF.findall(xml[:20000]):
        seen[tag] = seen.get(tag, 0) + 1
        out[f"{key}<{tag}>" + (f"[{seen[tag]}]" if seen[tag] > 1 else "")] = re.sub(r"\s+", " ", text).strip()
    for tag in dict.fromkeys(XML_TAG.findall(xml)):
        if not any(k.startswith(f"{key}<{tag}>") for k in out):
            out[f"{key}<{tag}>"] = ""
    return out


# ---- summary -----------------------------------------------------------------------------------------------
def shape(values: Iterable[str]) -> str:
    values = [v for v in values if v]
    if not values:
        return "no value"
    if all(v == XML for v in values):
        return "XML"
    lengths = sorted(len(v) for v in values)
    kinds = Counter("digits" if v.isdigit() else "xml" if v.lstrip().startswith("<") else
                    "letters" if v.replace(" ", "").isalpha() else "text" for v in values)
    span = f"{lengths[0]}" if lengths[0] == lengths[-1] else f"{lengths[0]}-{lengths[-1]}"
    return f"length {span}, " + ", ".join(f"{k} {n}" for k, n in kinds.most_common())


def safe(value: str, limit: int = 80) -> str:
    value = EMAIL.sub("<email>", LONG_DIGITS.sub(lambda m: f"<{len(m.group(0))} digits>", value.strip()))
    return value if len(value) <= limit else value[:limit] + "…"


def shared_values(pairs: Iterable[tuple[str, int]], min_clients: int, top: int = 12) -> list[tuple[str, int, int]]:
    """(value, rows, distinct accounts) for values seen for at least min_clients accounts, most common first."""
    rows: Counter = Counter()
    who: dict[str, set] = defaultdict(set)
    for value, client in pairs:
        value = (value or "").strip()
        if value:
            rows[value] += 1
            who[value].add(client)
    keep = [(v, n, len(who[v])) for v, n in rows.most_common() if len(who[v]) >= min_clients]
    return keep[:top]


def summarise(rows: list[dict], min_clients: int) -> list[str]:
    by_code: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_code[str(row.get("rsp_code_rsp") or "").strip() or "-"].append(row)
    out: list[str] = []
    for code in sorted(by_code):
        legs = by_code[code]
        n = len(legs)
        out.append(f"\n=== response {code}: {n} legs sampled ===")
        sinks = Counter(str(r.get("sink_node_name") or "-").strip() for r in legs)
        out.append("  sink nodes: " + ", ".join(f"{s} {c}" for s, c in sinks.most_common(6)))
        for column in ("extended_rsp_code", "additional_rsp_data"):
            pairs = [(str(r.get(column) or ""), r.get("who")) for r in legs]
            filled = sum(1 for v, _ in pairs if v.strip())
            if not filled:
                continue
            out.append(f"  {column}: filled on {filled}/{n}")
            for value, count, clients in shared_values(pairs, min_clients):
                out.append(f"      {safe(value)!r}: {count} legs, {clients} accounts")
        parsed = [(keys_of(str(r.get("structured_data_rsp") or "")), r.get("who")) for r in legs]
        filled = sum(1 for k, _ in parsed if k)
        if not filled:
            continue
        out.append(f"  structured_data_rsp: filled on {filled}/{n}")
        key_rows: Counter = Counter()
        key_values: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for keys, client in parsed:
            for key, value in keys.items():
                key_rows[key] += 1
                key_values[key].append((value, client))
        for key, count in key_rows.most_common():
            values = [v for v, _ in key_values[key]]
            out.append(f"    {key}: on {count}/{filled} ({shape(values)})")
            if PERSONAL.search(key.split(".")[-1].split("<")[0]):
                continue
            for value, legs_n, clients in shared_values([p for p in key_values[key] if p[0] != XML], min_clients, top=8):
                out.append(f"        {safe(value)!r}: {legs_n} legs, {clients} accounts")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="Metabase database name (default POST_OFFICE_DB_NAME, or POST OFFICE)")
    parser.add_argument("--codes", default="", help="response codes, comma-separated (default: every code but 00)")
    parser.add_argument("--hours", type=int, default=24, help="how far back to sample (default 24)")
    parser.add_argument("--per-code", type=int, default=60, help="legs sampled per response code (default 60)")
    parser.add_argument("--min-clients", type=int, default=3,
                        help="print a value only when this many different accounts share it (default 3)")
    parser.add_argument("--out", metavar="FILE", help="also write the summary to this file (UTF-8)")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    args = parser.parse_args(argv)
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    if any(not re.fullmatch(r"[0-9A-Za-z]{2}", c) for c in codes):
        parser.error("--codes takes two-character response codes, e.g. 30,91")
    if args.min_clients < 2:
        parser.error("--min-clients must be at least 2")
    load_dotenv(args.env_file, override=False)
    host, key = os.environ.get("METABASE_HOST"), os.environ.get("METABASE_API_KEY")
    if not host or not key:
        print("METABASE_HOST and METABASE_API_KEY must be set in .env")
        return 1
    name = args.db or os.environ.get("POST_OFFICE_DB_NAME") or "POST OFFICE"
    mb = MetabaseClient(host, key, retry_count=1)
    try:
        rows = mb.query(mb.resolve(name), sample_sql(args.hours, args.per_code, codes))
    except MetabaseError as exc:
        print(f"Metabase: {exc}")
        return 2
    finally:
        mb.close()
    lines = [f"Postilion response fields in {name!r}, last {args.hours}h, up to {args.per_code} random legs per code; "
             f"values shown only when shared by {args.min_clients}+ accounts"]
    if len(rows) >= ROW_CAP:
        lines.append(f"(Metabase stops at {ROW_CAP} rows: some codes are cut short; use --codes or a lower --per-code)")
    lines += summarise(rows, args.min_clients)
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
