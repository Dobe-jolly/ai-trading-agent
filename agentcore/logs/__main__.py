"""python -m agentcore.logs: ingest service logs into the store and look at what is in it.

    python -m agentcore.logs ingest --service ussd ussd.log            # replaces that service's earlier data
    python -m agentcore.logs ingest --service ussd --append more.log
    python -m agentcore.logs services                                  # what is held
    python -m agentcore.logs errors [--service S] [--mode top|spikes|new] [--since 2026-10-17]
    python -m agentcore.logs search --kind account --value 86000013101001 [--since ...]
    python -m agentcore.logs note SIGNATURE "what this error means and what to do"

Settings (from the environment or .env): LOG_DB (default state/logs.db) and LOG_HASH_KEY (at least 16 characters, kept
out of the store and out of git: it is what makes the identifier hashes searchable by us and meaningless to anyone else)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from agentcore.dataquery import identifier_forms

from .mask import KeyedMasker
from .profiles import PROFILE_DIR, load_profiles, profile_for
from .store import LogStore
from .tools import LogTools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agentcore.logs", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--db", default=None, help="the store (default LOG_DB, then state/logs.db)")
    parser.add_argument("--profiles", default=str(PROFILE_DIR))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="mask and store a service's log file")
    p.add_argument("files", nargs="+")
    p.add_argument("--service", required=True)
    p.add_argument("--append", action="store_true", help="keep what the service already has")
    sub.add_parser("services", help="what the store holds")
    p = sub.add_parser("errors", help="what is going wrong")
    p.add_argument("--service")
    p.add_argument("--mode", default="top", choices=["top", "spikes", "new"])
    p.add_argument("--since")
    p.add_argument("--until")
    p = sub.add_parser("search", help="lines about an identifier")
    p.add_argument("--kind", required=True, choices=["account", "mobile", "reference"])
    p.add_argument("--value", required=True)
    p.add_argument("--service")
    p.add_argument("--since")
    p.add_argument("--until")
    p = sub.add_parser("note", help="record what a signature means")
    p.add_argument("signature")
    p.add_argument("text")
    args = parser.parse_args(argv)

    load_dotenv(args.env_file, override=False)
    key = os.environ.get("LOG_HASH_KEY", "")
    db = args.db or os.environ.get("LOG_DB") or "state/logs.db"
    if args.cmd in ("ingest", "search") and len(key) < 16:
        print("LOG_HASH_KEY must be set (at least 16 characters) in .env or the environment")
        return 1
    store = LogStore(db)
    profiles = load_profiles(Path(args.profiles))
    if args.cmd == "ingest":
        masker = KeyedMasker(key)
        profile = profile_for(args.service, Path(args.profiles))
        for i, file in enumerate(args.files):
            stats = store.ingest(file, profile, masker, replace=not args.append and i == 0)
            print(json.dumps(stats))
    elif args.cmd == "services":
        print(json.dumps(store.services(), indent=1))
    elif args.cmd == "errors":
        tools = LogTools(store, KeyedMasker(key or "x" * 16), profiles)
        print(json.dumps(tools.errors({"service": args.service, "mode": args.mode, "since": args.since,
                                       "until": args.until}), indent=1, default=str))
    elif args.cmd == "search":
        tools = LogTools(store, KeyedMasker(key), profiles)
        out, _ = tools.search({"identifier": {"kind": args.kind, "value": args.value}, "service": args.service,
                               "since": args.since, "until": args.until}, identifier_forms)
        print(json.dumps(out, indent=1, default=str))
    elif args.cmd == "note":
        print("noted" if store.set_note(args.signature, args.text, by=os.environ.get("USERNAME", "")) else "no such signature")
    return 0


if __name__ == "__main__":
    sys.exit(main())
