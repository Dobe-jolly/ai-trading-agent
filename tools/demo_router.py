"""Invented demo data for the router's part of the console preview (tools/build_preview.py).

The snapshot is produced by seeding a throwaway SQLite log and running the console's real read models over it,
so the preview shows what the server returns. No bank data is involved: every ticket here is invented.

    (built by tools/build_preview.py)
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentcore.audit import AuditLog  # noqa: E402
from router.config import Settings  # noqa: E402
from router.portal import data  # noqa: E402
from router.rules import ASK  # noqa: E402

R = random.Random(20261010)
NOW = datetime.now(timezone.utc).replace(microsecond=0)
DAYS = 14
CATCH_ALL = "HelpDesk / Logging Ticket"
# (subject, first message, words searched, target, desk that works it or None, weight)
TICKETS = [
    ("Alerts", "Client not receiving SMS alerts on debits since last week, number on file is <MOBILE_1>.", "alerts",
     "SMS Alerts / Transactional", "SMS Alerts", 12),
    ("Urgent ZIPIT", "Client sent ZIPIT of USD 80 to CBZ on Monday, money not received. Acc <NUMBER_1>.", "zipit",
     "Failed ZIPIT Transaction", "Failed transactions", 10),
    ("B2W", "Client did bank to wallet of USD 20, account debited but wallet not credited.", "wallet",
     "FAILED ECOCASH B2W / WB2", "Failed transactions", 6),
    ("EcoCash link", "Client failing to link EcoCash wallet <MOBILE_1> to account <NUMBER_1>, error on USSD.", "ecocash",
     "Ecocash / Registration", "EcoCash", 7),
    ("Assist", "Client says OTP not coming through when logging in to the NMB Connect app.", "otp",
     "NMB Connect / OTP", None, 9),
    ("Login", "I cannot log in to T24, profile locked after password change.", "locked",
     "HelpDesk / Login", None, 7),
    ("Card", "Client requesting a replacement debit card, old one damaged.", "card",
     "Cards and E-Banking", None, 5),
    ("Accounts not showing", "Client's USD account not visible on NMB Connect after linking.", "visible",
     "NMB Connect / Account", None, 5),
]
STAYS = [("Printer", "Branch printer jamming on every second page, please send someone."),
         ("Request", "Please create a shared folder for the credit team on the file server.")]
UNCLEAR = [("Transfer", "Client says a transfer did not go through.", "transfer",
            ["NMB Connect / Failed Transaction", "Internet Banking / Internal Transfer"])]
VAGUE = [("Assist", "Please assist the client urgently."), ("Urgent", "Kindly help, account <NUMBER_1>.")]


def transcript(number: str, subject: str, message: str, words: str, decision: dict, similar: dict) -> list:
    rendered = (f"Today: {NOW.date()}\nTicket #{number}   Filed under: {CATCH_ALL}   Raised by: Harare Main\n"
                f"Subject: {subject}\n\nForm fields (as captured on the ticket):\n  (none)\n\nThread, oldest first:\n"
                f"--- Ticket owner\n{message}")
    steps = [{"role": "user", "content": rendered + "\n\nWork out which help topic this ticket belongs under, then call submit_decision."}]
    if words:
        steps += [{"role": "assistant", "content": [{"type": "text", "text": "Checking where tickets like this were filed."},
                                                    {"type": "tool_use", "id": "s", "name": "find_similar_closed_tickets",
                                                     "input": {"words": [words]}}]},
                  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "s", "is_error": False,
                                                "content": json.dumps(similar)}]}]
    steps += [{"role": "assistant", "content": [{"type": "tool_use", "id": "d", "name": "submit_decision", "input": decision}]},
              {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "d", "content": "Decision recorded.", "is_error": False}]}]
    return steps


def similar_for(words: str, target: str, n: int) -> dict:
    return {"words": [words], "closed_tickets_found": n + 2,
            "filed_under": [{"topic": target, "tickets": n, "active": True},
                            {"topic": CATCH_ALL, "tickets": 2, "active": True, "catch_all": True}],
            "examples": [{"subject": f"{words.title()} issue", "filed_under": target}]}


def seed(audit: AuditLog) -> None:
    db = audit.db
    go_live = NOW - timedelta(days=4, hours=3)
    t = NOW - timedelta(days=DAYS)
    runs = []
    while t < NOW - timedelta(minutes=8):
        mode = "live" if t >= go_live else "shadow"
        rid = f"{R.getrandbits(48):012x}"
        runs.append((rid, t, mode))
        db.execute("INSERT INTO runs (run_id, started_at, finished_at, mode, model, tickets_seen, decisions, errors) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (rid, t.isoformat(), (t + timedelta(seconds=R.randint(15, 70))).isoformat(),
                                                      mode, "claude-sonnet-5-5", R.randint(0, 5), 0, 0))
        t += timedelta(minutes=15)
    kinds = {"routed": 61, "stays": 10, "unclear": 6, "vague": 9}
    branches = ["Harare Main", "Bulawayo", "Gweru", "Mutare", "Borrowdale", "Msasa", "Head Office ICT"]
    number = 710100
    for day_back in range(DAYS - 1, -1, -1):
        for when in sorted((NOW - timedelta(days=day_back, hours=R.uniform(0.3, 10))).replace(second=0)
                           for _ in range(R.randint(3, 7))):
            run = min(runs, key=lambda r: abs((r[1] - when).total_seconds()))
            live = run[2] == "live"
            number += R.randint(1, 5)
            kind = R.choices(list(kinds), weights=list(kinds.values()))[0]
            notes, alternatives, reply, target, desk = [], [], "", "", None
            confidence = round(R.uniform(0.86, 0.98), 2)
            if kind == "routed":
                subject, message, words, target, desk, _ = R.choices(TICKETS, weights=[x[5] for x in TICKETS])[0]
                proposed = "move"
                if desk is None:  # only topics an agent works are moved to on their own: a suggestion by design
                    proposed, final, category = "escalate", "escalate", "staff_topic"
                elif R.random() < 0.08:
                    confidence = round(R.uniform(0.62, 0.83), 2)
                    final, category = "escalate", "desk_topic"
                    notes = [f"confidence {confidence:.2f} is below 0.85"]
                else:
                    final, category = "move", "desk_topic"
                reason = (f"The owner describes {subject.lower()} trouble: '{message[:60]}...'. Similar closed tickets were "
                          f"filed under {target}.")
                executed = (["moved", "note"] if final == "move" else ["note"]) if live else []
            elif kind == "stays":
                subject, message = R.choice(STAYS)
                words, proposed, final, category = "", "no_action", "no_action", "stays"
                reason = "A general IT request with no more specific topic; it belongs where it is."
                executed = []
            elif kind == "unclear":
                subject, message, words, alternatives = R.choice(UNCLEAR)
                target, proposed, final, category = alternatives[0], "escalate", "escalate", "unclear"
                confidence = round(R.uniform(0.5, 0.7), 2)
                reason = "The owner does not say whether the transfer was made in the app or on internet banking."
                executed = ["note"] if live else []
                alternatives = alternatives[1:]
            else:
                subject, message = R.choice(VAGUE)
                words, proposed, final, category = "", "request_info", "request_info", "no_description"
                reply = ASK
                reason = "The ticket does not say what the problem is."
                executed = ["reply", "answered", "email queued"] if live else []
            decision = {"action": proposed, "confidence": confidence, "category": category, "reason": reason,
                        "evidence": [f"similar closed tickets: {target}"] if target else ["no description of the problem"],
                        "target_topic": target, "alternatives": alternatives, "reply_text": reply}
            branch = R.choice(branches)
            did = audit.record(
                run_id=run[0], created_at=when.isoformat(timespec="seconds"), mode=run[2], ticket_id=number - 600000,
                ticket_number=str(number), last_entry_id=number * 3, last_message_id=number * 3, proposed_action=proposed,
                final_action=final, confidence=confidence, category=category, reason=reason,
                evidence=decision["evidence"], account_statuses={"topic": target} if target else {},
                reply_text=reply or None, template_reply=reply or None, guardrail_notes=notes,
                tools_used=["find_similar_closed_tickets"] if words else [], executed=executed, deferred=0, branch=branch,
                facts={"route": {"from": CATCH_ALL, "to": target or None, "desk": desk, "alternatives": alternatives,
                                 "similar": {target: R.randint(3, 14)} if target else {}, "branch": branch}},
                transcript=transcript(str(number), subject, message, words, decision,
                                      similar_for(words, target, R.randint(3, 14)) if words else {}),
                input_tokens=R.randint(3800, 7000), output_tokens=R.randint(180, 420))
            if live and final == "request_info":
                db.execute("INSERT INTO notifications (uuid, created_at, decision_id, ticket_id, ticket_number, kind, channel, "
                           "recipient, cc, subject, status, attempts, sent_at) VALUES (?, ?, ?, ?, ?, ?, 'gateway', ?, '', ?, ?, ?, ?)",
                           (f"demo-r-{number}", when.isoformat(), did, number - 600000, str(number), "request_info",
                            f"{R.choice(['t.moyo', 'r.ncube', 'p.dube'])}@{branch.lower().replace(' ', '')}.branch.example",
                            f"[#{number}] Ticket: please describe the problem", "sent", 1,
                            (when + timedelta(minutes=1)).isoformat()))
    for at, actor, action, detail in [(NOW - timedelta(days=13, hours=5), "dobe", "hold at shadow", "new desk: first week in shadow"),
                                      (go_live, "dobe", "release mode hold", "shadow routes checked against how staff filed them")]:
        db.execute("INSERT INTO control_events (created_at, actor, action, detail) VALUES (?, ?, ?, ?)",
                   (at.isoformat(timespec="seconds"), actor, action, detail))
    db.commit()
    last = runs[-1][0]
    rows = db.execute("SELECT id, ticket_id, ticket_number, created_at, final_action FROM decisions WHERE mode = 'live' "
                      "AND final_action IN ('escalate', 'request_info', 'no_action') ORDER BY id DESC LIMIT 6").fetchall()
    audit.replace_queue(last, [{"ticket_id": r["ticket_id"], "ticket_number": r["ticket_number"], "help_topic": CATCH_ALL,
                                "opened": r["created_at"], "state": "skipped",
                                "detail": {"escalate": "already escalated", "request_info": "waiting on ticket owner",
                                           "no_action": "unchanged since last decision"}[r["final_action"]],
                                "decision_id": r["id"]} for r in rows])


def seed_approval(audit: AuditLog) -> None:
    from router.approval import ApprovalStore

    store = ApprovalStore(audit.db)
    run = audit.db.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()["run_id"]
    when = (NOW - timedelta(hours=1.4)).isoformat(timespec="seconds")
    number = "710988"
    reply = ("Kindly confirm whether the client made the transfer on the NMB Connect app or on internet banking, and "
             "share a screenshot of the error, so that we can send the ticket to the right team.")
    why = "the ticket already describes the problem, so the standard outcome is move, not request info"
    decision = {"action": "request_info", "confidence": 0.88, "category": "unclear", "reason": "Could be either channel.",
                "evidence": ["the owner does not name the channel"], "target_topic": "NMB Connect / Failed Transaction",
                "alternatives": ["Internet Banking / Internal Transfer"], "reply_text": reply,
                "follows_standard_outcome": False}
    did = audit.record(run_id=run, created_at=when, mode="live", ticket_id=110988, ticket_number=number,
                       last_entry_id=2132964, last_message_id=2132964, proposed_action="request_info",
                       final_action="request_info", confidence=0.88, category="no_description",
                       reason=decision["reason"], evidence=decision["evidence"],
                       account_statuses={"topic": "NMB Connect / Failed Transaction"}, reply_text=reply,
                       guardrail_notes=[], executed=["awaiting approval"], tools_used=["find_similar_closed_tickets"],
                       approval=f"waiting: {why}", branch="Bulawayo",
                       transcript=transcript(number, "Transfer", "Client says a transfer of USD 200 did not go through.",
                                             "transfer", decision, similar_for("transfer", "NMB Connect / Failed Transaction", 6)),
                       input_tokens=6100, output_tokens=330)
    aid = store.create(decision_id=did, ticket_id=110988, ticket_number=number, last_entry_id=2132964,
                       action="request_info", why=why, reply_masked=reply, reply_text="(demo)", token_map={})
    audit.db.execute("UPDATE approvals SET created_at = ? WHERE id = ?", (when, aid))
    audit.db.commit()


def snapshot() -> dict:
    """This desk's console data from a throwaway log seeded with invented tickets."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "demo.sqlite"
        audit = AuditLog(db_path)
        seed(audit)
        seed_approval(audit)
        conn = audit.db
        settings = replace(Settings.from_env(Path(tmp) / "none.env"), state_db=db_path, mode="live",
                           reply_staff_id=531, model="claude-sonnet-5-5")
        summaries = [data.summary(r) for r in conn.execute(
            f"SELECT {data.SUMMARY_COLUMNS} FROM decisions d ORDER BY d.id DESC").fetchall()]
        details = {str(s["id"]): data.decision_detail(conn, s["id"]) for s in summaries}
        snap = {
            "session": {"user": "dobe", "role": "admin", "demo": True, "configured_mode": "live", "effective_mode": "live",
                        "kill_switch": False, "model": settings.model, "reply_staff_id": 531,
                        "osticket_ticket_url": None, "stale_after_minutes": settings.portal_stale_minutes,
                        "notify_transport": "gateway", "anomaly": None,
                        "approvals_pending": data.pending_approvals(conn), "approval_mode": "unscripted"},
            "approvals": data.approvals(conn),
            "live": data.live(conn),
            "overview": data.overview(conn, 14, settings.portal_stale_minutes, 3.0, 15.0),
            "queue": data.queue(conn),
            "runs": data.runs(conn, 60),
            "quality": data.quality(conn, 30),
            "controls": {"kill_switch": False, "mode_override": None, "configured_mode": "live", "effective_mode": "live",
                         "can_edit": True, "events": data.control_events(conn)},
            "notifications": {**data.notifications(conn, {"days": "30"}), "can_retry": True, "transport": "gateway"},
            "anomalies": data.anomalies(conn),
            "anomaly_reports": {},
            "decisions": summaries,
            "details": details,
        }
        audit.close()
    return snap
