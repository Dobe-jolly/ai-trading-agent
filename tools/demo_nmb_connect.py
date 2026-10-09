"""Invented demo data for the NMB Connect desk's part of the console preview (tools/build_preview.py).

The snapshot is produced by seeding a throwaway SQLite log and running the console's real read models over it,
so the preview shows what the server returns. No bank data is involved: every ticket and number here is invented.

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
from nmb_connect.config import Settings  # noqa: E402
from nmb_connect.portal import data  # noqa: E402
from nmb_connect.tools import STATUS_TEXT  # noqa: E402

R = random.Random(20261011)
NOW = datetime.now(timezone.utc).replace(microsecond=0)
DAYS = 14
HANDSET = ("Kindly ask the client to check the blocked or spam SMS folder, unblock the bank's sender, restart the phone "
           "and request a new OTP. If it still does not arrive, reply on this ticket and we will check the SMS gateway.")
# status -> (weight, problem, topic, action, subject, message, reply)
CASES = {
    "registered": (14, "otp_not_received", "NMB Connect / OTP", "reply_and_close", "OTP not received",
                   "Client CIF {cif}, cell <MOBILE_1>, is not receiving the OTP on the app.",
                   "The client is registered on NMB Connect with <MOBILE_1>, the number on the ticket, so the OTP goes "
                   "to the right number: the last OTP was delivered on 08-10-2026 09:41. " + HANDSET),
    "not_registered": (8, "forgot_username", "NMB Connect / USSD / Forgot Username", "reply_and_close", "Forgot username",
                       "Client CIF {cif}, cell <MOBILE_1>, forgot the username on USSD.",
                       "The client is not registered on NMB Connect: there is no profile for CIF {cif}, so Forgot "
                       "Username has nothing to match. Kindly register the client (the mobile can be captured as 07…, 7… "
                       "or 263…), then ask them to log in."),
    "mobile_mismatch": (10, "number_changed", "NMB Connect / Account", "request_info", "Mobile number does not match",
                        "Client CIF {cif} changed number to <MOBILE_1>; the app says 'your mobile number does not match'.",
                        "The OTP and NMB Connect messages go to the number on the client's profile, <MOBILE_2>, not "
                        "<MOBILE_1>. If the client has changed number, kindly make sure the new number is captured and "
                        "authorised on the CIF in IDCCOB (core banking already shows <MOBILE_1>), then email ICT Data "
                        "Science to update the number on the NMB Connect profile, and reply here once it is done."),
    "already_registered": (12, "cannot_register", "NMB Connect / Registration", "request_info", "Already registered",
                           "Client CIF {cif}, cell <MOBILE_1>: 'Already registered, kindly log in' but cannot log in.",
                           "The client already has an NMB Connect profile (created 14/03/2025, mobile <MOBILE_1>), which "
                           "is why registration says the client is already registered. Kindly delete the profile on the "
                           "dashboard and have the client register again. If the delete fails, reply here with a "
                           "screenshot of the error."),
    "reregistered": (6, "cannot_register", "NMB Connect / Registration", "reply_and_close", "Already registered",
                     "Done, the profile was deleted and the client registered again.",
                     "The client's NMB Connect profile was created again on 08/10/2026 with mobile <MOBILE_1>. The client "
                     "can now log in; if anything still fails, reply on this ticket with a screenshot of the error."),
    "account_active": (7, "accounts_not_visible", "NMB Connect / Account", "request_info", "ZiG account not showing",
                       "Client CIF {cif}: the ZiG account <NUMBER_2> does not show on the app.",
                       "Account <NUMBER_2> is active under the client's CIF {cif}. NMB Connect picks up the client's "
                       "accounts when the profile is created, so kindly delete the client's NMB Connect profile on the "
                       "dashboard and have them register again; it will include all their accounts."),
    "account_inactive": (5, "account_inactive", "NMB Connect / System Issue", "reply_and_close", "Request not completed",
                         "Client account <NUMBER_1>: every transfer on the app fails with 'request not completed'.",
                         "Account <NUMBER_1> is dormant in core banking, which is why it fails on NMB Connect. The client "
                         "should visit the branch to have it reactivated."),
    "registration_failed_temporary": (3, "cannot_register", "NMB Connect / USSD / Registration", "reply_and_close",
                                      "Registration error", "Client CIF {cif} gets an error when registering on USSD.",
                                      "The client's NMB Connect registration on 07/10/2026 failed because of a temporary "
                                      "system error. Kindly ask the client to try again; if it fails again, reply on this "
                                      "ticket with a screenshot of the error."),
    "registration_failed_email": (2, "cannot_register", "NMB Connect / Registration", "reply_and_close",
                                  "Registration error", "Client CIF {cif}: registration says the e-mail or password is wrong.",
                                  "The client's NMB Connect registration on 07/10/2026 failed because the e-mail or "
                                  "password entered did not match the bank's records; the e-mail on the client's bank "
                                  "profile is <EMAIL_1>. Kindly ask the client to register again using that e-mail."),
    "duplicate_profiles": (3, "cannot_login", "NMB Connect / System Issue", "escalate", "Cannot log in",
                           "Client CIF {cif} cannot log in, 'wrong password or username'.", ""),
    "number_on_other_profile": (2, "cannot_register", "NMB Connect / Registration", "escalate", "Number registered",
                                "Client CIF {cif}: the mobile <MOBILE_1> is already registered on another account.", ""),
    "otp_not_delivered": (2, "otp_not_received", "NMB Connect / USSD / OTP", "escalate", "No OTP",
                          "Client CIF {cif}, cell <MOBILE_1>, gets no OTP on USSD.", ""),
    "registered_cannot_login": (4, "cannot_login", "NMB Connect / Account", "escalate", "Cannot log in",
                                "Client CIF {cif}, cell <MOBILE_1>: 'wrong password or username' after changing phones.", ""),
    "needs_details": (5, "cannot_login", "NMB Connect / System Issue", "request_info", "Assist",
                      "Kindly assist the client, cannot log in.",
                      "To check the client's NMB Connect profile we need the client's CIF or account number and the "
                      "mobile number they use. Kindly reply with it, and with a screenshot of the error if there is one."),
    "other": (6, "other", "NMB Connect / System Issue", "escalate", "VISA withdrawal",
              "The client gets 'wrong destination account' on a VISA withdrawal reference.", ""),
}
NOTES = {"duplicate_profiles": "ICT Data Science: the client has 2 active NMB Connect profiles for CIF {cif}.",
         "number_on_other_profile": "The mobile is on an NMB Connect profile under another CIF.",
         "otp_not_delivered": "OTPs to the number are failing at the SMS gateway ({'failed': 3}).",
         "registered_cannot_login": "Registered with the right number; profile not marked locked.",
         "other": "Not a registration, login, OTP or account-visibility problem."}


def transcript(number: str, cif: str, status: str, c: tuple, standard: bool = True, reply: str = "") -> list:
    _, problem, topic, action, subject, message, std = c
    reply = reply or std.format(cif=cif)
    rendered = (f"Today: {NOW.date()}\nTicket #{number}   Help topic: {topic}   Raised by: Harare Main\n"
                f"Subject: {subject}\n\nForm fields (as captured on the ticket):\n  (none)\n\n"
                f"Details the ticket owner gave (form, messages and their attachments):\n  CIFs: {cif}\n"
                f"  Account numbers: (none)\n  Mobile numbers: <MOBILE_1>\n  National IDs: (none)\n\n"
                f"Thread, oldest first:\n--- Ticket owner\n{message.format(cif=cif)}")
    profile = {"profiles": [{"profile": "P1", "cif": cif, "mobile_on_profile": "<MOBILE_2>" if status == "mobile_mismatch"
                             else "<MOBILE_1>", "mobile_matches_ticket": "no" if status == "mobile_mismatch" else "yes",
                             "belongs_to_ticket_cif": "yes", "created": "14-03-2025 10:22", "active": True,
                             "locked_or_blocked": False, "found_by": ["cif", "mobile"],
                             "state": {"status": "ACTIVE", "login_attempts": 0}}] if status not in (
        "not_registered", "registration_failed_temporary", "registration_failed_email", "needs_details") else [],
        "active_profiles_for_cif": {cif: 0 if status == "not_registered" else 1}}
    assessed = {"problem": problem, "where_the_client_stands": status, "meaning": STATUS_TEXT.get(status, status),
                "standard_outcome": {"action": action, "reply": std.format(cif=cif) or None}}
    steps = [{"role": "user", "content": rendered + "\n\nLook the client up, name the problem with assess_access_issue, "
                                                    "then call submit_decision."},
             {"role": "assistant", "content": [{"type": "text", "text": "Looking up the client's NMB Connect profile."},
                                               {"type": "tool_use", "id": "p", "name": "get_connect_profile",
                                                "input": {"cif": cif, "mobile": "<MOBILE_1>"}}]},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "p", "is_error": False,
                                           "content": json.dumps(profile)}]},
             {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "assess_access_issue",
                                                "input": {"problem": problem}}]},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "is_error": False,
                                           "content": json.dumps(assessed)}]}]
    decision = {"action": action, "confidence": 0.92, "category": status,
                "reason": f"Looked the client up; {STATUS_TEXT.get(status, status)}.", "evidence": [f"assessment: {status}"],
                "reply_text": reply if action != "escalate" else "", "follows_standard_outcome": standard}
    steps += [{"role": "assistant", "content": [{"type": "tool_use", "id": "d", "name": "submit_decision", "input": decision}]},
              {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "d", "content": "Decision recorded.",
                                            "is_error": False}]}]
    return steps


def seed(audit: AuditLog) -> None:
    db = audit.db
    go_live = NOW - timedelta(days=3, hours=1)
    t = NOW - timedelta(days=DAYS)
    runs = []
    while t < NOW - timedelta(minutes=8):
        mode = "live" if t >= go_live else "shadow"
        rid = f"{R.getrandbits(48):012x}"
        runs.append((rid, t, mode))
        db.execute("INSERT INTO runs (run_id, started_at, finished_at, mode, model, tickets_seen, decisions, errors) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (rid, t.isoformat(), (t + timedelta(seconds=R.randint(20, 90))).isoformat(),
                                                      mode, "claude-sonnet-5-5", R.randint(1, 7), 0, 0))
        t += timedelta(minutes=15)
    branches = ["Harare Main", "Bulawayo", "Gweru", "Mutare", "Borrowdale", "Msasa", "Kwekwe"]
    number = 720300
    ids = []
    statuses = list(CASES)
    for day_back in range(DAYS - 1, -1, -1):
        for when in sorted((NOW - timedelta(days=day_back, hours=R.uniform(0.3, 10))).replace(second=0)
                           for _ in range(R.randint(3, 7))):
            run = min(runs, key=lambda r: abs((r[1] - when).total_seconds()))
            number += R.randint(1, 4)
            status = R.choices(statuses, weights=[CASES[s][0] for s in statuses])[0]
            c = CASES[status]
            _, problem, topic, action, _, _, std = c
            cif = str(8600000 + R.randint(100, 9999))
            reply = std.format(cif=cif)
            proposed, notes = action, []
            if status in ("mobile_mismatch", "already_registered", "account_active") and R.random() < 0.3:
                proposed = "reply_and_close"
                notes = [f"kept open: {status} needs the branch to act; the agent checks again when they reply"]
            if status == "duplicate_profiles" and R.random() < 0.4:
                proposed = "reply_and_close"
                notes = [f"close blocked: the assessment is {status}, which needs a person"]
            live = run[2] == "live"
            executed = ({"reply_and_close": ["reply", "close", "email queued"],
                         "request_info": ["reply", "answered", "email queued"], "escalate": ["note"]}[action] if live else [])
            branch = R.choice(branches)
            tools = ["get_connect_profile"] + (["check_core_banking"] if status in ("mobile_mismatch", "account_active",
                                                                                 "account_inactive") else []) \
                + (["get_sms_to_number"] if problem == "otp_not_received" else []) + ["assess_access_issue"]
            ids.append(audit.record(
                run_id=run[0], created_at=when.isoformat(timespec="seconds"), mode=run[2], ticket_id=number - 600000,
                ticket_number=str(number), last_entry_id=number * 3, last_message_id=number * 3, proposed_action=proposed,
                final_action=action, confidence=round(R.uniform(0.86, 0.98), 2), category=status,
                reason=f"Looked the client up; {STATUS_TEXT.get(status, status)}.", evidence=[f"assessment: {status}"],
                account_statuses={"connect": status}, reply_text=reply or None, template_reply=reply or None,
                guardrail_notes=notes, tools_used=tools, executed=executed, deferred=0, branch=branch,
                facts={"assessment": {"problem": problem, "status": status, "action": action, "profiles": 1,
                                      "branch": branch}},
                transcript=transcript(str(number), cif, status, c), input_tokens=R.randint(4500, 9000),
                output_tokens=R.randint(300, 700)))
            if live and action != "escalate":
                db.execute("INSERT INTO notifications (uuid, created_at, decision_id, ticket_id, ticket_number, kind, channel, "
                           "recipient, cc, subject, status, attempts, sent_at) VALUES (?, ?, ?, ?, ?, ?, 'gateway', ?, '', ?, ?, ?, ?)",
                           (f"demo-nc-{number}", when.isoformat(), ids[-1], number - 600000, str(number), action,
                            f"{R.choice(['t.moyo', 'r.ncube', 'p.dube'])}@{branch.lower().replace(' ', '')}.branch.example",
                            f"[#{number}] NMB Connect ticket " + ("resolved" if action == "reply_and_close"
                                                                  else "action or information needed"),
                            "sent", 1, (when + timedelta(minutes=1)).isoformat()))
    for did in ids[: int(len(ids) * 0.6)]:
        if R.random() < 0.4:
            row = db.execute("SELECT created_at FROM decisions WHERE id = ?", (did,)).fetchone()
            wrong = R.random() < 0.08
            db.execute("INSERT INTO reviews (decision_id, created_at, reviewer, verdict, expected_action, comment) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (did, (datetime.fromisoformat(row["created_at"]) + timedelta(hours=2)).isoformat(timespec="seconds"),
                        R.choice(["dobe", "tsitsi.m"]), "incorrect" if wrong else "correct", "escalate" if wrong else None,
                        "Corporate profile: the branch cannot delete it." if wrong else None))
    for at, actor, action, detail in [(NOW - timedelta(days=13, hours=3), "dobe", "hold at shadow", "new desk: first week in shadow"),
                                      (go_live, "dobe", "release mode hold", "shadow assessments checked against the dashboard")]:
        db.execute("INSERT INTO control_events (created_at, actor, action, detail) VALUES (?, ?, ?, ?)",
                   (at.isoformat(timespec="seconds"), actor, action, detail))
    db.commit()
    last = runs[-1][0]
    rows = db.execute("SELECT id, ticket_id, ticket_number, created_at, final_action FROM decisions WHERE mode = 'live' "
                      "AND final_action IN ('escalate', 'request_info') ORDER BY id DESC LIMIT 6").fetchall()
    audit.replace_queue(last, [{"ticket_id": r["ticket_id"], "ticket_number": r["ticket_number"],
                                "help_topic": "NMB Connect / Registration", "opened": r["created_at"], "state": "skipped",
                                "detail": "already escalated" if r["final_action"] == "escalate" else "waiting on ticket owner",
                                "decision_id": r["id"]} for r in rows])


APPROVAL_CASES = [
    dict(ticket="720912", hours=1.1, status="registered_cannot_login", action="request_info",
         reply=("The client is registered on NMB Connect with <MOBILE_1> and the profile is not locked, but the app "
                "has rejected every login since the client changed phones on 05/10/2026. Kindly delete the profile on "
                "the dashboard and have the client register again on the new phone, then reply here."),
         why="the agent reasoned beyond the standard outcome of the assessment; the assessment alone leads to escalate, "
             "not request info"),
    dict(ticket="720905", hours=3.4, status="mobile_mismatch", action="reply_and_close",
         reply=("The OTP goes to the number on the client's NMB Connect profile, <MOBILE_2>, which is still the client's "
                "number according to the thread. Kindly ask the client to use that phone for the OTP."),
         why="the agent reasoned beyond the standard outcome of the assessment; the assessment alone leads to request "
             "info, not reply and close"),
]


def seed_approvals(audit: AuditLog) -> None:
    from nmb_connect.approval import ApprovalStore

    store = ApprovalStore(audit.db)
    run = audit.db.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()["run_id"]
    for c in APPROVAL_CASES:
        when = (NOW - timedelta(hours=c["hours"])).isoformat(timespec="seconds")
        tid = int(c["ticket"]) - 600000
        case = CASES[c["status"]]
        did = audit.record(run_id=run, created_at=when, mode="live", ticket_id=tid, ticket_number=c["ticket"],
                           last_entry_id=int(c["ticket"]) * 3, last_message_id=int(c["ticket"]) * 3,
                           proposed_action=c["action"], final_action=c["action"], confidence=0.9, category=c["status"],
                           reason="Looked the client up; the reply goes beyond the standard outcome.",
                           evidence=[f"assessment: {c['status']}"], account_statuses={"connect": c["status"]},
                           reply_text=c["reply"], guardrail_notes=[], executed=["awaiting approval"],
                           tools_used=["get_connect_profile", "get_app_activity", "assess_access_issue"],
                           approval=f"waiting: {c['why']}", branch="Bulawayo",
                           transcript=transcript(c["ticket"], "8601234", c["status"],
                                                 (case[0], case[1], case[2], c["action"], case[4], case[5], case[6]),
                                                 standard=False, reply=c["reply"]),
                           input_tokens=R.randint(7000, 11000), output_tokens=R.randint(500, 900))
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
                           reply_staff_id=533, model="claude-sonnet-5-5")
        summaries = [data.summary(r) for r in conn.execute(
            f"SELECT {data.SUMMARY_COLUMNS} FROM decisions d ORDER BY d.id DESC").fetchall()]
        details = {str(s["id"]): data.decision_detail(conn, s["id"]) for s in summaries}
        snap = {
            "session": {"user": "dobe", "role": "admin", "demo": True, "configured_mode": "live", "effective_mode": "live",
                        "kill_switch": False, "model": settings.model, "reply_staff_id": 533,
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
