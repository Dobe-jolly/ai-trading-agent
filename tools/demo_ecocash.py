"""Invented demo data for the ecocash desk's part of the console preview (tools/build_preview.py).

The snapshot is produced by seeding a throwaway SQLite log and running the portal's real read
models (and the real anomaly analysis) over it, so the preview shows what the server returns.
No bank data is involved: every number and name here is invented.

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

from ecocash.anomaly.detect import SEVERITY_RANK, analyse, incident_key  # noqa: E402
from ecocash.anomaly.features import attempt_features  # noqa: E402
from ecocash.anomaly.service import SCHEMA as ANOMALY_SCHEMA, _slim  # noqa: E402
from ecocash.anomaly.summarize import admin_text, fallback_summary  # noqa: E402
from agentcore.audit import AuditLog  # noqa: E402
from ecocash.config import Settings  # noqa: E402
from ecocash.normalize import LOCAL_ZONE  # noqa: E402
from ecocash.portal import data  # noqa: E402

R = random.Random(20261008)
NOW = datetime.now(timezone.utc).replace(microsecond=0)
DAYS = 14
OUTAGE_START = NOW - timedelta(hours=2, minutes=50)  # the demo EcoCash incident
# The demo traffic follows a working-day curve that peaks around the build time, so the incident shows
# whatever hour the preview is built at.
SHIFT = (NOW.astimezone(LOCAL_ZONE).hour - 14) % 24

# status -> (weight, final action, reply)
CASES = {
    "already_linked": (30, "request_info", "EcoCash rejected the link because account <NUMBER_1> is already linked to an "
                       "EcoCash wallet (EcoCash error 20503). Kindly delink the account on the Universal dashboard, then ask "
                       "the client to link it again. If the delink fails, reply on this ticket with a screenshot of the error "
                       "message and ICT will delink it."),
    "linked": (14, "reply_and_close", "Account <NUMBER_1> was linked to EcoCash on <MOBILE_1> successfully this morning. "
               "The client can now use the link."),
    "kyc_mismatch": (12, "reply_and_close", "EcoCash rejected the link because the client's details do not match EcoCash's "
                     "KYC records (EcoCash error BCS50646). The national ID the bank sent matches the client's ID on this "
                     "ticket, so the difference is on EcoCash's side. Kindly advise the client to visit an EcoCash shop to "
                     "update their EcoCash details, then link the account again."),
    "number_not_ecocash": (8, "reply_and_close", "EcoCash rejected the link because <MOBILE_1> is not registered on EcoCash "
                           "(EcoCash error TP40288). Kindly advise the client to register for EcoCash on that number, or to "
                           "link using the number their EcoCash wallet is on."),
    "wallet_closed": (8, "reply_and_close", "EcoCash rejected the link because the client's EcoCash wallet on <MOBILE_1> is "
                      "closed (EcoCash error TP40050). Kindly advise the client to visit an EcoCash shop to reactivate the "
                      "wallet, then link the account again."),
    "ecocash_system_error": (6, "reply_and_close", "EcoCash returned a temporary system error when the client tried to link "
                             "the account (EcoCash error TP40024). Kindly ask the client to try again in a few hours."),
    "account_rejected": (3, "escalate", ""),
    "no_response": (2, "escalate", ""),
    "none": (6, "request_info", "We could not find an EcoCash link attempt for <MOBILE_1> in the last 60 days. Kindly "
             "confirm the client's EcoCash mobile number and account number, and ask the client to try linking again."),
}
REASONS = {
    "reply_and_close": "Looked up the client's link attempts by mobile and account: the latest failed with {status}. The "
                       "standard reply fits and the thread does not contradict it.",
    "request_info": "Latest attempt: {status}. The branch has to act and report back, so the ticket stays open.",
    "escalate": "Latest attempt: {status}. This needs ICT; the attempts are listed in the note.",
}
FILES = [
    [{"name": "IMG_4410.jpg", "type": "image/jpeg", "size": 211_000, "status": "read",
      "kind": "Screenshot of an error on the client's phone (USSD or EcoCash)",
      "summary": "USSD menu after choosing 'Link bank account': the error says the account is already linked.\n"
                 "Error messages: Request failed. Bank account already exists in the identity."}],
    [{"name": "ID front.jpg", "type": "image/jpeg", "size": 388_000, "status": "read", "kind": "Identity document",
      "summary": "Front of a Zimbabwe national ID card.\nNational IDs shown: <NATIONAL_ID_1>"}],
    [{"name": "dashboard error.png", "type": "image/png", "size": 145_000, "status": "read",
      "kind": "Screenshot of a bank system screen (e.g. the Universal dashboard)",
      "summary": "Universal dashboard, EcoCash de-registration screen for account <NUMBER_1>: 'Delink failed: AccountNumber "
                 "parameter is incorrect'.\nAccount numbers shown: <NUMBER_1>"},
     {"name": "image001.png", "type": "image/png", "size": 4_100, "status": "skipped",
      "note": "small image in the message body (an e-mail signature logo)", "kind": "", "summary": ""}],
]


def pick(rng, weights):
    total = sum(weights.values())
    roll = rng.uniform(0, total)
    for key, w in weights.items():
        roll -= w
        if roll <= 0:
            return key
    return key


def transcript(number: str, status: str, action: str, reply: str, reason: str, files: list) -> list:
    rendered = (f"Today: {NOW.date()}\nTicket #{number}   Help topic: EcoCash   Raised by: Harare Main\n"
                "Subject: Client cannot link account to EcoCash\n\nForm fields (as captured on the ticket):\n"
                "  Mobile number: <MOBILE_1>\n  Account number: <NUMBER_1>\n\n"
                "Details the ticket owner gave (form, messages and their attachments):\n  Mobile numbers: <MOBILE_1>\n"
                "  Account numbers: <NUMBER_1>\n  National IDs: " + ("<NATIONAL_ID_1>" if files and "ID" in files[0]["name"] else "(none)")
                + "\n\nThread, oldest first:\n--- Ticket owner\nClient is failing to link the account to EcoCash on *151#.")
    for f in files:
        rendered += f"\n[Attachment: {f['name']}] " + (f"{f['kind']}:\n    " + f["summary"].replace("\n", "\n    ")
                                                         if f["status"] == "read" else f"Not read: {f['note']}")
    attempt = {"attempt_id": "a1042", "when": "07-10-2026 09:14", "request": "link", "outcome": "linked" if status == "linked" else "failed",
               "requested_by": "client on USSD", "mobile_used": "<MOBILE_1>", "account_used": "<NUMBER_1>",
               "national_id_sent": "<NATIONAL_ID_1>", "currency": "USD", "name_sent_matches_ticket": True}
    result = {"found": 0 if status == "none" else 1, "attempts": [] if status == "none" else [attempt],
              "summary": {"status": status, "suggested_action": action, "standard_reply": reply}}
    decision = {"action": action, "confidence": 0.93, "category": status, "reason": reason, "evidence": [f"latest attempt {status}"],
                "reply_text": reply}
    return [
        {"role": "user", "content": rendered + "\n\nInvestigate with the lookups, then call submit_decision."},
        {"role": "assistant", "content": [{"type": "text", "text": "Looking up the client's link attempts."},
                                          {"type": "tool_use", "id": "t1", "name": "find_ecocash_attempts",
                                           "input": {"mobile": "<MOBILE_1>", "account_number": "<NUMBER_1>"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": json.dumps(result), "is_error": False}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "submit_decision", "input": decision}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "Decision recorded.", "is_error": False}]},
    ]


def seed(audit: AuditLog) -> None:
    db = audit.db
    go_live = NOW - timedelta(days=6, hours=3)
    t = NOW - timedelta(days=DAYS)
    runs = []
    while t < NOW - timedelta(minutes=6):
        mode = "live" if t >= go_live else "shadow"
        rid = f"{R.getrandbits(48):012x}"
        dur = R.randint(20, 120)
        runs.append((rid, t, mode))
        db.execute("INSERT INTO runs (run_id, started_at, finished_at, mode, model, tickets_seen, decisions, errors) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (rid, t.isoformat(), (t + timedelta(seconds=dur)).isoformat(), mode,
                                                      "claude-sonnet-5-5", R.randint(3, 11), 0, 1 if R.random() < 0.02 else 0))
        t += timedelta(minutes=15)
    weights = {k: v[0] for k, v in CASES.items()}
    branches = ["Harare Main", "Bulawayo", "Gweru", "Mutare", "Borrowdale", "Msasa", "Kwekwe"]
    number = 600100
    ids = []
    for day_back in range(DAYS - 1, -1, -1):
        for when in sorted((NOW - timedelta(days=day_back, hours=R.uniform(0.2, 10))).replace(second=0)
                           for _ in range(R.randint(4, 10))):
            run = min(runs, key=lambda r: abs((r[1] - when).total_seconds()))
            number += R.randint(1, 5)
            status = pick(R, weights)
            if when >= OUTAGE_START and R.random() < 0.7:
                status = "ecocash_system_error"
            weight, action, reply = CASES[status]
            proposed, notes, confidence = action, [], round(R.uniform(0.87, 0.99), 2)
            if status == "already_linked" and R.random() < 0.2:
                proposed = "reply_and_close"
                notes = ["close changed to a reply that keeps the ticket open: latest attempt is already_linked"]
            if status == "ecocash_system_error" and when >= OUTAGE_START:
                action, reply = "escalate", ""
                proposed = "reply_and_close"
                notes = ["close blocked: an EcoCash incident is open, so 'try again later' may not help"]
            files = R.choice(FILES) if R.random() < 0.3 else []
            if status == "kyc_mismatch" and R.random() < 0.6:
                files = FILES[1]
            reason = REASONS[action].format(status=status)
            executed = []
            if run[2] == "live":
                executed = {"reply_and_close": ["reply", "close", "email queued"], "request_info": ["reply", "answered", "email queued"],
                            "escalate": ["note"]}[action]
            facts = {"latest": {"status": status, "via": R.choice(["client on USSD", "client on USSD", "account opening (iApprove)"]),
                                "currency": R.choice(["USD", "ZiG"]), "network": "Econet", "cif_series": R.choice(["09", "12", "86"]),
                                "product": R.choice(["3101", "1101"]), "id_format": "valid format", "branch": R.choice(branches)}}
            ids.append(audit.record(
                run_id=run[0], created_at=when.isoformat(timespec="seconds"), mode=run[2], ticket_id=number - 500000,
                ticket_number=str(number), last_entry_id=number * 3, last_message_id=number * 3, proposed_action=proposed,
                final_action=action, confidence=confidence, category=status, reason=reason, evidence=[f"latest attempt {status}"],
                account_statuses={"latest": status}, reply_text=reply or None, template_reply=reply or None,
                guardrail_notes=notes, tools_used=["find_ecocash_attempts"] + (["check_bank_link_record"] if status == "already_linked" else []),
                executed=executed, deferred=0, transcript=transcript(str(number), status, proposed, reply, reason, files),
                attachments=[{"number": i + 1, **f} for i, f in enumerate(files)] or None, facts=facts,
                branch=facts["latest"]["branch"], input_tokens=R.randint(4200, 8800) + 1600 * len([f for f in files if f["status"] == "read"]),
                output_tokens=R.randint(300, 800)))
            if run[2] == "live" and action != "escalate":
                db.execute("INSERT INTO notifications (uuid, created_at, decision_id, ticket_id, ticket_number, kind, channel, "
                           "recipient, cc, subject, status, attempts, sent_at) VALUES (?, ?, ?, ?, ?, ?, 'gateway', ?, '', ?, ?, ?, ?)",
                           (f"demo-{number}", when.isoformat(), ids[-1], number - 500000, str(number), action,
                            f"{R.choice(['t.moyo', 'r.ncube', 'p.dube'])}@{R.choice(branches).lower().replace(' ', '')}.branch.example",
                            f"[#{number}] EcoCash ticket " + ("resolved" if action == "reply_and_close" else "action or information needed"),
                            "sent" if R.random() > 0.05 else "failed", 1, (when + timedelta(minutes=1)).isoformat()))
    for did in ids[: int(len(ids) * 0.6)]:
        if R.random() < 0.45:
            row = db.execute("SELECT final_action, created_at FROM decisions WHERE id = ?", (did,)).fetchone()
            wrong = R.random() < 0.08
            db.execute("INSERT INTO reviews (decision_id, created_at, reviewer, verdict, expected_action, comment) VALUES (?, ?, ?, ?, ?, ?)",
                       (did, (datetime.fromisoformat(row["created_at"]) + timedelta(hours=3)).isoformat(timespec="seconds"),
                        R.choice(["dobe", "tsitsi.m"]), "incorrect" if wrong else "correct",
                        "escalate" if wrong else None, "The client had already delinked; needed ICT." if wrong else None))
    for at, actor, action, detail in [(NOW - timedelta(days=13, hours=5), "dobe", "hold at shadow", "first week in shadow"),
                                      (go_live, "dobe", "release mode hold", "replay of closed tickets reviewed")]:
        db.execute("INSERT INTO control_events (created_at, actor, action, detail) VALUES (?, ?, ?, ?)",
                   (at.isoformat(timespec="seconds"), actor, action, detail))
    db.commit()
    last = runs[-1][0]
    open_rows = db.execute("SELECT id, ticket_id, ticket_number, created_at, final_action FROM decisions WHERE mode = 'live' "
                           "AND final_action IN ('escalate', 'request_info') ORDER BY id DESC LIMIT 8").fetchall()
    audit.replace_queue(last, [{"ticket_id": r["ticket_id"], "ticket_number": r["ticket_number"], "help_topic": "EcoCash",
                                "opened": r["created_at"], "state": "skipped",
                                "detail": "already escalated" if r["final_action"] == "escalate" else "waiting on ticket owner",
                                "decision_id": r["id"]} for r in open_rows])


# ---- anomaly watch demo ----------------------------------------------------------------------
PROFILE = [0.1, 0.05, 0.05, 0.05, 0.1, 0.3, 0.8, 1.3, 1.6, 1.7, 1.6, 1.5, 1.5, 1.5, 1.4, 1.3, 1.2, 1.0, 0.8, 0.6, 0.4, 0.3, 0.2, 0.1]
USUAL_MIX = {"linked": 0.62, "already_linked": 0.2, "kyc_mismatch": 0.07, "number_not_ecocash": 0.04, "wallet_closed": 0.04,
             "ecocash_system_error": 0.02, "account_rejected": 0.005, "no_response": 0.005}


def _hour(start: datetime, at: datetime | None = None) -> dict[str, int]:
    """Attempts in the hour from start, as seen at `at` (an hour still in progress is counted so far)."""
    rng = random.Random(start.isoformat())
    seen = 1.0 if at is None else max(0.0, min(1.0, (at - start).total_seconds() / 3600))
    total = 14 * PROFILE[(start.astimezone(LOCAL_ZONE).hour - SHIFT) % 24] * rng.uniform(0.85, 1.15) * seen
    out = {k: int(round(total * share * rng.uniform(0.7, 1.3))) for k, share in USUAL_MIX.items()}
    if start + timedelta(hours=1) > OUTAGE_START and (at is None or at > OUTAGE_START + timedelta(minutes=10)):
        out["ecocash_system_error"] += int(total * 0.55)
        out["no_response"] += int(total * 0.1)
        out["linked"] = int(out["linked"] * 0.25)
    return out


def _collect(at: datetime) -> dict:
    hour0 = at.replace(minute=0, second=0, microsecond=0)
    current, recent, hourly = {}, {}, []
    for h in range(47, -1, -1):
        start = hour0 - timedelta(hours=h)
        c = _hour(start, at)
        hourly.append({"hour": start.astimezone(LOCAL_ZONE).strftime("%Y-%m-%dT%H:00"), "linked": c["linked"],
                       "failed": sum(v for k, v in c.items() if k not in ("linked", "no_response")),
                       "no_response": c["no_response"], "delink": int(c["already_linked"] * 0.3)})
        if start >= at - timedelta(hours=24):
            for k, v in c.items():
                current[k] = current.get(k, 0) + v
        if start >= hour0 - timedelta(hours=2):
            for k, v in c.items():
                recent[k] = recent.get(k, 0) + v
    base = {}
    for d in range(1, DAYS + 1):
        day = (at - timedelta(days=d + 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        for h in range(24):
            for k, v in _hour(day + timedelta(hours=h)).items():
                base[k] = base.get(k, 0) + v
    rng = random.Random(at.isoformat())
    outage = at > OUTAGE_START + timedelta(minutes=30)

    def row(via, currency, product):
        return attempt_features({"via": via, "channel": "IAPPROVE" if via == "IAPPROVE" else "NMBCONNECT", "currency": currency,
                                 "mobile_number": "263771234567", "account_number": f"09{rng.randint(10000, 99999)}{product}001",
                                 "id_number": "63-1234567K12", "requested_at": at.astimezone(LOCAL_ZONE).strftime("%Y-%m-%d %H:%M:%S")})
    failed = [dict(row(rng.choice(["USSD", "USSD", "IAPPROVE"]), rng.choice(["USD", "ZiG"]), rng.choice(["3101", "1101"])),
                   error_type=("ecocash_system_error" if outage and rng.random() < 0.7 else
                               rng.choice(["already_linked", "already_linked", "kyc_mismatch", "wallet_closed"])))
              for _ in range(120)]
    linked = [row(rng.choice(["USSD", "IAPPROVE", "IAPPROVE"]), rng.choice(["USD", "ZiG"]), rng.choice(["3101", "1101"]))
              for _ in range(150)]
    daily = [{"day": (at - timedelta(days=i)).astimezone(LOCAL_ZONE).date().isoformat(), "count": R.randint(4, 10)}
             for i in range(DAYS, -1, -1)]
    return {"generated_at": at.isoformat(timespec="seconds"), "window_hours": 24, "baseline_days": DAYS,
            "tickets": {"available": True, "current": 7 + (6 if outage else 0), "baseline": [R.randint(5, 9) for _ in range(DAYS)],
                        "by_topic": {"EcoCash": 7}, "by_topic_baseline": {"EcoCash": 7.0}, "by_branch": {"Harare Main": 3},
                        "by_branch_baseline": {"Harare Main": 2.0}, "daily": daily},
            "attempts": {"available": True, "current": current, "recent": recent,
                         "recent_since": (hour0 - timedelta(hours=2)).astimezone(LOCAL_ZONE).strftime("%H:00"),
                         "by_via": {}, "baseline_per_day": {k: round(v / DAYS, 1) for k, v in base.items()},
                         "baseline_days": DAYS, "recording_since": (NOW - timedelta(days=40)).isoformat(), "hourly": hourly,
                         "errors": []},
            "samples": {"available": True, "failed": failed, "linked": linked}}


def _summary(analysis: dict, at: datetime) -> dict:
    sig = {s["key"]: s for s in analysis["signals"]}
    if "ecocash_errors_recent" in sig or "kind_ecocash_system_error" in sig:
        since = OUTAGE_START.astimezone(LOCAL_ZONE).strftime("%H:%M")
        recent = sig.get("failure_rate_recent", {})
        return {"headline": f"EcoCash returning system errors since about {since}: most links failing",
                "summary": (f"Since about {since}, most link requests fail with EcoCash's temporary system error (TP40024) or get "
                            f"no answer: {recent.get('value', 'most')} failed in the last hours against a usual "
                            f"{recent.get('usual', 'few')}. The errors are spread across USSD and account opening, and across "
                            "currencies and products, so it points at EcoCash or the link to it rather than at the bank's data. "
                            "EcoCash tickets are up in the same period."),
                "likely_causes": ["An EcoCash API outage or degradation: TP40024 and no-answer responses rose together, on every channel."],
                "actions": ["Check ecocashreg.service for timeouts and confirm with EcoCash whether their API is degraded.",
                            "Hold 'try again later' advice to branches until EcoCash confirms recovery.",
                            "When it recovers, ask branches to have affected clients retry."],
                "branch_message": f"Since about {since} EcoCash linking is failing for many clients because of a problem on "
                                  "EcoCash's side. ICT is following up; please hold new tickets for this error.",
                "by": "model"}
    return fallback_summary(analysis)


def seed_anomalies(audit: AuditLog) -> None:
    db = audit.db
    db.executescript(ANOMALY_SCHEMA)
    active, keys, last_alert = False, set(), None
    for k in range(47, -1, -1):
        at = NOW - timedelta(hours=k, minutes=3)
        d = _collect(at)
        analysis = analyse(d, min_tickets=5, min_attempts=30)
        severity, key = analysis["severity"], incident_key(analysis)
        summary = _summary(analysis, at) if severity != "ok" else fallback_summary(analysis)
        alert = None
        if SEVERITY_RANK[severity] >= SEVERITY_RANK["incident"]:
            level = {s["key"] for s in analysis["signals"] if s["severity"] == "incident" and s["value"] is not None}
            alert = "incident" if not active else ("update" if level - keys else
                                                   ("reminder" if at - last_alert >= timedelta(hours=4) else None))
            active, keys = True, keys | level
            last_alert = at if alert else last_alert
        elif active and severity == "ok":
            alert, active, keys = "resolved", False, set()
        db.execute("INSERT INTO anomaly_reports (created_at, severity, incident_key, headline, summary, analysis, data, alert) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (at.isoformat(timespec="seconds"), severity, key, summary["headline"],
                                                      json.dumps(summary), json.dumps(analysis), json.dumps(_slim(d)), alert))
        if alert:
            sms, subject, _ = admin_text({"severity": severity, "summary": summary, "analysis": analysis},
                                         "https://ecocash-agent.bank.example", resolved=alert == "resolved")
            for channel, to in (("sms", "+263771000001"), ("gateway", "ict.ops@bank.example")):
                db.execute("INSERT INTO notifications (uuid, created_at, kind, channel, recipient, cc, subject, status, attempts, sent_at) "
                           "VALUES (?, ?, ?, ?, ?, '', ?, 'sent', 1, ?)", (f"admin-{k}-{to}", at.isoformat(), f"admin_{alert}",
                                                                           channel, to, subject, at.isoformat()))
    db.execute("INSERT OR REPLACE INTO anomaly_state (id, active, incident_key, started_at, last_alert_at) VALUES (1, ?, ?, ?, ?)",
               (int(active), "|".join(sorted(keys)) or None, OUTAGE_START.isoformat() if active else None,
                last_alert.isoformat() if last_alert else None))
    db.commit()


# ---- approvals and the live view -------------------------------------------------------------
def _approval_transcript(ticket: str, topic: str, message: str, steps: list, decision: dict) -> list:
    rendered = (f"Today: {NOW.date()}\nTicket #{ticket}   Help topic: {topic}   Raised by: Harare Main\n"
                "Subject: Client cannot link account to EcoCash\n\nForm fields (as captured on the ticket):\n"
                "  Mobile number: <MOBILE_1>\n  Account number: <NUMBER_1>\n\nThread, oldest first:\n--- Ticket owner\n" + message)
    out = [{"role": "user", "content": rendered + "\n\nInvestigate with the lookups, then call submit_decision."}]
    for i, (thought, name, args, result) in enumerate(steps, 1):
        blocks = ([{"type": "text", "text": thought}] if thought else []) + [{"type": "tool_use", "id": f"s{i}", "name": name, "input": args}]
        out += [{"role": "assistant", "content": blocks},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"s{i}", "content": json.dumps(result), "is_error": False}]}]
    out += [{"role": "assistant", "content": [{"type": "tool_use", "id": "d", "name": "submit_decision", "input": decision}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "d", "content": "Decision recorded.", "is_error": False}]}]
    return out


def _attempt(status, when="07-10-2026 09:14", outcome="failed", mobile="<MOBILE_1>"):
    return {"attempt_id": "a1042", "when": when, "request": "link", "outcome": outcome, "error_type": status,
            "requested_by": "client on USSD", "mobile_used": mobile, "account_used": "<NUMBER_1>", "currency": "USD"}


APPROVAL_CASES = [
    dict(ticket="600512", hours=0.7, status="already_linked", action="reply_and_close",
         message="Client gets 'already linked' when linking on *151#. She says she never linked before.",
         steps=[("", "find_ecocash_attempts", {"mobile": "<MOBILE_1>", "account_number": "<NUMBER_1>"},
                 {"found": 1, "attempts": [_attempt("already_linked")],
                  "summary": {"status": "already_linked", "suggested_action": "request_info",
                              "standard_reply": "Kindly delink the account on the Universal dashboard, then ask the client to link it again."}}),
                ("If the existing link is to her own number, there is nothing to delink.", "check_bank_link_record",
                 {"account_number": "<NUMBER_1>"},
                 {"found": 1, "links": [{"mobile": "<MOBILE_1>", "account": "<NUMBER_1>", "linked_on": "14-03-2025", "status": "1"}]})],
         reply=("Account <NUMBER_1> has been linked to EcoCash on <MOBILE_1> since 14-03-2025, which is why EcoCash says it is "
                "already linked. No delink is needed: kindly advise the client to choose the bank account option in the "
                "EcoCash menu on <MOBILE_1>, where the account is already available."),
         standard=False, conf=0.9),
    dict(ticket="600507", hours=1.6, status="number_not_ecocash", action="reply_and_close",
         message="Client cannot link. Acc <NUMBER_1>, cell <MOBILE_1>.",
         steps=[("", "find_ecocash_attempts", {"mobile": "<MOBILE_1>", "account_number": "<NUMBER_1>"},
                 {"found": 1, "attempts": [_attempt("number_not_ecocash", mobile="<MOBILE_2>")],
                  "summary": {"status": "number_not_ecocash", "suggested_action": "reply_and_close"}}),
                ("The attempt used a different number from the ticket. Checking the bank profile.", "check_bank_account",
                 {"account_number": "<NUMBER_1>"}, {"exists": True, "currency_code": "840", "mobile_on_file": "<MOBILE_1>"})],
         reply=("The client tried to link from <MOBILE_2>, which is not registered on EcoCash (EcoCash error TP40288). The "
                "number on the ticket and on the bank profile is <MOBILE_1>. Kindly ask the client to dial *151# from the "
                "<MOBILE_1> line and link the account from there."),
         standard=False, conf=0.91),
    dict(ticket="600499", hours=3.4, status="kyc_mismatch", action="request_info",
         message="KYC failed when linking. ID copy attached.",
         steps=[("", "find_ecocash_attempts", {"mobile": "<MOBILE_1>"},
                 {"found": 1, "attempts": [_attempt("kyc_mismatch")], "summary": {"status": "kyc_mismatch", "suggested_action": "reply_and_close"}}),
                ("The ID on the ticket differs from the one sent. A correction may already be waiting.", "get_pending_profile_changes",
                 {"account_number": "<NUMBER_1>"},
                 {"exists": True, "pending_changes": [{"field": "ID_NUMBER", "new_value": "<NATIONAL_ID_1>", "entered": "2026-10-06", "status": "awaiting authorisation"}]})],
         reply=("EcoCash rejected the link because the national ID the bank sent differs from the client's ID "
                "(EcoCash error BCS50646). A correction to <NATIONAL_ID_1> was captured on 06-10-2026 but is still awaiting "
                "authorisation. Kindly have it authorised, then ask the client to link again, and let us know here if it fails."),
         standard=False, conf=0.88),
]
DECIDED_CASES = [("600488", "reply_and_close", "posted", "dobe", "Wording tightened.", 1),
                 ("600481", "request_info", "escalated", "tsitsi.m", "ICT already delinked this one; handle manually.", 0),
                 ("600477", "reply_and_close", "dropped", None, None, 0)]


def seed_approvals(audit: AuditLog) -> None:
    from ecocash.approval import ApprovalStore

    store = ApprovalStore(audit.db)
    run = audit.db.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()["run_id"]
    for c in sorted(APPROVAL_CASES, key=lambda c: -c["hours"]):
        when = (NOW - timedelta(hours=c["hours"])).isoformat(timespec="seconds")
        mismatch = {"already_linked": "request info", "number_not_ecocash": None, "kyc_mismatch": "reply and close"}[c["status"]]
        why = "the agent reasoned beyond the standard reply for the latest attempt" + (
            f"; the latest attempt alone leads to {mismatch}, not {c['action'].replace('_', ' ')}" if mismatch else "")
        decision = {"action": c["action"], "confidence": c["conf"], "category": c["status"], "follows_standard_outcome": c["standard"],
                    "reason": "Lookups went beyond the standard reply; see the steps.", "evidence": [f"latest attempt {c['status']}"],
                    "reply_text": c["reply"]}
        tid = int(c["ticket"]) - 500000
        did = audit.record(run_id=run, created_at=when, mode="live", ticket_id=tid, ticket_number=c["ticket"],
                           last_entry_id=int(c["ticket"]) * 3, last_message_id=int(c["ticket"]) * 3, proposed_action=c["action"],
                           final_action=c["action"], confidence=c["conf"], category=c["status"], reason=decision["reason"],
                           evidence=decision["evidence"], account_statuses={"latest": c["status"]}, reply_text=c["reply"],
                           guardrail_notes=[], executed=["awaiting approval"], tools_used=[s[1] for s in c["steps"]],
                           approval=f"waiting: {why}", branch="Harare Main",
                           transcript=_approval_transcript(c["ticket"], "EcoCash", c["message"], c["steps"], decision),
                           input_tokens=R.randint(7000, 12000), output_tokens=R.randint(500, 900))
        aid = store.create(decision_id=did, ticket_id=tid, ticket_number=c["ticket"], last_entry_id=int(c["ticket"]) * 3,
                           action=c["action"], why=why, reply_masked=c["reply"], reply_text="(demo)", token_map={})
        audit.db.execute("UPDATE approvals SET created_at = ? WHERE id = ?", (when, aid))
    for i, (num, action, status, who, comment, edited) in enumerate(DECIDED_CASES):
        when = (NOW - timedelta(hours=20 + 9 * i)).isoformat(timespec="seconds")
        reply = "Account <NUMBER_1> is already linked to <MOBILE_1>; no delink is needed."
        did = audit.record(run_id=run, created_at=when, mode="live", ticket_id=int(num) - 500000, ticket_number=num,
                           last_entry_id=int(num) * 3, proposed_action=action, final_action=action, confidence=0.9,
                           category="already_linked", reason="Lookups went beyond the standard reply.", reply_text=reply,
                           executed=["awaiting approval"], approval="waiting: the agent reasoned beyond the standard reply")
        aid = store.create(decision_id=did, ticket_id=int(num) - 500000, ticket_number=num, last_entry_id=int(num) * 3,
                           action=action, why="the agent reasoned beyond the standard reply for the latest attempt",
                           reply_masked=reply, reply_text="(demo)", token_map={})
        if who:
            store.decide(aid, "approve" if status == "posted" else "reject", who, comment)
        store.finish(aid, status, {"posted": "reply, close, email queued", "escalated": "escalation note posted",
                                   "dropped": "the ticket changed before the reply was posted"}[status])
        audit.db.execute("UPDATE approvals SET created_at = ?, decided_at = ?, finished_at = ? WHERE id = ?", (when, when, when, aid))
    audit.db.commit()
    audit.live_start("b7e2a0c4d913")
    audit.live_ticket("600515", "EcoCash")
    for step in ("find_ecocash_attempts(<MOBILE_1>, <NUMBER_1>)", "get_ecocash_service_health(3)"):
        audit.live_step(step)


def snapshot() -> dict:
    """This desk's console data from a throwaway log seeded with invented tickets."""
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
        details = {str(s["id"]): data.decision_detail(conn, s["id"]) for s in summaries}
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
    return snapshot
