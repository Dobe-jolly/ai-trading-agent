"""Build preview.html: the console for every desk, with an embedded snapshot of invented demo data.

Each desk's snapshot comes from seeding a throwaway SQLite log and running that desk's real read models
(tools/demo_<desk>.py), so the preview shows exactly what the console shows on the server. No customer data.

    python tools/build_preview.py [--out preview.html] [--fragment]

The file opens straight from disk in a browser. --fragment leaves out the <html>/<head> wrapper (for hosts that
add their own, such as a published artifact).
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from agentcore.portal.server import SERVED_MARKER, STATIC  # noqa: E402
from nmbz import desks as registry  # noqa: E402


def build(out: Path, fragment: bool = False) -> None:
    desks, snapshots = [], {}
    for desk in registry.ALL.values():
        snap = importlib.import_module(f"demo_{desk.key}").snapshot()
        snapshots[desk.key] = snap
        session = snap["session"]
        anomaly = session.get("anomaly") or {}
        desks.append({"key": desk.key, "title": desk.title, "blurb": desk.blurb,
                      "vocab": desk.module("portal.vocab").VOCAB,
                      "configured_mode": session["configured_mode"], "effective_mode": session["effective_mode"],
                      "kill_switch": session["kill_switch"], "approvals_pending": session.get("approvals_pending") or 0,
                      "anomaly": {k: anomaly.get(k) for k in ("severity", "headline", "created_at")} if anomaly else None})
    demo = {"service": {"user": "dobe", "role": "admin", "paused": False, "can_edit": True, "desks": desks},
            "desks": snapshots}
    payload = json.dumps(demo, default=str, separators=(",", ":")).replace("</", "<\\/")
    page = STATIC.read_text(encoding="utf-8").replace(
        SERVED_MARKER, f'<script id="demo-data" type="application/json">{payload}</script>', 1)
    if not fragment:
        page = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                '</head><body>' + page + "</body></html>")
    out.write_text(page, encoding="utf-8")
    counts = ", ".join(f"{k}: {len(v['decisions'])} decisions" for k, v in snapshots.items())
    print(f"Wrote {out} ({len(page) // 1024} KB; {counts})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "preview.html"))
    parser.add_argument("--fragment", action="store_true", help="body only, for a host that adds <html>/<head>")
    args = parser.parse_args()
    build(Path(args.out), args.fragment)
