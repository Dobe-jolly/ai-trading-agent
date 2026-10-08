"""Investigative lookups, approval of off-script replies, and the console's live view. All data invented."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from agent.approval import ApprovalStore
from agent.lookups import Lookups, message_facts, parse_range
from agent.osticket import ESCALATION_NOTE_MARKER
from agentcore.pii import Pseudonymizer
from agent.portal import data
from agent.portal.server import HttpError, Portal
from agentcore.portal.users import UserStore
from agent.tools import Evidence
from tests.conftest import ScriptedModel, check, decision, response, tool_block
from tests.test_agent_runner import ACCT, CLOSE_REPLY, build, seed
from tests.test_notify import RecordingTransport

PENDING_SQL = "SELECT CHANGE_TYPE, NEW_VALUE FROM DEMO_PENDING_CHANGES WHERE CLIENT_NUM = '{client}'"
OFF_SCRIPT = ("The customer record has <MOBILE_1> but the alert registration still has the old number. Kindly update the "
              "IDCCOB registration for account <NUMBER_1>.")


def off_script(action="reply_and_close", reply=OFF_SCRIPT, standard=False, conf=0.92):
    return response(tool_block("submit_decision", {
        "action": action, "confidence": conf, "category": "different_number", "reason": "Found with the lookups.",
        "evidence": ["registration has the old number"], "reply_text": reply, "follows_standard_outcome": standard}, "d"))


def lookup(name, args, id_="l"):
    return response(tool_block(name, args, id_))


def with_lookups(runner, mb, pending_sql=None):
    runner.toolbox.lookups = Lookups(mb, runner.toolbox.checks, pending_sql)
    return runner


# ---- lookups -----------------------------------------------------------------------------
def test_activity_lists_transactions_and_which_raised_alerts(settings, mb):
    seed(mb)
    day = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    mb.accounts[ACCT]["plain_txns"] = [(day + " 00:00:00", "100.00")]
    runner, _ = build(settings, mb, ScriptedModel())
    lk = Lookups(mb, runner.toolbox.checks)
    start = date.today() - timedelta(days=10)
    out = lk.account_activity(ACCT, start, date.today())
    assert out["summary"] == "2 transaction(s), 1 raised an alert, 1 did not"
    plain = [t for t in out["transactions"] if t["amount"] == "100.00"][0]
    assert plain["alert_event"] is None and plain["date"] == day
    assert out["gateway_for_alerts"]["status"] == "delivered"
    assert lk.account_activity("00099999999999", start, date.today()) == {"account_number": "00099999999999", "found": False}


def test_gateway_messages_give_status_and_amount_not_the_text(settings, mb):
    seed(mb)
    runner, _ = build(settings, mb, ScriptedModel())
    out = Lookups(mb, runner.toolbox.checks).gateway_messages("0772123456", date.today() - timedelta(days=5), date.today())
    assert out["by_status"] == {"delivered": 1}
    msg = out["messages"][0]
    assert msg["currency"] == "USD" and msg["amount"] == "150.00" and msg["kind"] == "debit"
    assert "logged_responses" not in json.dumps(out) and "5678901" not in json.dumps(out)
    assert message_facts("Acc ****1234 credited ZWG 1,200.50 salary") == {"currency": "ZWG", "amount": "1200.50", "kind": "credit"}


def test_profile_and_pending_changes(settings, mb):
    seed(mb)
    mb.pending_changes = [{"CLIENT_NUM": "4410021", "CHANGE_TYPE": "MOBILE", "NEW_VALUE": "0772999000"},
                          {"CLIENT_NUM": "9999999", "CHANGE_TYPE": "MOBILE", "NEW_VALUE": "0771111111"}]
    runner, _ = build(settings, mb, ScriptedModel())
    plain = Lookups(mb, runner.toolbox.checks)
    assert "get_pending_profile_changes" not in plain.names  # hidden until PENDING_CHANGES_SQL is set
    profile = plain.customer_profile(ACCT)
    assert profile["sms_flag_on"] is True and profile["alert_registrations"][0]["mobile"] == "0772123456"
    assert profile["usable_email_on_file"] is True
    pending = Lookups(mb, runner.toolbox.checks, PENDING_SQL).pending_profile_changes(ACCT)
    assert pending["pending_changes"] == [{"client_num": "4410021", "change_type": "MOBILE", "new_value": "0772999000"}]


def test_date_ranges_are_bounded():
    today = date(2026, 10, 8)
    assert parse_range("2026-10-10", "2026-10-01", today) == (date(2026, 10, 1), date(2026, 10, 8))
    with pytest.raises(ValueError, match="31 days"):
        parse_range("2026-08-01", "2026-10-01", today)
    with pytest.raises(ValueError, match="13 months"):
        parse_range("2025-01-01", "2025-01-10", today)


def test_lookups_only_take_numbers_from_the_ticket(settings, mb):
    seed(mb)
    model = ScriptedModel(check(), lookup("get_customer_profile", {"account_number": "00012345670000"}),
                          decision("reply_and_close", CLOSE_REPLY))
    runner, audit = build(settings, mb, model, mode="live")
    with_lookups(runner, mb)
    runner.run()
    result = model.calls[2]["messages"][-1]["content"][0]
    assert result["is_error"] and result["content"].startswith("Refused")
    row = audit.last_for_ticket(7)
    assert row["final_action"] == "escalate" and "not on the ticket" in row["guardrail_notes"]
    assert mb.tickets[7]["state"] == "open"


# ---- approvals ---------------------------------------------------------------------------
def test_off_script_reply_waits_then_is_posted_once_approved(settings, mb):
    seed(mb)
    model = ScriptedModel(check(), lookup("get_customer_profile", {"account_number": "<NUMBER_1>"}), off_script())
    runner, audit = build(settings, mb, model, mode="live")
    with_lookups(runner, mb)
    transport = RecordingTransport()
    from agent.notify import Notifier
    nsettings = replace(runner.s, notify_transport="gateway", notif_email_url="http://gw.test/email")
    runner.notifier = Notifier(audit, nsettings, transport)

    summary = runner.run()
    assert summary.actions == {"awaiting approval": 1} and mb.writes == []
    store = ApprovalStore(audit.db)
    pending = store.open_for(7)
    assert pending["status"] == "pending" and "beyond the standard" in pending["why"]
    assert "12345678901" in pending["reply_text"] and "<NUMBER_1>" in pending["reply_masked"]
    assert audit.last_for_ticket(7)["approval"].startswith("waiting:")
    queue = audit.db.execute("SELECT state FROM queue_state").fetchone()
    assert queue["state"] == "awaiting approval"

    assert runner.run().skipped == {"awaiting approval": 1}  # no model call while it waits

    edited = pending["reply_masked"] + " Thank you."
    store.decide(pending["id"], "approve", "dobe", "fine", edited)
    runner.run()
    posted = [e for e in mb.thread_entries(7) if e["type"] == "R"][-1]["body"]
    assert "Thank you." in posted and "12345678901" in posted and "<NUMBER_1>" not in posted
    assert mb.tickets[7]["state"] == "closed"
    done = store.get(pending["id"])
    assert done["status"] == "posted" and done["reply_text"] is None and done["token_map"] is None
    last = audit.last_for_ticket(7)
    assert last["category"] == "approved_reply" and "email queued" in json.loads(last["executed"])
    assert transport.sent and "12345678901" in transport.sent[0]["body"]


def test_rejected_reply_becomes_an_escalation_note(settings, mb):
    seed(mb)
    runner, audit = build(settings, mb, ScriptedModel(check(), off_script()), mode="live")
    with_lookups(runner, mb)
    runner.run()
    store = ApprovalStore(audit.db)
    store.decide(store.open_for(7)["id"], "reject", "tsitsi.m", "Wrong registration; handle manually")
    runner.run()
    note = mb.thread_entries(7)[-1]
    assert note["type"] == "N" and ESCALATION_NOTE_MARKER in note["body"] and "handle manually" in note["body"]
    assert "12345678901" in note["body"] and mb.tickets[7]["state"] == "open"
    assert store.get(1)["status"] == "escalated"


def test_a_changed_ticket_drops_the_waiting_reply_and_is_decided_again(settings, mb):
    seed(mb)
    model = ScriptedModel(check(), off_script(), check(), decision("reply_and_close", CLOSE_REPLY))
    runner, audit = build(settings, mb, model, mode="live")
    runner.run()
    mb.add_entry(7, "M", "Any update?")
    runner.run()
    store = ApprovalStore(audit.db)
    assert store.get(1)["status"] == "dropped" and store.get(1)["reply_text"] is None
    assert mb.tickets[7]["state"] == "closed"  # the new decision was the standard close


def test_standard_outcomes_go_straight_out_and_mismatches_wait(settings, mb):
    seed(mb)
    runner, audit = build(settings, mb, ScriptedModel(check(), decision("reply_and_close", CLOSE_REPLY)), mode="live")
    runner.run()
    assert mb.tickets[7]["state"] == "closed" and ApprovalStore(audit.db).pending_count() == 0

    mb2 = type(mb)()
    seed(mb2)
    ask = "The gateway shows delivery to <MOBILE_1>. Kindly confirm the SIM in the client's phone is this number."
    runner2, audit2 = build(replace(settings, state_db=settings.state_db.with_name("b.sqlite")), mb2,
                            ScriptedModel(check(), decision("request_info", ask)), mode="live")
    runner2.run()
    row = ApprovalStore(audit2.db).open_for(7)
    assert row is not None and "lead to reply and close, not request info" in row["why"]


def test_shadow_and_off_modes(settings, mb):
    seed(mb)
    runner, audit = build(settings, mb, ScriptedModel(check(), off_script()), mode="shadow")
    runner.run()
    assert audit.last_for_ticket(7)["approval"].startswith("would wait:")
    assert ApprovalStore(audit.db).pending_count() == 0
    mb.add_entry(7, "M", "again")
    runner2, audit2 = build(replace(settings, approval_mode="off"), mb, ScriptedModel(check(), off_script()), mode="live")
    runner2.s = replace(runner2.s, approval_mode="off")
    runner2.run()
    assert mb.tickets[7]["state"] == "closed"


# ---- console -------------------------------------------------------------------------------
def test_console_approval_endpoints(settings, mb, tmp_path):
    seed(mb)
    runner, audit = build(settings, mb, ScriptedModel(check(), off_script()), mode="live")
    runner.run()
    s = replace(settings, portal_users_file=tmp_path / "u.json", mode="live")
    users = UserStore(s.portal_users_file)
    users.set("dobe", "correct horse battery", "admin")
    users.set("viewer1", "another long password", "viewer")
    portal = Portal(s, users)
    listing = portal.get("/api/approvals", {}, "dobe", "admin")
    item = listing["pending"][0]
    assert listing["can_decide"] and "reply_text" not in item and "token_map" not in item
    assert portal.session("dobe", "admin")["approvals_pending"] == 1
    with pytest.raises(HttpError) as err:
        portal.post(f"/api/approvals/{item['id']}", {"verdict": "approve"}, "viewer1", "viewer")
    assert err.value.status == 403
    with pytest.raises(HttpError) as err:
        portal.post(f"/api/approvals/{item['id']}", {"verdict": "approve", "reply": "Call 0772 555 444 now please."},
                    "dobe", "admin")
    assert err.value.status == 400 and "never shown" in err.value.message
    done = portal.post(f"/api/approvals/{item['id']}", {"verdict": "approve", "comment": "ok"}, "dobe", "admin")
    assert done["status"] == "approved" and "reply_text" not in done
    with pytest.raises(HttpError) as err:
        portal.post(f"/api/approvals/{item['id']}", {"verdict": "reject"}, "dobe", "admin")
    assert err.value.status == 409
    detail = portal.get(f"/api/decisions/{item['decision_id']}", {}, "dobe", "admin")
    assert detail["approval"]["status"] == "approved"
    assert portal.get("/api/overview", {}, "dobe", "admin")["kpis"]["approvals_pending"] == 0


def test_live_view_tracks_the_pass_with_masked_steps(settings, mb):
    seed(mb)
    runner, audit = build(settings, mb, ScriptedModel(check(), decision("reply_and_close", CLOSE_REPLY)))
    seen = []
    original = audit.live_step
    audit.live_step = lambda text: (seen.append(text), original(text))
    runner.run()
    assert any(t.startswith("check_sms_alert_account(<NUMBER_1>") for t in seen)
    assert any(t.startswith("decided: reply_and_close") for t in seen)
    assert not any("12345678901" in t for t in seen)
    live = data.live(audit.db)
    assert live["running"] is False and live["steps"]
