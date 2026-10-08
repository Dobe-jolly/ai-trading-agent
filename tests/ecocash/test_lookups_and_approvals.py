"""New lookups, approval of off-script replies, and the console's live view. All data invented."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from ecocash.approval import ApprovalStore
from ecocash.ecocash import BankAccounts
from ecocash.normalize import now_local
from ecocash.osticket import ESCALATION_NOTE_MARKER
from ecocash.portal import data
from ecocash.portal.server import HttpError, Portal
from agentcore.portal.users import UserStore
from tests.ecocash.conftest import ACCOUNT, STANDBY_DB, ScriptedModel, decision, find, response, tool_block
from tests.ecocash.test_runner import CLOSED_REPLY, DELINK_REPLY, RecordingTransport, build

PENDING_SQL = "SELECT CHANGE_TYPE, NEW_VALUE FROM DEMO_PENDING_CHANGES WHERE CLIENT_NUM = '{client}'"
LINKED_REPLY = ("Account <NUMBER_1> is already linked to EcoCash on <MOBILE_1>, so no delink is needed. Kindly advise the "
                "client to choose the bank account option in the EcoCash menu.")


def off_script(action="reply_and_close", reply=LINKED_REPLY, standard=False, conf=0.92):
    return response(tool_block("submit_decision", {
        "action": action, "confidence": conf, "category": "already_linked", "reason": "Found with the lookups.",
        "evidence": ["link record shows the same number"], "reply_text": reply, "follows_standard_outcome": standard}, "d"))


def call(name, args, id_="l"):
    return response(tool_block(name, args, id_))


def seed(mb, kind="already_linked"):
    mb.add_ticket(1, "100001", ["Client 0771234567 gets already linked, acc 86000013101001"])
    mb.add_attempt(kind)


# ---- lookups -----------------------------------------------------------------------------
def test_service_health_compares_with_the_same_hours_yesterday(settings, mb):
    now = now_local()
    for i in range(6):
        mb.add_attempt("ecocash_system_error", now - timedelta(minutes=10 + i))
    mb.add_attempt("linked", now - timedelta(minutes=30))
    mb.add_attempt("linked", now - timedelta(days=1, minutes=30))
    seed(mb, "ecocash_system_error")
    model = ScriptedModel(find(), call("get_ecocash_service_health", {"hours": 2}),
                          decision("escalate", category="ecocash_system_error"))
    runner, audit = build(settings, mb, model)
    runner.run()
    result = json.loads(model.calls[2]["messages"][-1]["content"][0]["content"])
    assert result["by_result"]["ecocash_system_error"] == 6 and result["same_hours_yesterday"] == {"linked": 1}
    assert result["summary"].startswith("6 of 7 request(s) failed in the last 2 h")


def test_pending_changes_are_hidden_until_configured_and_scoped_to_the_ticket(settings, mb):
    seed(mb, "kyc_mismatch")
    mb.accounts[ACCOUNT] = {"client": "8600001"}
    mb.pending_changes = [{"CLIENT_NUM": "8600001", "CHANGE_TYPE": "ID_NUMBER", "NEW_VALUE": "58-7654321 F 09"},
                          {"CLIENT_NUM": "1111111", "CHANGE_TYPE": "MOBILE", "NEW_VALUE": "0779999999"}]
    runner, _ = build(settings, mb, ScriptedModel())
    assert "get_pending_profile_changes" not in {t["name"] for t in runner.toolbox.schemas()}
    runner.toolbox.accounts = BankAccounts(mb, STANDBY_DB, pending_sql=PENDING_SQL)
    assert "get_pending_profile_changes" in {t["name"] for t in runner.toolbox.schemas()}
    out = runner.toolbox.accounts.pending_changes(ACCOUNT)
    assert out["pending_changes"] == [{"client_num": "8600001", "change_type": "ID_NUMBER", "new_value": "58-7654321 F 09"}]

    model = ScriptedModel(call("get_pending_profile_changes", {"account_number": "86000099999999"}),
                          decision("reply_and_close", CLOSED_REPLY))
    runner2, audit = build(settings, mb, model)
    runner2.toolbox.accounts = BankAccounts(mb, STANDBY_DB, pending_sql=PENDING_SQL)
    runner2.run()
    refused = model.calls[1]["messages"][-1]["content"][0]
    assert refused["is_error"] and "not on this ticket" in refused["content"]
    assert audit.last_for_ticket(1)["final_action"] == "escalate"


# ---- approvals ---------------------------------------------------------------------------
def test_off_script_reply_waits_then_is_posted_once_approved(settings, mb):
    seed(mb)
    transport = RecordingTransport()
    s = replace(settings, notify_transport="gateway", notif_email_url="http://gw.test/email")
    runner, audit = build(s, mb, ScriptedModel(find(), off_script()), mode="live", emails=transport)
    assert runner.run().actions == {"awaiting approval": 1} and not mb.writes
    store = ApprovalStore(audit.db)
    pending = store.open_for(1)
    # The guardrails keep an already-linked ticket open, so the reply waits as a request for information.
    assert "beyond the standard reply" in pending["why"] and pending["action"] == "request_info"
    assert "86000013101001" in pending["reply_text"] and "<NUMBER_1>" in pending["reply_masked"]
    assert runner.run().skipped == {"awaiting approval": 1}

    store.decide(pending["id"], "approve", "dobe", None, pending["reply_masked"] + " Thank you.")
    runner.run()
    posted = [e for e in mb.thread_entries(1) if e["type"] == "R"][-1]["body"]
    assert "Thank you." in posted and "86000013101001" in posted and mb.closed == []
    done = store.get(pending["id"])
    assert done["status"] == "posted" and done["reply_text"] is None and done["token_map"] is None
    assert transport.sent and "86000013101001" in transport.sent[0]["body"]


def test_standard_replies_go_straight_out(settings, mb):
    seed(mb, "wallet_closed")
    runner, audit = build(settings, mb, ScriptedModel(find(), decision("reply_and_close", CLOSED_REPLY)), mode="live")
    runner.run()
    assert mb.closed == ["100001"] and ApprovalStore(audit.db).pending_count() == 0


def test_rejected_reply_becomes_an_escalation_note(settings, mb):
    seed(mb)
    runner, audit = build(settings, mb, ScriptedModel(find(), off_script()), mode="live")
    runner.run()
    store = ApprovalStore(audit.db)
    store.decide(store.open_for(1)["id"], "reject", "tsitsi.m", "ICT delinked it already")
    runner.run()
    note = mb.thread_entries(1)[-1]
    assert note["type"] == "N" and ESCALATION_NOTE_MARKER in note["body"] and "ICT delinked it already" in note["body"]
    assert mb.closed == []


def test_a_changed_ticket_drops_the_waiting_reply(settings, mb):
    seed(mb)
    model = ScriptedModel(find(), off_script(), find(), decision("request_info", DELINK_REPLY, category="already_linked"))
    runner, audit = build(settings, mb, model, mode="live")
    runner.run()
    mb.add_entry(1, "M", "Any news?")
    runner.run()
    assert ApprovalStore(audit.db).get(1)["status"] == "dropped"
    assert [e for e in mb.thread_entries(1) if e["type"] == "R"]  # the standard delink request went out


def test_console_endpoints_and_live_view(settings, mb, tmp_path):
    seed(mb)
    runner, audit = build(settings, mb, ScriptedModel(find(), off_script()), mode="live")
    runner.run()
    s = replace(settings, portal_users_file=tmp_path / "u.json", mode="live")
    users = UserStore(s.portal_users_file)
    users.set("dobe", "correct horse battery", "admin")
    users.set("viewer1", "another long password", "viewer")
    portal = Portal(s, users)
    item = portal.get("/api/approvals", {}, "dobe", "admin")["pending"][0]
    assert "reply_text" not in item and "token_map" not in item
    with pytest.raises(HttpError) as err:
        portal.post(f"/api/approvals/{item['id']}", {"verdict": "approve"}, "viewer1", "viewer")
    assert err.value.status == 403
    with pytest.raises(HttpError) as err:
        portal.post(f"/api/approvals/{item['id']}", {"verdict": "approve", "reply": "Call 0772 555 444 please, today."},
                    "dobe", "admin")
    assert err.value.status == 400
    assert portal.post(f"/api/approvals/{item['id']}", {"verdict": "approve"}, "dobe", "admin")["status"] == "approved"
    assert portal.session("dobe", "admin")["approvals_pending"] == 0
    live = data.live(audit.db)
    assert live["running"] is False and any(st["text"].startswith("find_ecocash_attempts(<MOBILE_1>") for st in live["steps"])
