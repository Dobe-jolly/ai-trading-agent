"""Run the real console over invented demo data, for working on the portal without the bank's systems.

Each desk's log is seeded by the same demo modules that build preview.html (tools/demo_<desk>.py), into
state/demo/ (or --dir), and served by the real console code (nmbz.console) with two sign-ins:

    dobe    / demo-password-123   admin   (approve, reject, hold, pause, re-send)
    checker / demo-password-123   admin   (a second Admin: switches on notes dobe taught, maker-checker)
    viewer  / demo-password-123   viewer  (read, review, teach and try notes)

    python tools/demo_console.py [--port 8085] [--host 127.0.0.1] [--dir state/demo] [--keep]

Approvals, reviews, holds and pauses made in the portal are written to the demo logs and files, exactly as on the
server; nothing runs the agents, so an approved reply stays "approved, posts on the next pass". "Try it on this
ticket" is simulated here (marked as such; no model call). Re-run without --keep for fresh data. The portal's dev server (portal-ui: npm run dev) sends its API calls here.
No bank data is involved; nothing here reaches Metabase, osTicket or the model.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import shutil
import sys
import time
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from agentcore.audit import AuditLog  # noqa: E402
from agentcore.knowledge import Draft, Item, KnowledgeStore  # noqa: E402
from agentcore.pii import TOKEN_RE  # noqa: E402
from agentcore.portal.server import make_handler  # noqa: E402
from agentcore.portal.users import UserStore  # noqa: E402
from nmbz import desks as registry  # noqa: E402
from nmbz.console import Console  # noqa: E402

PASSWORD = "demo-password-123"
USERS = (("dobe", "admin"), ("checker", "admin"), ("viewer", "viewer"))
SEEDERS = ("seed", "seed_anomalies", "seed_approvals", "seed_approval")


# Invented stand-ins for the masked values in held replies, so an edited reply passes the same checks as on the server.
FAKE_VALUE = {"MOBILE": "07710000{:02d}", "NUMBER": "100000000{:02d}", "EMAIL": "client{:02d}@example.invalid",
              "NATIONAL_ID": "63-0000{:02d}A00"}


def give_held_replies_tokens(audit: AuditLog) -> None:
    """The demo seeders hold replies without the token map a real run keeps; invent one per held reply."""
    if not audit.db.execute("SELECT name FROM sqlite_master WHERE name = 'approvals'").fetchone():
        return
    for row in audit.db.execute("SELECT id, reply_masked FROM approvals WHERE status = 'pending'").fetchall():
        tokens = sorted(set(TOKEN_RE.findall(row["reply_masked"] or "")))
        values = {t: FAKE_VALUE[t[1:-1].rsplit("_", 1)[0]].format(i + 1) for i, t in enumerate(tokens)}
        audit.db.execute("UPDATE approvals SET token_map = ? WHERE id = ?", (json.dumps(values), row["id"]))
    audit.db.commit()


# Invented notes, as staff would teach them from a ticket's "Teach the agent" step: (taught by, approved by or None, note)
DEMO_NOTES: dict[str, list[tuple[str, str | None, Draft]]] = {
    "failed_txn": [
        ("viewer", "dobe", Draft(
            title="Response 91 means the issuer timed out",
            body="When Postilion shows response 91 on the 0200 leg the issuer did not answer in time. The client may still "
                 "have been debited, so check core banking for a debit on the same day before replying. A debit with no "
                 "reversal goes to Card Operations.",
            topic=None, query_db="POST OFFICE",
            query_sql="SELECT TOP 20 datetime_req, message_type, rsp_code_rsp, sink_node_name\nFROM post_tran_leg WITH (NOLOCK)\n"
                      "WHERE from_account_id = {{account}} AND CONVERT(date, datetime_req) = {{day}}\nORDER BY datetime_req DESC",
            query_slots={"account": "account", "day": "date"},
            query_about="The switch legs for the client's account on one day, newest first, with response codes and the "
                        "node that answered. Use it when a card or ZIPIT transaction may have timed out.")),
        ("viewer", None, Draft(
            title="Them on Us declines with 51 are the other bank's",
            body="A Them on Us transaction (another bank's card on our ATM or POS) declined with 51 means the client's own "
                 "bank refused it for insufficient funds. Nothing was debited at NMB, so tell the branch to refer the client "
                 "to their bank.",
            example="2026-10-11 09:14:02 0200 tran_type=01 rsp=51 source=VisaAcq sink=NmbZswAcq amount=USD 40.00")),
        ("dobe", None, Draft(
            title="ZIPIT reversals can take until the next business day",
            body="A ZIPIT that failed after the debit is reversed by the switch, usually within the hour but sometimes on the "
                 "next business day. If the reversal is not there yet and the debit is under 24 hours old, ask the branch to "
                 "check again tomorrow instead of escalating.")),
    ],
    "ecocash": [
        ("viewer", "dobe", Draft(
            title="Status 'PENDING_OTP' means the client never confirmed",
            body="A link attempt left at PENDING_OTP means the client did not enter the one-time code EcoCash sent. Ask the "
                 "branch to have the client start the link again and confirm it within five minutes; nothing needs fixing.")),
    ],
}


def seed_notes(key: str, audit: AuditLog) -> None:
    """A few notes per desk, one switched on and used by recent decisions, so the Knowledge page has something to show."""
    store = KnowledgeStore(audit.db)
    used = []
    for by, approver, draft in DEMO_NOTES.get(key, []):
        note = store.propose(draft, by)
        if approver:
            store.decide(note["id"], "approve", approver, "admin", "Checked against last week's tickets.")
            used.append(note["id"])
    audit.db.execute("UPDATE decisions SET prompt_version = ?, knowledge_active = ?",
                     (hashlib.sha256(key.encode()).hexdigest()[:12], json.dumps(used)))
    if used:
        audit.db.execute("UPDATE decisions SET knowledge_used = ? WHERE id IN (SELECT id FROM decisions "
                         "WHERE final_action = 'reply_and_close' ORDER BY id DESC LIMIT 3)", (json.dumps(used[:1]),))
    audit.db.commit()


def demo_trial(ticket_number: str, item: Item, original: dict | None) -> dict:
    """What "Try it on this ticket" shows in the demo: the original decision, re-labelled. No model call."""
    time.sleep(3)  # long enough to see the console poll a running trial
    o = original or {}
    action = o.get("final_action") or "escalate"
    tools = ["find_switch_transactions"] + (["k0_draft_lookup"] if item.query_sql else [])
    return {
        "proposed_action": action, "final_action": action,
        "confidence": min(0.97, float(o.get("confidence") or 0.8) + 0.05), "category": "demo",
        "reason": f"Simulated in the demo console with the draft note \u201c{item.title}\u201d. {o.get('reason') or ''}".strip(),
        "evidence": [f"DRAFT: {item.title}"], "reply_text": o.get("reply_text"), "would_post": o.get("reply_text"),
        "guardrail_notes": [], "deferred": False, "tools_used": tools, "draft_used": True, "knowledge_used": [],
        "prompt_version": "demo00000000", "input_tokens": 6100, "output_tokens": 420, "transcript": [], "demo": True,
    }


def seed_desk(key: str, db_path: Path) -> None:
    module = importlib.import_module(f"demo_{key}")
    audit = AuditLog(db_path)
    try:
        for name in SEEDERS:
            fn = getattr(module, name, None)
            if fn is not None:
                fn(audit)
        give_held_replies_tokens(audit)
        seed_notes(key, audit)
    finally:
        audit.close()


def build(root: Path, keep: bool = False) -> Console:
    if root.exists() and not keep:
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    users = UserStore(root / "portal_users.json")
    for name, role in USERS:
        if name not in users.users():
            users.set(name, PASSWORD, role)
    desk_settings = {}
    for desk in registry.ALL.values():
        folder = root / desk.key
        folder.mkdir(exist_ok=True)
        db_path = folder / "agent.sqlite"
        if not db_path.exists():
            seed_desk(desk.key, db_path)
        desk_settings[desk.key] = replace(
            desk.settings(str(root / "none.env")), state_db=db_path, kill_switch_file=folder / "STOP",
            mode_override_file=folder / "MODE_OVERRIDE", portal_users_file=users.path, mode="live")
    console = Console(desk_settings, users, stop_file=root / "STOP")
    for portal in console.portals.values():
        portal.trials.execute = demo_trial
    return console


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8085)
    parser.add_argument("--dir", default=str(ROOT / "state" / "demo"), help="where the demo logs go (wiped unless --keep)")
    parser.add_argument("--keep", action="store_true", help="keep the existing demo logs and what was done to them")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = build(Path(args.dir), args.keep)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(console))
    server.daemon_threads = True
    where = f"http://{args.host}:{args.port}"
    print(f"Demo console on {where}  (data: {args.dir})")
    for name, role in USERS:
        print(f"  sign in as {name:7} / {PASSWORD}   {role}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
