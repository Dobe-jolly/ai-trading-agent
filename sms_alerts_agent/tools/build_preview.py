"""Build preview.html: the console with an embedded snapshot of invented demo data.

The snapshot is produced by seeding a throwaway SQLite log and running the portal's real read
models over it, so the preview shows exactly what the server returns. No bank data is involved.

    python tools/build_preview.py [--out preview.html]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.audit import AuditLog  # noqa: E402
from agent.anomaly.detect import SEVERITY_RANK, analyse, incident_key  # noqa: E402
from agent.anomaly.features import gateway_features  # noqa: E402
from agent.anomaly.service import SCHEMA as ANOMALY_SCHEMA, _slim  # noqa: E402
from agent.anomaly.summarize import admin_text, fallback_summary  # noqa: E402
from agent.normalize import LOCAL_ZONE  # noqa: E402
from agent.config import Settings  # noqa: E402
from agent.portal import data  # noqa: E402
from agent.portal.server import SERVED_MARKER, STATIC  # noqa: E402

R = random.Random(20261007)
NOW = datetime.now(timezone.utc).replace(microsecond=0)
DAYS = 14
# The demo incident: the NetOne route starts failing a few hours before the snapshot.
INCIDENT_START = NOW - timedelta(hours=6, minutes=40)
_TWO_DAYS_AGO = datetime.combine((NOW.astimezone(LOCAL_ZONE) - timedelta(days=2)).date(), datetime.min.time(), tzinfo=LOCAL_ZONE)
BURST = (_TWO_DAYS_AGO + timedelta(hours=9), _TWO_DAYS_AGO + timedelta(hours=12))  # earlier ticket surge from one branch
HISTORY_HOURS = 72

CLOSE_STATUSES = [("delivered_check_spam", 40), ("different_number", 20), ("sms_no_customer_email", 10),
                  ("sms_not_enabled", 8), ("no_txn_number_match", 8), ("sms_registration_disabled", 6),
                  ("salaries_match", 5), ("salaries_phone_mismatch", 3)]
ESCALATE_STATUSES = [("socket_failed", 30), ("socket_not_found", 30), ("socket_pending", 15),
                     ("socket_unknown", 10), ("salaries_unverified", 15)]
ASK_STATUSES = [("invalid_ticket_phone", 55), ("invalid_account_number", 45)]

REPLIES = {
    "delivered_check_spam": "The number on the ticket matches the number maintained on Intellect, and the SMS gateway confirmed the alerts for account <NUMBER_1> were delivered. Kindly advise the client to check the SMS spam or blocked folder and confirm our sender ID is not blocked on the handset.",
    "different_number": "The number on the ticket differs from the number maintained on Intellect (<MOBILE_2>). Kindly update the client's correct mobile number on Intellect.",
    "sms_no_customer_email": "The number and account are correct and the alerts for account <NUMBER_1> were generated, but the client has no valid email address in the core customer profile, which the alert platform currently needs to send an SMS. Kindly capture a valid email at the branch and alerts will resume.",
    "sms_not_enabled": "Kindly enable the SMS alerts at account level for account <NUMBER_1>.",
    "no_txn_number_match": "No SMS-enabled transactions were found for account <NUMBER_1> on Intellect in the previous 15 days. The number on the ticket matches Intellect, so the account is set up correctly; there was simply no qualifying activity.",
    "sms_registration_disabled": "The mobile number on file is correct, but SMS-alert registration is switched off for this client on IDCCOB. Kindly enable the client's SMS-alert registration on IDCCOB.",
    "salaries_match": "The number on customer_accounts_idc matches the ticket. Kindly advise the client to check the blocked SMS folder.",
    "salaries_phone_mismatch": "The phone number on the ticket does not match the one on customer_accounts_idc (<MOBILE_2>). Kindly advise ICT Data Science to update the mobile number.",
    "invalid_ticket_phone": "The phone number captured on the ticket (<NUMBER_2>) is not a valid mobile number. Kindly reply with the client's correct mobile number so we can check the alerts.",
    "invalid_account_number": "Account <NUMBER_1> was not found in core banking. Kindly confirm the client's account number.",
}
REASONS = {
    "reply_and_close": "Ran the debit check on {acct}: {status}. The evidence directly supports the standard resolution and the thread has no contradicting information.",
    "escalate": "Ran the debit check on {acct}: {status}. The alert was generated in core banking but delivery could not be proven at the gateway, so the branch cannot fix this; staff need to check the queue.",
    "request_info": "The check returned {status}. The thread does not contain a corrected value, so the owner needs to supply it before the alerts can be checked.",
    "no_action": "The latest entry is the owner's thank-you after an earlier resolution; nothing to do.",
}


def pick(pairs):
    total = sum(w for _, w in pairs)
    roll = R.uniform(0, total)
    for value, weight in pairs:
        roll -= weight
        if roll <= 0:
            return value
    return pairs[-1][0]


DEMO_FILES = {
    "inbox": {"name": "IMG_2031.jpg", "type": "image/jpeg", "size": 184_320, "status": "read",
              "kind": "Screenshot of SMS messages on a phone",
              "summary": "SMS inbox on the client's phone. The last NMB BANK alert shown is from 21-09; nothing after it.\n"
                         "Mobile numbers shown: <MOBILE_1>\nBank SMS visible, newest first:\n"
                         "  [21-09-2026 16:44] NMB BANK: Acc ****8901 debited USD 42.00 at OK MART. Avail bal USD 310.15\n"
                         "  [19-09-2026 09:12] NMB BANK: Acc ****8901 credited USD 600.00 SALARY. Avail bal USD 352.15"},
    "intellect": {"name": "intellect profile.png", "type": "image/png", "size": 412_000, "status": "read",
                  "kind": "Screenshot of a banking system screen",
                  "summary": "Customer profile screen in Intellect for the account. The mobile number field differs from the ticket "
                             "and the e-mail field is empty.\nAccount numbers shown: <NUMBER_1>\nMobile numbers shown: <MOBILE_2>\n"
                             "Other text: SMS alerts: Y; E-mail: (blank)"},
    "list": {"name": "accounts.xlsx", "type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             "size": 9_800, "status": "read", "kind": "Excel workbook",
             "summary": "Account | Phone | Currency\n<NUMBER_1> | <MOBILE_1> | USD\n<NUMBER_2> | <MOBILE_1> | ZWG"},
    "logo": {"name": "image001.png", "type": "image/png", "size": 4_100, "status": "skipped",
             "note": "small image in the message body (an e-mail signature logo)", "kind": "", "summary": ""},
    "heic": {"name": "IMG_5520.HEIC", "type": "image/heic", "size": 2_310_000, "status": "failed",
             "note": "image could not be opened (iPhone HEIC photo: install pillow-heif, or ask for a JPEG/screenshot)",
             "kind": "", "summary": ""},
}
FILE_SETS = [["inbox", "logo"], ["intellect"], ["list", "logo"], ["heic"], ["inbox"]]


def demo_files() -> list:
    names = R.choice(FILE_SETS)
    return [{"number": i, **DEMO_FILES[k]} for i, k in enumerate(names, 1)]


def render_files(files: list) -> str:
    out = []
    for f in files:
        head = f"[Attachment {f['number']}: {f['name']}, {f['type']}, {max(1, round(f['size'] / 1024))} KB]"
        if f["status"] == "read":
            out.append(f"{head} {f['kind']}:\n" + "\n".join("    " + x for x in f["summary"].splitlines()))
        else:
            out.append(f"{head} {'Could not be read' if f['status'] == 'failed' else 'Not read'}: {f['note']}")
    return ("\n" + "\n".join(out)) if out else ""


def transcript(ticket: str, status: str, action: str, reply: str, reason: str, files: list = ()) -> list:
    rendered = (f"Today: {NOW.date()}\nTicket #{ticket}   Help topic: SMS Alerts / Transactional\n\nForm fields:\n"
                "  Phone to receive alerts: <MOBILE_1>\n  Account number: <NUMBER_1>\n  Currency: USD\n"
                "  Salaries or Debits: Debits\n\nThread, oldest first:\n--- Ticket owner\n"
                "Client says she stopped receiving debit alerts on <MOBILE_1> since Monday." + render_files(list(files)))
    result = {"account_number": "<NUMBER_1>", "alert_type": "DEBITS", "status": status,
              "reply": REPLIES.get(status, ""), "auto_close_allowed": not status.startswith("socket_"),
              "standby_trigger_count": R.randint(0, 9)}
    if status.startswith("socket_") or status == "delivered_check_spam":
        result["socket_delivery"] = {"status": status.replace("socket_", "").replace("delivered_check_spam", "delivered"),
                                     "triggers_checked": 3, "detail": "3 alert(s) checked."}
    decision = {"action": action, "confidence": 0.93, "category": status, "reason": reason,
                "evidence": [f"status {status}"], "reply_text": reply}
    return [
        {"role": "user", "content": rendered + "\n\nInvestigate with the checks, then call submit_decision."},
        {"role": "assistant", "content": [{"type": "text", "text": "Checking the account on the form with the form phone."},
                                          {"type": "tool_use", "id": "t1", "name": "check_sms_alert_account",
                                           "input": {"account_number": "<NUMBER_1>", "alert_types": "debits"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": json.dumps(result), "is_error": False}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "submit_decision", "input": decision}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "Decision recorded.", "is_error": False}]},
    ]


def seed(audit: AuditLog) -> None:
    db = audit.db
    # Runs every 15 minutes; shadow for the first week, live after go-live.
    go_live = NOW - timedelta(days=6, hours=3)
    t = NOW - timedelta(days=DAYS)
    runs = []
    while t < NOW - timedelta(minutes=6):
        mode = "live" if t >= go_live else "shadow"
        rid = f"{R.getrandbits(48):012x}"
        dur = R.randint(25, 160)
        errors = 1 if R.random() < 0.02 else 0
        runs.append((rid, t, mode, dur, errors))
        db.execute("INSERT INTO runs (run_id, started_at, finished_at, mode, model, tickets_seen, decisions, errors) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (rid, t.isoformat(), (t + timedelta(seconds=dur)).isoformat(), mode, "claude-sonnet-5-5",
                    R.randint(4, 14), 0, errors))
        t += timedelta(minutes=15)
    db.commit()

    ticket_no = 482100
    incident_day = (NOW - timedelta(days=4)).date()
    outage_day = (NOW - timedelta(days=9)).date()
    decision_ids = []
    for day_back in range(DAYS - 1, -1, -1):
        day = (NOW - timedelta(days=day_back)).date()
        for _ in range(R.randint(6, 13) + (5 if day == incident_day else 0)):
            when = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(
                hours=R.randint(6, 17), minutes=R.choice([0, 15, 30, 45]), seconds=R.randint(30, 140))
            if when > NOW - timedelta(minutes=10):
                continue
            run = min(runs, key=lambda r: abs((r[1] - when).total_seconds()))
            mode = run[2]
            ticket_no += R.randint(1, 4)
            roll = R.random()
            in_incident = when >= INCIDENT_START + timedelta(minutes=40)
            if day == incident_day and roll < 0.55:
                action, status = "escalate", R.choice(["socket_not_found", "socket_not_found", "socket_failed"])
            elif in_incident and roll < 0.6:
                action, status = "escalate", "socket_failed"
            elif roll < 0.56:
                action, status = "reply_and_close", pick(CLOSE_STATUSES)
            elif roll < 0.70:
                action, status = "request_info", pick(ASK_STATUSES)
            elif roll < 0.97:
                action, status = "escalate", pick(ESCALATE_STATUSES)
            else:
                action, status = "no_action", "delivered_check_spam"
            proposed = action
            notes: list[str] = []
            confidence = round(R.uniform(0.86, 0.99), 2)
            if action == "escalate" and status.startswith("socket_") and R.random() < 0.25:
                proposed = "reply_and_close"
                notes = [f"close blocked by check status (<NUMBER_1>={status})"]
            elif action == "escalate" and R.random() < 0.08:
                proposed = "reply_and_close"
                confidence = round(R.uniform(0.6, 0.8), 2)
                notes = [f"confidence {confidence:.2f} is below 0.85"]
            category = status
            if day == outage_day and R.random() < 0.6:
                category = "rules_fallback"
                confidence = 1.0
            error = "Metabase unavailable: STANDBY DB read timed out" if R.random() < 0.015 else None
            reply = REPLIES.get(status, "") if proposed in ("reply_and_close", "request_info") else ""
            if action == "request_info" and status not in REPLIES:
                reply = "Kindly share the client's account number so we can check the alerts."
            reason = REASONS[action].format(acct="<NUMBER_1>", status=status)
            if in_incident and status == "socket_failed":
                reason = ("Ran the debit check on <NUMBER_1>: socket_failed, the gateway reports FAILED_TO_DELIVER via NETONE for "
                          "the client's NetOne number. This matches the open incident (NetOne route failing since "
                          f"{INCIDENT_START.astimezone(LOCAL_ZONE):%H:%M}), so it is escalated with the other tickets rather than "
                          "asking the branch anything.")
            files = demo_files() if not error and R.random() < 0.3 else []
            executed: list[str] = []
            if mode == "live" and not error:
                executed = {"reply_and_close": ["reply", "close"], "request_info": ["reply", "answered"],
                            "escalate": ["note"], "no_action": []}[action]
            decision_ids.append(audit.record(
                run_id=run[0], created_at=when.isoformat(timespec="seconds"), mode=mode, ticket_id=ticket_no - 400000,
                ticket_number=str(ticket_no), last_entry_id=ticket_no * 3, last_message_id=ticket_no * 3,
                proposed_action=None if error else proposed, final_action=None if error else action,
                confidence=None if error else confidence, category=None if error else category,
                reason=None if error else reason, evidence=[f"check_sms_alert_account: {status}"],
                account_statuses=None if error else {"<NUMBER_1>": status},
                reply_text=reply or None, template_reply=REPLIES.get(status) if action == "reply_and_close" else None,
                guardrail_notes=notes, tools_used=["check_sms_alert_account"] + (["trace_alert_pipeline"] if status == "socket_not_found" else []),
                executed=executed, deferred=0, error=error,
                transcript=[] if (error or category == "rules_fallback") else transcript(str(ticket_no), status, proposed, reply, reason, files),
                attachments=files or None,
                input_tokens=(0 if category == "rules_fallback" else R.randint(5200, 9800))
                + 1700 * sum(1 for f in files if f["status"] == "read" and f["type"].startswith("image/")),
                output_tokens=0 if category == "rules_fallback" else R.randint(380, 900),
            ))

    # Reviews on older decisions.
    reviewers = ["dobe", "tsitsi.m", "farai.k"]
    for did in decision_ids[: int(len(decision_ids) * 0.6)]:
        if R.random() < 0.45:
            row = db.execute("SELECT final_action, created_at FROM decisions WHERE id = ?", (did,)).fetchone()
            if not row["final_action"]:
                continue
            wrong = R.random() < 0.09
            expected = None
            comment = None
            if wrong:
                expected = {"reply_and_close": "escalate", "escalate": "reply_and_close",
                            "request_info": "reply_and_close", "no_action": "escalate"}[row["final_action"]]
                comment = R.choice(["The owner had already given the right number in their second message.",
                                    "Gateway was fine by then; the branch only needed the spam-folder advice.",
                                    "Client also mentioned a SIM swap; this needed staff."])
            created = (datetime.fromisoformat(row["created_at"]) + timedelta(hours=R.randint(1, 20))).isoformat(timespec="seconds")
            db.execute("INSERT INTO reviews (decision_id, created_at, reviewer, verdict, expected_action, comment) VALUES (?, ?, ?, ?, ?, ?)",
                       (did, created, R.choice(reviewers), "incorrect" if wrong else "correct", expected, comment))
    db.commit()

    # Closed-ticket replays from before go-live.
    replay_run = f"{R.getrandbits(48):012x}"
    replay_at = NOW - timedelta(days=8, hours=2)
    db.execute("INSERT INTO runs (run_id, started_at, finished_at, mode, model, tickets_seen, decisions, errors) VALUES (?, ?, ?, 'audit', ?, 40, 40, 0)",
               (replay_run, replay_at.isoformat(), (replay_at + timedelta(minutes=22)).isoformat(), "claude-sonnet-5-5"))
    for i in range(40):
        status = pick(CLOSE_STATUSES + [("socket_not_found", 12), ("socket_failed", 6)])
        action = "escalate" if status.startswith("socket_") else "reply_and_close"
        template = REPLIES.get(status, "The number on the ticket matches the number maintained on Intellect. Please advise the client to check the SMS spam folder.")
        actual = template if R.random() < 0.75 else "The number on the ticket matches the number maintained on Intellect. Please advise the client to check the SMS spam folder or blocked SMS folder."
        audit.record(run_id=replay_run, created_at=(replay_at + timedelta(seconds=30 * i)).isoformat(timespec="seconds"),
                     mode="audit", ticket_id=70000 + i, ticket_number=str(481200 + i * 3), proposed_action=action,
                     final_action=action, confidence=0.92, category=status, reason=REASONS[action].format(acct="<NUMBER_1>", status=status),
                     account_statuses={"<NUMBER_1>": status}, reply_text=template if (action == "reply_and_close" and R.random() < 0.4) else None,
                     template_reply=template, actual_reply=actual, guardrail_notes=[], tools_used=["check_sms_alert_account"],
                     executed=[], deferred=0, input_tokens=R.randint(5000, 9000), output_tokens=R.randint(300, 800))

    # Control log.
    for at, actor, action, detail in [
        (NOW - timedelta(days=13, hours=5), "dobe", "hold at shadow", "first week in shadow next to the Java agent"),
        (go_live, "dobe", "release mode hold", "audit replay reviewed; Java sms-agent disabled"),
        (NOW - timedelta(days=4, hours=6), "tsitsi.m", "pause", "gateway incident INC-2231, alerts not leaving the queue"),
        (NOW - timedelta(days=4, hours=2), "tsitsi.m", "resume", "gateway restored"),
    ]:
        db.execute("INSERT INTO control_events (created_at, actor, action, detail) VALUES (?, ?, ?, ?)",
                   (at.isoformat(timespec="seconds"), actor, action, detail))
    db.commit()

    # Branch emails for live replies.
    branches = ["harare.main", "bulawayo", "gweru", "mutare", "borrowdale", "msasa", "kwekwe", "masvingo"]
    live_replies = db.execute(
        "SELECT id, ticket_id, ticket_number, created_at, final_action FROM decisions WHERE mode = 'live' "
        "AND final_action IN ('request_info', 'reply_and_close') AND error IS NULL ORDER BY id").fetchall()
    for i, r in enumerate(live_replies):
        created = datetime.fromisoformat(r["created_at"])
        roll = R.random()
        recent = created > NOW - timedelta(minutes=40)
        status, error, sent, attempts = "sent", None, (created + timedelta(seconds=R.randint(20, 300))).isoformat(timespec="seconds"), 1
        recipient = f"{R.choice(['t.moyo', 'r.ncube', 'p.dube', 's.chikore', 'k.banda'])}@{R.choice(branches)}.branch.example"
        if recent:
            status, sent, attempts = "pending", None, 0
        elif roll < 0.04:
            status, error, sent, attempts = "failed", "gateway returned HTTP 503: service unavailable", None, 5
        elif roll < 0.10:
            status, error, sent, attempts, recipient = "skipped", "the ticket owner has no valid email address in osTicket", None, 0, None
        subject = (f"[#{r['ticket_number']}] SMS alerts ticket: information needed" if r["final_action"] == "request_info"
                   else f"[#{r['ticket_number']}] SMS alerts ticket resolved")
        db.execute("INSERT INTO notifications (uuid, created_at, decision_id, ticket_id, ticket_number, kind, channel, recipient, "
                   "cc, subject, body, status, attempts, last_error, sent_at) VALUES (?, ?, ?, ?, ?, ?, 'gateway', ?, '', ?, ?, ?, ?, ?, ?)",
                   (f"demo-{i}", r["created_at"], r["id"], r["ticket_id"], r["ticket_number"], r["final_action"], recipient,
                    subject, "kept for retry" if status in ("failed", "pending") else None, status, attempts, error, sent))
        executed = json.loads(db.execute("SELECT executed FROM decisions WHERE id = ?", (r["id"],)).fetchone()[0] or "[]")
        db.execute("UPDATE decisions SET executed = ? WHERE id = ?", (json.dumps(executed + ["email queued"]), r["id"]))
    db.commit()

    # The last pass's view of the open queue.
    last = runs[-1][0]
    queue = []
    open_rows = db.execute("SELECT id, ticket_id, ticket_number, created_at, final_action FROM decisions "
                           "WHERE mode = 'live' AND final_action IN ('escalate', 'request_info') ORDER BY id DESC LIMIT 7").fetchall()
    for r in open_rows:
        queue.append({"ticket_id": r["ticket_id"], "ticket_number": r["ticket_number"], "help_topic": "SMS Alerts / Transactional",
                      "opened": r["created_at"], "state": "skipped",
                      "detail": "already escalated" if r["final_action"] == "escalate" else "waiting on ticket owner", "decision_id": r["id"]})
    newest = db.execute("SELECT id, ticket_id, ticket_number, created_at, final_action FROM decisions WHERE mode = 'live' "
                        "AND final_action IS NOT NULL ORDER BY id DESC LIMIT 2").fetchall()
    for r in newest:
        if any(q["ticket_id"] == r["ticket_id"] + 1 for q in queue):
            continue
        queue.append({"ticket_id": r["ticket_id"] + 1, "ticket_number": str(int(r["ticket_number"]) + 1), "help_topic": "SMS Alerts / Salaries",
                      "opened": r["created_at"], "state": r["final_action"], "detail": None, "decision_id": r["id"]})
    audit.replace_queue(last, queue)


# ---- anomaly watch demo ------------------------------------------------------------------
SHARES = {"ECONET": 0.66, "NETONE": 0.26, "TELECEL": 0.08}
PREFIXES = {"ECONET": ("26377", "26378"), "NETONE": ("26371",), "TELECEL": ("26373",)}
HOUR_PROFILE = [0.15, 0.1, 0.08, 0.08, 0.12, 0.3, 0.7, 1.1, 1.4, 1.5, 1.5, 1.45,
                1.4, 1.4, 1.35, 1.3, 1.25, 1.15, 0.95, 0.75, 0.6, 0.45, 0.3, 0.2]
PER_HOUR = 760
BRANCHES = [("Harare Main", 3), ("Bulawayo", 2), ("Gweru", 1), ("Mutare", 1), ("Borrowdale", 1), ("Msasa", 1),
            ("Kwekwe", 0.6), ("Masvingo", 0.6), ("Head Office", 0.4)]
INCIDENT_BRANCHES = [("Harare Main", 3), ("Gweru", 2), ("Kwekwe", 1.5), ("Masvingo", 1.5), ("Mutare", 1), ("Borrowdale", 1),
                     ("Bulawayo", 1), ("Msasa", 1)]
ERRORS = [("Absent subscriber", 4), ("Unknown subscriber", 3), ("Subscriber barred", 1), ("Handset memory full", 1)]


def _hour_counts(start: datetime) -> dict[str, dict[str, int]]:
    """Gateway outcomes for the hour starting at `start` (local), per route."""
    rng = random.Random(start.isoformat())
    total = PER_HOUR * HOUR_PROFILE[start.hour] * rng.uniform(0.9, 1.1)
    out = {}
    for provider, share in SHARES.items():
        n = total * share
        failing = provider == "NETONE" and start + timedelta(hours=1) > INCIDENT_START
        failed, pending = (0.85, 0.08) if failing else (rng.uniform(0.016, 0.026), 0.006)
        if failing and start < INCIDENT_START:  # the hour the incident started in
            part = (start + timedelta(hours=1) - INCIDENT_START).total_seconds() / 3600
            failed, pending = failed * part + 0.02 * (1 - part), pending * part + 0.006 * (1 - part)
        out[provider] = {"failed": round(n * failed), "pending": round(n * pending)}
        out[provider]["delivered"] = round(n) - out[provider]["failed"] - out[provider]["pending"]
    return out


def _demo_tickets() -> list[dict]:
    rng = random.Random(11)
    out = []
    start = (NOW - timedelta(days=17)).astimezone(LOCAL_ZONE).date()
    for i in range(18):
        day = start + timedelta(days=i)
        for _ in range(rng.choice([7, 8, 9, 9, 10, 10, 11])):
            at = datetime.combine(day, datetime.min.time(), tzinfo=LOCAL_ZONE) + timedelta(minutes=rng.randint(450, 1000))
            out.append({"at": at, "branch": pick_from(rng, BRANCHES), "kind": "normal",
                        "topic": "SMS Alerts / Salaries" if rng.random() < 0.25 else "SMS Alerts / Transactional"})
    for _ in range(5):
        at = BURST[0] + (BURST[1] - BURST[0]) * rng.random()
        out.append({"at": at, "branch": "Bulawayo" if rng.random() < 0.75 else pick_from(rng, BRANCHES), "kind": "burst",
                    "topic": "SMS Alerts / Transactional"})
    at = INCIDENT_START + timedelta(minutes=45)
    while at < NOW:
        out.append({"at": at, "branch": pick_from(rng, INCIDENT_BRANCHES), "kind": "incident", "topic": "SMS Alerts / Transactional"})
        at += timedelta(minutes=rng.randint(8, 30))
    for x in out:  # what the agent's check found for each ticket's account
        r = rng.random()
        network = pick_from(rng, [("Econet", 66), ("NetOne", 26), ("Telecel", 8)])
        fact = {"alert_type": "salaries" if "Salaries" in x["topic"] else "debits",
                "account_currency": "USD" if rng.random() < 0.7 else "ZWG", "phone_network": network,
                "email_on_file": "yes" if rng.random() < 0.9 else "no", "cob_registered": "on",
                "trigger_band": rng.choice(["1–3", "4–10", "4–10", "over 10"]), "event": ["DR01"],
                "dispatch_lag": "under 5 min", "branch": x["branch"]}
        if x["kind"] == "incident" and r < 0.8:
            fact.update(status="socket_failed", phone_network="NetOne", providers=["NETONE"], dispatch_lag="under 5 min")
        elif x["kind"] == "burst" and r < 0.5:
            fact.update(status="sms_no_customer_email", email_on_file="no", providers=["none"], dispatch_lag="no send",
                        trigger_band="4–10")
        elif x["kind"] == "burst" and r < 0.8:
            fact.update(status="delivered_check_spam", providers=[network.upper()])
        elif r < 0.45:
            fact.update(status="delivered_check_spam", providers=[network.upper()])
        elif r < 0.55:
            fact.update(status=rng.choice(["socket_not_found", "sms_no_customer_email"]), providers=["none"],
                        dispatch_lag="no send")
            if fact["status"] == "sms_no_customer_email":
                fact["email_on_file"] = "no"
        elif r < 0.9:
            fact.update(status=rng.choice(["different_number", "sms_not_enabled", "no_txn_number_match"]), providers=["none"],
                        trigger_band="none", dispatch_lag="no send")
        else:
            fact = None  # asked the branch for details first
        x["fact"] = fact
    return out


def pick_from(rng: random.Random, pairs):
    total = sum(w for _, w in pairs)
    roll = rng.uniform(0, total)
    for value, weight in pairs:
        roll -= weight
        if roll <= 0:
            return value
    return pairs[-1][0]


def _samples(at: datetime, rng: random.Random) -> dict:
    """Failed and delivered gateway messages from the 24 hours before `at`, as the collector describes them."""
    hours = [(at - timedelta(hours=h)).replace(minute=0, second=0) for h in range(24)]
    pools = {"failed": [], "delivered": []}
    for start in hours:
        for provider, c in _hour_counts(start).items():
            for bucket in pools:
                pools[bucket] += [(start, provider)] * c[bucket]
    out = {"failed": [], "delivered": []}
    for bucket, size in (("failed", 600), ("delivered", 600)):
        for start, provider in rng.sample(pools[bucket], min(size, len(pools[bucket]))):
            incident = provider == "NETONE" and start >= INCIDENT_START - timedelta(minutes=30) and bucket == "failed"
            prefix = rng.choice(PREFIXES[provider])
            phone = prefix + f"{rng.randint(0, 9999999):07d}"
            if bucket == "failed" and not incident and rng.random() < 0.12:
                phone = "263" + phone  # numbers stored with 263 twice fail at the gateway
            currency, amount = ("USD", rng.choice([2.5, 15, 40, 120, 650])) if rng.random() < 0.7 else ("ZWG", rng.choice([30, 250, 1800]))
            kind = "debited" if rng.random() < 0.8 else "credited"
            text = (f"NMB Alert: Account ****{rng.randint(1000000, 9999999)} {kind} {currency} {amount:,.2f} on "
                    f"{start:%d-%m-%Y}. Avail bal {currency} {rng.randint(10, 5000):,}.00")
            error = ""
            if bucket == "failed":
                error = "SMSC bind timeout (NETONE)" if incident else pick_from(rng, ERRORS)
            payload = json.dumps({"to": phone, "message": text, **({"errorDescription": error} if error else {})})
            row = {"time_stamp": (start + timedelta(minutes=rng.randint(0, 59))).isoformat(), "service_provider_name": provider,
                   "external_status": "FAILED_TO_DELIVER" if bucket == "failed" else "DELIVERED_SUCCESSFULLY",
                   "logged_responses": payload}
            out[bucket].append(gateway_features(row))
    return {"available": True, **out}


def _collect(at: datetime, tickets: list[dict], baseline_gateway: dict) -> dict:
    """What the collector would have returned at `at` (local time)."""
    rng = random.Random(at.isoformat())
    # tickets: rolling 24 h windows
    windows = [[] for _ in range(DAYS + 1)]
    for x in tickets:
        if x["at"] < at:
            i = int((at - x["at"]).total_seconds() // 86400)
            if i <= DAYS:
                windows[i].append(x)
    cur, base = windows[0], windows[1:]

    def avg(key):
        totals = {}
        for w in base:
            for x in w:
                totals[x[key]] = totals.get(x[key], 0) + 1
        return {k: round(v / DAYS, 2) for k, v in totals.items()}

    def by(key, items):
        out = {}
        for x in items:
            out[x[key]] = out.get(x[key], 0) + 1
        return out

    today = at.date()
    daily = [{"day": (today - timedelta(days=i)).isoformat(),
              "count": sum(1 for x in tickets if x["at"] < at and x["at"].date() == today - timedelta(days=i))}
             for i in range(DAYS, -1, -1)]
    # gateway
    hourly, current, recent, stuck = [], {}, {}, 0
    hour0 = at.replace(minute=0, second=0, microsecond=0)
    for h in range(47, -1, -1):
        start = hour0 - timedelta(hours=h)
        counts = _hour_counts(start)
        frac = min(1.0, (at - start).total_seconds() / 3600)
        row = {"hour": start.strftime("%Y-%m-%dT%H:00"), "delivered": 0, "failed": 0, "pending": 0, "other": 0}
        for provider, c in counts.items():
            for bucket, n in c.items():
                n = round(n * frac)
                row[bucket] += n
                if start >= hour0 - timedelta(hours=2):
                    recent.setdefault(provider, {"delivered": 0, "failed": 0, "pending": 0})
                    recent[provider][bucket] += n
                if start >= at - timedelta(hours=24):
                    current.setdefault(provider, {"delivered": 0, "failed": 0, "pending": 0})
                    current[provider][bucket] += n
                    if bucket == "pending" and start < at - timedelta(hours=2):
                        stuck += n
        hourly.append(row)
    # the agent's own checks
    checks = {"delivered": [], "not_delivered": [], "other": 0}
    base_checks = {"delivered": 0, "not_delivered": 0}
    for x in tickets:
        f = x["fact"]
        checked = x["at"] + timedelta(minutes=20)
        if not f or checked >= at or checked < at - timedelta(days=DAYS):
            continue
        group = ("delivered" if f["status"] == "delivered_check_spam" else
                 "not_delivered" if f["status"] in {"socket_failed", "socket_not_found", "socket_pending", "socket_unknown",
                                                    "sms_no_customer_email"} else None)
        if checked >= at - timedelta(hours=24):
            if group:
                checks[group].append(f)
            else:
                checks["other"] += 1
        elif group:
            base_checks[group] += 1
    sent = round(PER_HOUR * sum(HOUR_PROFILE[:at.hour + 1]) * 0.98)
    return {
        "generated_at": at.isoformat(timespec="seconds"), "window_hours": 24, "baseline_days": DAYS,
        "tickets": {"available": True, "current": len(cur), "baseline": [len(w) for w in base],
                    "by_topic": by("topic", cur), "by_topic_baseline": avg("topic"),
                    "by_branch": by("branch", cur), "by_branch_baseline": avg("branch"), "daily": daily},
        "gateway": {"available": True, "current": current, "recent": recent,
                    "recent_since": (hour0 - timedelta(hours=2)).strftime("%H:00"), "baseline_per_day": baseline_gateway, "baseline_days": DAYS,
                    "stuck_pending": stuck, "hourly": hourly, "errors": []},
        "gateway_samples": _samples(at, rng),
        "dispatch": {"available": True, "today": {"SENT": sent, "FAILED": round(sent * 0.022)},
                     "baseline_total": {"SENT": 238000, "FAILED": 5300}, "baseline_days": DAYS, "errors": []},
        "cae": {"available": True, "today": {"SUCCESS": round(sent * 1.01), "FAILED": round(sent * 0.008)},
                "baseline_total": {"SUCCESS": 245000, "FAILED": 2100}, "baseline_days": DAYS, "errors": []},
        "ticket_checks": {"available": True, "current": checks, "baseline": base_checks},
    }


def _demo_summary(analysis: dict, started: datetime) -> dict:
    """Stands in for Claude's summary: written from the numbers in the analysis."""
    sig = {s["key"]: s for s in analysis["signals"]}
    day_route = sig.get("gateway_route_NETONE")
    route = sig.get("gateway_recent_NETONE") or day_route
    if route and route["severity"] != "ok":
        f = {(x["feature"], x["value"]): x for x in analysis["comparisons"]["gateway"]["findings"]}
        net = f.get(("network", "NetOne"))
        err = (analysis["comparisons"]["gateway"].get("errors") or [None])[0]
        dbl = f.get(("phone_format", "doubled 263"))
        tv = sig.get("ticket_volume")
        since = started.astimezone(LOCAL_ZONE).strftime("%H:%M")
        window = "since " + route["label"].rsplit(" since ", 1)[-1] if " since " in route["label"] else "over the last 24 hours"
        summary = (f"Delivery through the NETONE route is at {route['value']} {window} against a usual {route['usual']}, "
                   f"starting around {since}"
                   + (f" ({day_route['value']} over the full 24 hours)" if day_route and day_route is not route else "")
                   + "; Econet and Telecel are delivering normally and the overall 24-hour rate is "
                   f"{sig['gateway_delivery']['value'] if 'gateway_delivery' in sig else 'normal'}. "
                   + (f"{net['failed_share']:.0%} of failed sends are NetOne numbers, against {net['ok_share']:.0%} of delivered ones, "
                      if net else "")
                   + (f"and {err['failed_share']:.0%} carry the provider error \"{err['value']}\". " if err else "")
                   + "Dispatch and CAE are at their usual levels, so alerts are being generated and handed to the gateway; "
                     "they fail between the gateway and NetOne. "
                   + (f"SMS Alerts tickets are at {tv['value']} in 24 hours against a usual {tv['usual']:g}." if tv else ""))
        causes = [f"NetOne SMSC connection or route problem: failures are concentrated on NETONE "
                  f"({route['value']} delivered vs {route['usual']} usual)"
                  + (f" and {err['failed_share']:.0%} of failures carry the same error text." if err else ".")]
        if dbl:
            causes.append(f"Separately, numbers stored with 263 twice account for {dbl['failed_share']:.0%} of failures "
                          f"(vs {dbl['ok_share']:.0%} of deliveries); a known data-quality issue, not part of this incident.")
        return {"headline": f"NetOne route failing since {since}: {route['value']} delivered vs {route['usual']} usual",
                "summary": summary, "likely_causes": causes,
                "actions": ["Check the NETONE bind and connection status on the SMS gateway and its logs from " + since + ".",
                            "Raise a fault with NetOne's SMSC support if the bind is up but submissions time out.",
                            "Once fixed, re-send the failed NETONE alerts from the gateway for the affected period.",
                            "Keep escalated SMS Alerts tickets for NetOne numbers together until the route recovers."],
                "branch_message": (f"Since about {since} today some clients on NetOne may not be receiving SMS alerts. "
                                   "ICT is working on it with NetOne; there is no need to raise a ticket for each client."),
                "by": "model"}
    tv = sig.get("ticket_volume")
    if tv and tv["severity"] != "ok":
        tf = analysis["comparisons"]["tickets"]["findings"]
        email = next((x for x in tf if x["feature"] == "email_on_file" and x["value"] == "no"), None)
        branch = sig.get("ticket_volume_branch", {}).get("detail", "")
        return {"headline": f"More SMS Alerts tickets than usual ({tv['value']} vs {tv['usual']:g}); the alert pipeline is healthy",
                "summary": (f"{tv['value']} SMS Alerts tickets in 24 hours against a usual {tv['usual']:g}. Most of the increase: {branch}. "
                            "Gateway delivery, dispatch and CAE are all at their usual levels, so this is not a delivery outage. "
                            + (f"Of the accounts the agent checked whose alerts did not reach the client, "
                               f"{email['failed_share']:.0%} have no valid email in the core profile, against {email['ok_share']:.0%} of "
                               "accounts whose alerts were delivered." if email else "")),
                "likely_causes": ["Clients onboarded or updated recently at one branch without a valid email in the core profile, "
                                  "so CAE drops their alerts before dispatch." if email else
                                  "A local cause at the branch named above rather than a platform problem."],
                "actions": ["Ask the branch named above to capture valid emails for the affected clients.",
                            "Check whether a recent bulk data load or account-opening batch skipped the email field."],
                "branch_message": "", "by": "model"}
    return fallback_summary(analysis)


def seed_anomalies(audit: AuditLog) -> None:
    db = audit.db
    db.executescript(ANOMALY_SCHEMA)
    tickets = _demo_tickets()
    local_now = NOW.astimezone(LOCAL_ZONE)
    # Usual gateway day per route, from normal days.
    base: dict = {}
    for d in range(2, DAYS + 2):
        day = (local_now - timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
        for h in range(24):
            for provider, c in _hour_counts(day + timedelta(hours=h)).items():
                for bucket, n in c.items():
                    base.setdefault(provider, {}).setdefault(bucket, 0)
                    base[provider][bucket] += n
    baseline_gateway = {p: {b: round(v / DAYS, 1) for b, v in c.items()} for p, c in base.items()}

    active, active_key, started, last_alert = False, "", None, None
    for k in range(HISTORY_HOURS - 1, -1, -1):
        at = local_now - timedelta(hours=k, minutes=4)
        data_ = _collect(at, tickets, baseline_gateway)
        analysis = analyse(data_, min_tickets=5, min_sends=200)
        severity = analysis["severity"]
        key = incident_key(analysis)
        if severity != "ok" and started is None:
            started = INCIDENT_START if "NETONE" in key else at
        summary = _demo_summary(analysis, started or at) if severity != "ok" else fallback_summary(analysis)
        alert = None
        if SEVERITY_RANK[severity] >= SEVERITY_RANK["incident"]:  # mirrors AnomalyService._alert
            level_keys = {s["key"] for s in analysis["signals"] if s["severity"] == "incident" and s["value"] is not None}
            seen = set(filter(None, active_key.split("|"))) if active else set()
            if not active:
                alert = "incident"
            elif level_keys - seen:
                alert = "update"
            elif at - last_alert >= timedelta(hours=4):
                alert = "reminder"
            active, active_key = True, "|".join(sorted(seen | level_keys))
            last_alert = at if alert else last_alert
        elif active and severity == "ok":
            alert, active = "resolved", False
        if severity == "ok":
            started = None
        created = at.astimezone(timezone.utc).isoformat(timespec="seconds")
        report = {"severity": severity, "summary": summary, "analysis": analysis}
        db.execute("INSERT INTO anomaly_reports (created_at, severity, incident_key, headline, summary, analysis, data, alert) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (created, severity, key, summary["headline"], json.dumps(summary), json.dumps(analysis),
                    json.dumps(_slim(data_)), alert))
        if alert:
            sms, subject, body = admin_text(report, "https://smsagent.bank.example", resolved=(alert == "resolved"))
            for channel, to in (("sms", "+263771000001"), ("sms", "+263772000002"), ("gateway", "ict.ops@bank.example")):
                db.execute("INSERT INTO notifications (uuid, created_at, kind, channel, recipient, cc, subject, body, status, attempts, sent_at) "
                           "VALUES (?, ?, ?, ?, ?, '', ?, NULL, 'sent', 1, ?)",
                           (f"demo-admin-{k}-{to}", created, f"admin_{alert}", channel, to, subject,
                            (at + timedelta(seconds=40)).astimezone(timezone.utc).isoformat(timespec="seconds")))
    db.execute("INSERT OR REPLACE INTO anomaly_state (id, active, incident_key, started_at, last_alert_at) VALUES (1, ?, ?, ?, ?)",
               (int(active), active_key if active else None,
                INCIDENT_START.isoformat(timespec="seconds") if active else None,
                last_alert.astimezone(timezone.utc).isoformat(timespec="seconds") if last_alert else None))
    db.commit()


# ---- approvals and the live view -------------------------------------------------------------
def _approval_transcript(ticket: str, topic: str, message: str, steps: list, decision: dict) -> list:
    rendered = (f"Today: {NOW.date()}\nTicket #{ticket}   Help topic: {topic}\nSubject: SMS alerts\n\nForm fields:\n"
                "  Phone to receive alerts: <MOBILE_1>\n  Account number: <NUMBER_1>\n  Currency: USD\n"
                f"  Salaries or Debits: Debits\n\nThread, oldest first:\n--- Ticket owner\n{message}")
    out = [{"role": "user", "content": rendered + "\n\nInvestigate with the checks, then call submit_decision."}]
    for i, (thought, name, args, result) in enumerate(steps, 1):
        blocks = ([{"type": "text", "text": thought}] if thought else []) + [{"type": "tool_use", "id": f"s{i}", "name": name, "input": args}]
        out += [{"role": "assistant", "content": blocks},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"s{i}", "content": json.dumps(result), "is_error": False}]}]
    out += [{"role": "assistant", "content": [{"type": "tool_use", "id": "d", "name": "submit_decision", "input": decision}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "d", "content": "Decision recorded.", "is_error": False}]}]
    return out


APPROVAL_CASES = [
    dict(ticket="600512", topic="SMS Alerts / Transactional", hours=0.6, status="different_number", action="reply_and_close",
         message="Client updated her number at the branch last week but alerts still go to the old line.",
         steps=[("The check says the ticket number differs from Intellect. Comparing the records.", "check_sms_alert_account",
                 {"account_number": "<NUMBER_1>", "alert_types": "debits"},
                 {"status": "different_number", "standby_numbers": ["<MOBILE_2>"], "reply": "Kindly update the client's mobile number on Intellect."}),
                ("The customer record may already be updated; the alerts follow the IDCCOB registration.", "get_customer_profile",
                 {"account_number": "<NUMBER_1>"},
                 {"found": True, "sms_flag_on": True, "customer_record_mobile": "<MOBILE_1>",
                  "alert_registrations": [{"mobile": "<MOBILE_2>", "sms_alert": "1"}], "usable_email_on_file": True}),
                ("", "get_gateway_messages", {"mobile": "<MOBILE_2>", "date_from": "2026-10-01", "date_to": "2026-10-08"},
                 {"messages": [{"at": "07-10-2026 14:02", "network": "ECONET", "status": "delivered", "currency": "USD", "amount": "40.00", "kind": "debit"}],
                  "by_status": {"delivered": 6}})],
         reply=("The client's new number <MOBILE_1> is on the customer record, but the SMS-alert registration on IDCCOB still "
                "has the old number <MOBILE_2>, so alerts are delivered to the old line. Kindly update the number on the "
                "IDCCOB SMS-alert registration to <MOBILE_1>; alerts will then go to the new number."),
         standard=False, conf=0.92),
    dict(ticket="600507", topic="Account Services", hours=1.4, status="no_txn_number_match", action="reply_and_close",
         message="Client withdrew cash at an ATM on 18 September and got no SMS. Acc <NUMBER_1>, cell <MOBILE_1>.",
         steps=[("Filed under Account Services, but it is about a missing alert. The withdrawal is outside the 15-day window.",
                 "check_sms_alert_account", {"account_number": "<NUMBER_1>", "alert_types": "debits", "ticket_phone": "<MOBILE_1>"},
                 {"status": "no_txn_number_match", "reply": "No alertable activity in the window."}),
                ("", "get_account_activity", {"account_number": "<NUMBER_1>", "date_from": "2026-09-17", "date_to": "2026-09-19"},
                 {"summary": "2 transaction(s), 1 raised an alert, 1 did not",
                  "transactions": [{"date": "2026-09-18", "currency": "USD", "amount": "100.00", "alert_event": None},
                                   {"date": "2026-09-17", "currency": "USD", "amount": "12.50", "alert_event": "DEBIT ALERT"}]})],
         reply=("The ATM withdrawal of USD 100.00 on 18-09-2026 did not raise an alert in core banking, while other debits on "
                "the account did, so the number <MOBILE_1> and the alert set-up are correct. ATM withdrawals at that terminal "
                "are not configured to send alerts. Kindly advise the client accordingly."),
         standard=False, conf=0.88),
    dict(ticket="600499", topic="SMS Alerts / Transactional", hours=3.1, status="delivered_check_spam", action="request_info",
         message="Client says no SMS at all for two weeks, she has checked her phone.",
         steps=[("", "check_sms_alert_account", {"account_number": "<NUMBER_1>", "alert_types": "debits"},
                 {"status": "delivered_check_spam", "reply": "Kindly advise the client to check the SMS spam folder."}),
                ("The gateway says delivered, yet the owner says she checked. Looking at the messages.", "get_gateway_messages",
                 {"mobile": "<MOBILE_1>", "date_from": "2026-09-24", "date_to": "2026-10-08"},
                 {"by_status": {"delivered": 11}, "messages": [{"at": "06-10-2026 09:41", "network": "NETONE", "status": "delivered"}]})],
         reply=("The SMS gateway confirms that 11 alerts were delivered to <MOBILE_1> on the NetOne network in the last two "
                "weeks. Kindly ask the client to confirm that this number is the SIM in her phone, and share a screenshot of "
                "her blocked messages, so we can see where they went."),
         standard=False, conf=0.86),
]
DECIDED_CASES = [("600488", "reply_and_close", "posted", "dobe", "Wording tightened.", 1),
                 ("600481", "reply_and_close", "escalated", "tsitsi.m", "The client asked for an e-mail statement too; handle with Cards.", 0),
                 ("600477", "request_info", "dropped", None, None, 0)]


def seed_approvals(audit: AuditLog) -> None:
    from agent.approval import ApprovalStore

    store = ApprovalStore(audit.db)
    run = audit.db.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()["run_id"]
    for c in APPROVAL_CASES:
        when = (NOW - timedelta(hours=c["hours"])).isoformat(timespec="seconds")
        why = ("the agent reasoned beyond the standard check results" if c["action"] == "reply_and_close"
               else "the agent reasoned beyond the standard check results; the check results alone lead to reply and close, not request info")
        decision = {"action": c["action"], "confidence": c["conf"], "category": c["status"], "follows_standard_outcome": c["standard"],
                    "reason": "Lookups went beyond the standard check; see the steps.", "evidence": [f"status {c['status']}"],
                    "reply_text": c["reply"]}
        did = audit.record(run_id=run, created_at=when, mode="live", ticket_id=int(c["ticket"]) - 500000, ticket_number=c["ticket"],
                           last_entry_id=int(c["ticket"]) * 3, last_message_id=int(c["ticket"]) * 3, proposed_action=c["action"],
                           final_action=c["action"], confidence=c["conf"], category=c["status"],
                           reason=decision["reason"], evidence=decision["evidence"], account_statuses={"<NUMBER_1>": c["status"]},
                           reply_text=c["reply"], guardrail_notes=[], executed=["awaiting approval"],
                           tools_used=[s[1] for s in c["steps"]], approval=f"waiting: {why}",
                           transcript=_approval_transcript(c["ticket"], c["topic"], c["message"], c["steps"], decision),
                           input_tokens=R.randint(7000, 12000), output_tokens=R.randint(500, 900))
        aid = store.create(decision_id=did, ticket_id=int(c["ticket"]) - 500000, ticket_number=c["ticket"],
                           last_entry_id=int(c["ticket"]) * 3, action=c["action"], why=why, reply_masked=c["reply"],
                           reply_text="(demo)", token_map={})
        audit.db.execute("UPDATE approvals SET created_at = ? WHERE id = ?", (when, aid))
    for i, (num, action, status, who, comment, edited) in enumerate(DECIDED_CASES):
        when = (NOW - timedelta(hours=20 + 9 * i)).isoformat(timespec="seconds")
        did = audit.record(run_id=run, created_at=when, mode="live", ticket_id=int(num) - 500000, ticket_number=num,
                           last_entry_id=int(num) * 3, proposed_action=action, final_action=action, confidence=0.9,
                           category="different_number", reason="Lookups went beyond the standard check.",
                           reply_text="Kindly update the IDCCOB SMS-alert registration to <MOBILE_1>.",
                           executed=["awaiting approval"], approval="waiting: the agent reasoned beyond the standard check results")
        aid = store.create(decision_id=did, ticket_id=int(num) - 500000, ticket_number=num, last_entry_id=int(num) * 3,
                           action=action, why="the agent reasoned beyond the standard check results",
                           reply_masked="Kindly update the IDCCOB SMS-alert registration to <MOBILE_1>.", reply_text="(demo)", token_map={})
        if who:
            store.decide(aid, "approve" if status == "posted" else "reject", who, comment)
        store.finish(aid, status, {"posted": "reply, close, email queued", "escalated": "escalation note posted",
                                   "dropped": "the ticket changed before the reply was posted"}[status])
        audit.db.execute("UPDATE approvals SET created_at = ?, decided_at = ?, finished_at = ? WHERE id = ?",
                         (when, when, when, aid))
    audit.db.commit()
    audit.live_start("a41c0e93d2f7")
    audit.live_ticket("600515", "SMS Alerts / Salaries")
    for step in ("check_sms_alert_account(<NUMBER_1>, salaries)", "get_customer_profile(<NUMBER_1>)"):
        audit.live_step(step)


def build(out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "demo.sqlite"
        audit = AuditLog(db_path)
        seed(audit)
        seed_anomalies(audit)
        seed_approvals(audit)
        conn = audit.db
        settings = replace(Settings.from_env(Path(tmp) / "none.env"), state_db=db_path, mode="live",
                           reply_staff_id=525, model="claude-sonnet-5-5")
        summaries = [data.summary(r) for r in conn.execute(
            f"SELECT {data.SUMMARY_COLUMNS} FROM decisions d ORDER BY d.id DESC").fetchall()]
        details = {}
        for s in summaries:
            detail = data.decision_detail(conn, s["id"])
            if s["mode"] == "audit":
                detail["transcript"] = []
            details[str(s["id"])] = detail
        snapshot = {
            "session": {"user": "dobe", "role": "admin", "demo": True, "configured_mode": "live", "effective_mode": "live",
                        "kill_switch": False, "model": settings.model, "reply_staff_id": 525,
                        "osticket_ticket_url": None, "stale_after_minutes": settings.portal_stale_minutes,
                        "notify_transport": "gateway", "anomaly": data.latest_anomaly(conn),
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
            "anomaly_reports": {str(r["id"]): data.anomalies(conn, r["id"])["report"]
                                for r in conn.execute("SELECT id FROM anomaly_reports").fetchall()},
            "decisions": summaries,
            "details": details,
        }
        audit.close()
    payload = json.dumps(snapshot, default=str, separators=(",", ":")).replace("</", "<\\/")
    page = STATIC.read_text(encoding="utf-8").replace(
        SERVED_MARKER, f'<script id="demo-data" type="application/json">{payload}</script>', 1)
    out.write_text(page, encoding="utf-8")
    print(f"Wrote {out} ({len(page) // 1024} KB, {len(summaries)} demo decisions)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "preview.html"))
    build(Path(parser.parse_args().out))
