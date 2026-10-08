"""Invented demo data for the failed-transactions desk's part of the console preview (tools/build_preview.py).

The snapshot is produced by seeding a throwaway SQLite log and running the console's real read models over it,
so the preview shows what the server returns. No bank data is involved: every number and name here is invented.

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
from failed_txn.config import Settings  # noqa: E402
from failed_txn.portal import data  # noqa: E402

R = random.Random(20261009)
NOW = datetime.now(timezone.utc).replace(microsecond=0)
DAYS = 14
TOPICS = ["Failed ZIPIT Transaction", "Ecocash / Failed Transactions", "FAILED ECOCASH B2W / WB2",
          "NMB Connect / Failed Transaction", "NMB Connect / USSD / Failed Transactions / Airtime", "FAILED ZESA",
          "Postilion transactions / POS", "Postilion transactions / ATM"]
CHANNELS = {
    "Failed ZIPIT Transaction": ("find_switch_transactions", "card/switch transfer", "ZIPIT"),
    "Ecocash / Failed Transactions": ("find_switch_transactions", "card/switch payment", "EcoCash wallet-to-bank"),
    "FAILED ECOCASH B2W / WB2": ("find_switch_transactions", "card/switch transfer", "EcoCash bank-to-wallet"),
    "NMB Connect / Failed Transaction": ("find_nmb_connect_transactions", "NMB Connect internal transfer (IFT)", "transfer"),
    "NMB Connect / USSD / Failed Transactions / Airtime": ("find_nmb_connect_transactions", "Econet airtime", "airtime purchase"),
    "FAILED ZESA": ("find_nmb_connect_transactions", "ZESA token", "ZESA token purchase"),
    "Postilion transactions / POS": ("find_switch_transactions", "card/switch purchase", "card payment"),
    "Postilion transactions / ATM": ("find_switch_transactions", "card/switch cash withdrawal", "ATM withdrawal"),
}
# status -> (weight, final action, system detail, reply)
CASES = {
    "declined": (20, "reply_and_close", "response 51 (insufficient funds)",
                 "The {what} of USD {amount} on {when} was declined (insufficient funds) and no money left the client's "
                 "account. The client can try again once the account is funded."),
    "succeeded": (10, "reply_and_close", "response 00 (approved)",
                  "The {what} of USD {amount} on {when} (reference <NUMBER_2>) went through successfully. If the recipient "
                  "has not received it, kindly ask them to check with their bank or service provider, quoting reference "
                  "<NUMBER_2>."),
    "reversed": (14, "reply_and_close", "response 91 (issuer or switch inoperative (timeout)); Postilion marks it fully reversed",
                 "The {what} of USD {amount} on {when} did not go through and was reversed, so the money is back in the "
                 "client's account. Kindly ask the client to check the balance and try again if needed."),
    "failed_no_debit": (10, "reply_and_close", "status FAILED, code 907",
                        "The {what} of USD {amount} on {when} did not go through, and core banking shows no debit for "
                        "it, so no money left the client's account. The client can try again."),
    "failed_refunded": (5, "reply_and_close", "status FAILED, code 999",
                        "The {what} of USD {amount} on {when} did not go through. The client was debited, and the amount "
                        "was credited back the next day. Kindly ask the client to check the balance."),
    "reversal_pending": (10, "escalate", "status FLAG_REVERSAL, code 06", ""),
    "uncertain": (9, "escalate", "response 91 (issuer or switch inoperative (timeout))", ""),
    "reversal_failed": (4, "escalate", "status REVERSAL_FAILED", ""),
    "not_found": (12, "request_info", "",
                  "We could not find this transaction on account <NUMBER_1>. Kindly confirm the account it was made "
                  "from, the date and time, the amount and, if available, the reference (RRN) or a screenshot of the "
                  "message the client received."),
}
REASONS = {
    "reply_and_close": "Found the transaction on the account and confirmed it: {status}. The standard outcome fits and "
                       "the thread does not contradict it.",
    "request_info": "Searched the switch and NMB Connect for the account in the last 30 days: nothing of that amount.",
    "escalate": "Confirmed the transaction: {status}. The client may be out of pocket; operations must check the reversal.",
}


def pick(rng, weights):
    total = sum(weights.values())
    roll = rng.uniform(0, total)
    for key, w in weights.items():
        roll -= w
        if roll <= 0:
            return key
    return key


def transcript(number: str, topic: str, status: str, action: str, reply: str, reason: str, amount: str,
               detail: str, standard: bool = True) -> list:
    tool, channel, what = CHANNELS[topic]
    rendered = (f"Today: {NOW.date()}\nTicket #{number}   Help topic: {topic}   Raised by: Harare Main\n"
                f"Subject: Failed {what}\n\nForm fields (as captured on the ticket):\n  Issue Details: client says the "
                f"{what} of USD {amount} failed\n\nDetails the ticket owner gave (form, messages and their attachments):\n"
                f"  Account numbers: <NUMBER_1>\n  References: (none)\n  Amounts: {amount}\n  Dates: (none)\n"
                f"  Mobile numbers: (none)\n\nThread, oldest first:\n--- Ticket owner\nGood day, kindly assist the client: "
                f"{what} of ${amount} from account <NUMBER_1> failed yesterday.")
    txn = {"id": "T1", "source": "postilion" if tool == "find_switch_transactions" else "conect", "channel": channel,
           "when": "07-10-2026 14:03", "amount": f"USD {amount}", "account": "<NUMBER_1>",
           "status": status if status in ("declined", "succeeded", "reversed", "uncertain", "reversal_pending",
                                          "reversal_failed") else "failed", "detail": detail, "reference": "<NUMBER_2>"}
    steps = [{"role": "user", "content": rendered + "\n\nFind the transaction with the lookups, confirm it, then call submit_decision."},
             {"role": "assistant", "content": [{"type": "text", "text": f"Looking for the {what} on the account."},
                                               {"type": "tool_use", "id": "t1", "name": tool,
                                                "input": {"account_number": "<NUMBER_1>", "amount": amount}}]}]
    if status == "not_found":
        steps.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": False,
                                                   "content": json.dumps({"found": 0, "by_reading": {}, "transactions": []})}]})
    else:
        steps += [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": False,
                                                "content": json.dumps({"found": 1, "by_reading": {txn["status"]: 1},
                                                                       "transactions": [txn]})}]},
                  {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "confirm_transaction",
                                                     "input": {"transaction_id": "T1"}}]},
                  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "is_error": False,
                                                "content": json.dumps({"confirmed": txn, "where_this_leaves_the_client": status,
                                                                       "amount_matches_ticket": "yes",
                                                                       "standard_outcome": {"action": action, "reply": reply}})}]}]
    decision = {"action": action, "confidence": 0.93, "category": status, "reason": reason, "evidence": [detail or "nothing found"],
                "reply_text": reply, "follows_standard_outcome": standard}
    steps += [{"role": "assistant", "content": [{"type": "tool_use", "id": "d", "name": "submit_decision", "input": decision}]},
              {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "d", "content": "Decision recorded.", "is_error": False}]}]
    return steps


def seed(audit: AuditLog) -> None:
    db = audit.db
    go_live = NOW - timedelta(days=5, hours=2)
    t = NOW - timedelta(days=DAYS)
    runs = []
    while t < NOW - timedelta(minutes=8):
        mode = "live" if t >= go_live else "shadow"
        rid = f"{R.getrandbits(48):012x}"
        runs.append((rid, t, mode))
        db.execute("INSERT INTO runs (run_id, started_at, finished_at, mode, model, tickets_seen, decisions, errors) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (rid, t.isoformat(), (t + timedelta(seconds=R.randint(30, 150))).isoformat(),
                                                      mode, "claude-sonnet-5-5", R.randint(2, 9), 0, 1 if R.random() < 0.02 else 0))
        t += timedelta(minutes=15)
    weights = {k: v[0] for k, v in CASES.items()}
    branches = ["Harare Main", "Bulawayo", "Gweru", "Mutare", "Borrowdale", "Msasa", "Chitungwiza"]
    number = 700200
    ids = []
    for day_back in range(DAYS - 1, -1, -1):
        for when in sorted((NOW - timedelta(days=day_back, hours=R.uniform(0.3, 10))).replace(second=0)
                           for _ in range(R.randint(3, 8))):
            run = min(runs, key=lambda r: abs((r[1] - when).total_seconds()))
            number += R.randint(1, 4)
            status = pick(R, weights)
            topic = R.choice(TOPICS)
            weight, action, detail, reply = CASES[status]
            what = CHANNELS[topic][2]
            amount = f"{R.choice([5, 10, 20, 35, 50, 100, 150, 300]):.2f}"
            reply = reply.format(what=what, amount=amount, when="07/10/2026 14:03")
            proposed, notes, confidence = action, [], round(R.uniform(0.86, 0.99), 2)
            if status in ("uncertain", "reversal_pending") and R.random() < 0.3:
                proposed = "reply_and_close"
                notes = [f"close blocked: the confirmed transaction is {status}, which needs a person"]
            if status == "declined" and R.random() < 0.08:
                action, proposed, reply = "escalate", "reply_and_close", ""
                notes = ["close blocked: declined, but core banking shows the client was debited"]
            reason = REASONS[action].format(status=status)
            executed = []
            if run[2] == "live":
                executed = {"reply_and_close": ["reply", "close", "email queued"],
                            "request_info": ["reply", "answered", "email queued"], "escalate": ["note"]}[action]
            branch = R.choice(branches)
            tools = [CHANNELS[topic][0]] + ([] if status == "not_found" else ["confirm_transaction"])
            facts = None if status == "not_found" else {"confirmed": {
                "source": "postilion" if CHANNELS[topic][0] == "find_switch_transactions" else "conect",
                "channel": CHANNELS[topic][1], "status": status, "amount": amount, "currency": "USD",
                "debited": "no" if status in ("declined", "failed_no_debit") else "yes", "branch": branch}}
            ids.append(audit.record(
                run_id=run[0], created_at=when.isoformat(timespec="seconds"), mode=run[2], ticket_id=number - 600000,
                ticket_number=str(number), last_entry_id=number * 3, last_message_id=number * 3, proposed_action=proposed,
                final_action=action, confidence=confidence, category=status, reason=reason,
                evidence=[detail or "nothing found"], account_statuses={"transaction": status},
                reply_text=reply or None, template_reply=reply or None, guardrail_notes=notes, tools_used=tools,
                executed=executed, deferred=0, facts=facts, branch=branch,
                transcript=transcript(str(number), topic, status, proposed, reply, reason, amount, detail),
                input_tokens=R.randint(5200, 11000), output_tokens=R.randint(350, 900)))
            if run[2] == "live" and action != "escalate":
                db.execute("INSERT INTO notifications (uuid, created_at, decision_id, ticket_id, ticket_number, kind, channel, "
                           "recipient, cc, subject, status, attempts, sent_at) VALUES (?, ?, ?, ?, ?, ?, 'gateway', ?, '', ?, ?, ?, ?)",
                           (f"demo-{number}", when.isoformat(), ids[-1], number - 600000, str(number), action,
                            f"{R.choice(['t.moyo', 'r.ncube', 'p.dube'])}@{branch.lower().replace(' ', '')}.branch.example",
                            f"[#{number}] Failed transaction ticket " + ("resolved" if action == "reply_and_close"
                                                                          else "information needed"),
                            "sent" if R.random() > 0.04 else "failed", 1, (when + timedelta(minutes=1)).isoformat()))
    for did in ids[: int(len(ids) * 0.6)]:
        if R.random() < 0.45:
            row = db.execute("SELECT created_at FROM decisions WHERE id = ?", (did,)).fetchone()
            wrong = R.random() < 0.07
            db.execute("INSERT INTO reviews (decision_id, created_at, reviewer, verdict, expected_action, comment) VALUES (?, ?, ?, ?, ?, ?)",
                       (did, (datetime.fromisoformat(row["created_at"]) + timedelta(hours=3)).isoformat(timespec="seconds"),
                        R.choice(["dobe", "tsitsi.m"]), "incorrect" if wrong else "correct", "escalate" if wrong else None,
                        "Client was debited twice; needed a reversal." if wrong else None))
    for at, actor, action, detail in [(NOW - timedelta(days=13, hours=4), "dobe", "hold at shadow", "new desk: first week in shadow"),
                                      (go_live, "dobe", "release mode hold", "shadow decisions reviewed against Postilion Office")]:
        db.execute("INSERT INTO control_events (created_at, actor, action, detail) VALUES (?, ?, ?, ?)",
                   (at.isoformat(timespec="seconds"), actor, action, detail))
    db.commit()
    last = runs[-1][0]
    open_rows = db.execute("SELECT id, ticket_id, ticket_number, created_at, final_action FROM decisions WHERE mode = 'live' "
                           "AND final_action IN ('escalate', 'request_info') ORDER BY id DESC LIMIT 7").fetchall()
    audit.replace_queue(last, [{"ticket_id": r["ticket_id"], "ticket_number": r["ticket_number"], "help_topic": R.choice(TOPICS),
                                "opened": r["created_at"], "state": "skipped",
                                "detail": "already escalated" if r["final_action"] == "escalate" else "waiting on ticket owner",
                                "decision_id": r["id"]} for r in open_rows])


APPROVAL_CASES = [
    dict(ticket="700512", hours=0.9, status="declined", action="reply_and_close", topic="Failed ZIPIT Transaction",
         amount="150.00", detail="response 51 (insufficient funds)",
         reply=("Both ZIPIT transfers from account <NUMBER_1> on 07/10/2026 were declined and no money left the account: "
                "USD 150.00 at 09:12 (insufficient funds) and USD 100.00 at 09:20 (exceeds the transaction limit). Kindly "
                "advise the client to fund the account and send up to the daily ZIPIT limit, or ask the branch about the limit."),
         why="the agent reasoned beyond the standard outcome for the confirmed transaction (two transactions on one ticket)"),
    dict(ticket="700507", hours=2.3, status="failed_refunded", action="reply_and_close", topic="FAILED ZESA",
         amount="20.00", detail="status FAILED, code 999",
         reply=("The ZESA token purchase of USD 20.00 on 06/10/2026 did not go through. The client was debited and the "
                "amount was credited back on 07/10/2026 (narration REV ZESA). The token the client is asking about "
                "(meter <NUMBER_3>) was never issued, so kindly ask the client to buy it again."),
         why="the agent reasoned beyond the standard outcome for the confirmed transaction"),
]


def seed_approvals(audit: AuditLog) -> None:
    from failed_txn.approval import ApprovalStore

    store = ApprovalStore(audit.db)
    run = audit.db.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()["run_id"]
    for c in APPROVAL_CASES:
        when = (NOW - timedelta(hours=c["hours"])).isoformat(timespec="seconds")
        tid = int(c["ticket"]) - 600000
        did = audit.record(run_id=run, created_at=when, mode="live", ticket_id=tid, ticket_number=c["ticket"],
                           last_entry_id=int(c["ticket"]) * 3, last_message_id=int(c["ticket"]) * 3,
                           proposed_action=c["action"], final_action=c["action"], confidence=0.9, category=c["status"],
                           reason="Confirmed the transactions; the reply goes beyond the standard wording.",
                           evidence=[c["detail"], "confirmed with core banking"], account_statuses={"transaction": c["status"]},
                           reply_text=c["reply"], guardrail_notes=[], executed=["awaiting approval"],
                           tools_used=["find_switch_transactions", "confirm_transaction", "check_account_postings"],
                           approval=f"waiting: {c['why']}", branch="Harare Main",
                           transcript=transcript(c["ticket"], c["topic"], c["status"], c["action"], c["reply"],
                                                 "Confirmed; reasoned beyond the standard wording.", c["amount"],
                                                 c["detail"], standard=False),
                           input_tokens=R.randint(9000, 14000), output_tokens=R.randint(600, 1000))
        aid = store.create(decision_id=did, ticket_id=tid, ticket_number=c["ticket"], last_entry_id=int(c["ticket"]) * 3,
                           action=c["action"], why=c["why"], reply_masked=c["reply"], reply_text="(demo)", token_map={})
        audit.db.execute("UPDATE approvals SET created_at = ? WHERE id = ?", (when, aid))
    audit.db.commit()


def snapshot() -> dict:
    """This desk's console data from a throwaway log seeded with invented tickets."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "demo.sqlite"
        audit = AuditLog(db_path)
        seed(audit)
        seed_approvals(audit)
        conn = audit.db
        settings = replace(Settings.from_env(Path(tmp) / "none.env"), state_db=db_path, mode="live",
                           reply_staff_id=531, model="claude-sonnet-5-5")
        summaries = [data.summary(r) for r in conn.execute(
            f"SELECT {data.SUMMARY_COLUMNS} FROM decisions d ORDER BY d.id DESC").fetchall()]
        details = {str(s["id"]): data.decision_detail(conn, s["id"]) for s in summaries}
        snapshot = {
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
    return snapshot
