"""A pass of the NMB Connect desk end to end, with a scripted model, and the rules in code. All data invented."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

from agentcore.approval import ApprovalStore
from agentcore.audit import AuditLog
from agentcore.loop import AgentError
from agentcore.writer import OsTicketWriter, ShadowWriter
from nmb_connect.loop import TicketAgent
from nmb_connect.normalize import now_local
from nmb_connect.notify import Notifier
from nmb_connect.osticket import ESCALATION_NOTE_MARKER, OsTicket
from nmb_connect.runner import Runner
from nmb_connect.sources import AppActivity, CoreBanking, Registration, RegistrationFailures, SmsGateway
from nmb_connect.tools import Toolbox
from tests.nmb_connect.conftest import (
    AGENT_STAFF, CIF, CONECT_DB, FAILURES_DB, MOBILE, NEW_MOBILE, OTHER_MOBILE, REG_DB, SOCKET_DB, STANDBY_DB,
    TICKET_DB, ScriptedModel, call, decide,
)


class RecordingTransport:
    channel = "gateway"

    def __init__(self):
        self.sent = []

    def send(self, **kw):
        self.sent.append(kw)

    def close(self):
        pass


def build(settings, mb, model, mode="shadow", emails=None, **overrides):
    s = replace(settings, mode=mode, **overrides)
    store = OsTicket(mb, TICKET_DB, topics=s.topics, exclude_topics=s.exclude_topics)
    toolbox = Toolbox(Registration(mb, REG_DB), CoreBanking(mb, STANDBY_DB), AppActivity(mb, CONECT_DB),
                      RegistrationFailures(mb, FAILURES_DB), SmsGateway(mb, SOCKET_DB))
    agent = TicketAgent(model, "test-model", "system prompt", toolbox, max_turns=8, agent_staff_id=AGENT_STAFF)
    writer = OsTicketWriter(store, AGENT_STAFF, "NMB Connect Agent") if mode != "shadow" else ShadowWriter()
    audit = AuditLog(s.state_db)
    notifier = Notifier(audit, s, emails) if emails is not None else None
    return Runner(s, store, agent, toolbox, writer, audit, mode, notifier), audit


OTP = f"Client CIF {CIF}, cell {MOBILE}, is not receiving the OTP when logging in to the app"
PROFILE = call("get_connect_profile", {"cif": CIF, "mobile": "<MOBILE_1>"}, "p")
ASSESS_OTP = call("assess_access_issue", {"problem": "otp_not_received"}, "a")
OTP_REPLY = ("The client is registered on NMB Connect with <MOBILE_1>, the number on the ticket, so the OTP goes to the "
             "right number. Kindly ask the client to check the blocked or spam SMS folder, unblock the bank's sender "
             "and request a new OTP. If it still does not arrive, reply on this ticket.")


def test_shadow_pass_masks_everything_and_writes_nothing(settings, mb):
    mb.add_profile()
    mb.add_ticket(1, "300001", [OTP])
    model = ScriptedModel(PROFILE, ASSESS_OTP, decide("reply_and_close", OTP_REPLY, "registered", standard=True))
    runner, audit = build(settings, mb, model)
    summary = runner.run()
    assert summary.actions == {"reply_and_close": 1} and not mb.writes
    sent = json.dumps([c["messages"] for c in model.calls], default=str)
    assert "0771234567" not in sent and "tmoyo77" not in sent and "notarealhash" not in sent
    row = audit.last_for_ticket(1)
    assert json.loads(row["account_statuses"]) == {"connect": "registered"}
    facts = json.loads(row["facts"])["assessment"]
    assert facts["problem"] == "otp_not_received" and facts["status"] == "registered" and facts["profiles"] == 1
    assert "blocked or spam SMS folder" in row["template_reply"]


def test_live_close_posts_the_real_number_and_emails_the_branch(settings, mb):
    mb.add_profile()
    mb.add_ticket(1, "300001", [OTP])
    emails = RecordingTransport()
    model = ScriptedModel(PROFILE, ASSESS_OTP, decide("reply_and_close", OTP_REPLY, standard=True))
    runner, _ = build(settings, mb, model, mode="live", emails=emails, notify_transport="gateway",
                      notif_email_url="http://gw.test/email")
    runner.run()
    reply = [e for e in mb.thread_entries(1) if e["type"] == "R"][-1]["body"]
    assert "0771234567" in reply and mb.closed == ["300001"]
    assert emails.sent[0]["subject"] == "[#300001] NMB Connect ticket resolved"


def test_already_registered_keeps_the_ticket_open_then_closes_on_the_new_profile(settings, mb):
    old = mb.add_profile(created=now_local() - timedelta(days=300))
    mb.add_ticket(1, "300001", [f"CIF {CIF} cell {MOBILE}: 'Already registered, kindly log in' and cannot log in"],
                  topic="NMB Connect / Registration")
    ask = ("The client already has an NMB Connect profile, which is why registration says the client is already "
           "registered. Kindly delete the profile on the dashboard and have the client register again.")
    model = ScriptedModel(PROFILE, call("assess_access_issue", {"problem": "cannot_register"}, "a"),
                          decide("reply_and_close", ask, "already_registered", standard=True))  # the model tries to close
    runner, audit = build(settings, mb, model, mode="live")
    runner.run()
    row = audit.last_for_ticket(1)
    assert row["final_action"] == "request_info" and mb.closed == []  # kept open: the branch acts first
    assert "kept open" in json.loads(row["guardrail_notes"])[0]
    # the branch deletes the profile, the client registers again, the branch replies
    mb.profiles.remove(old)
    mb.add_profile(created=now_local() - timedelta(minutes=10))
    mb.add_entry(1, "M", "Done, profile deleted and client re-registered", created=now_local() - timedelta(minutes=5))
    done = ("The client's NMB Connect profile was created again today with mobile <MOBILE_1>. The client can now log in.")
    runner, audit = build(settings, mb, ScriptedModel(PROFILE, call("assess_access_issue", {"problem": "cannot_register"}),
                                                      decide("reply_and_close", done, standard=True)), mode="live")
    runner.run()
    assert audit.last_for_ticket(1)["account_statuses"] == json.dumps({"connect": "reregistered"})
    assert mb.closed == ["300001"]


def test_closing_on_an_outcome_that_needs_a_person_is_blocked(settings, mb):
    mb.add_profile()
    mb.add_profile()  # two profiles for the CIF
    mb.add_ticket(1, "300001", [OTP])
    model = ScriptedModel(PROFILE, ASSESS_OTP, decide("reply_and_close", OTP_REPLY))
    runner, audit = build(settings, mb, model, mode="live")
    runner.run()
    row = audit.last_for_ticket(1)
    assert row["final_action"] == "escalate" and "duplicate_profiles" in row["guardrail_notes"]
    note = mb.thread_entries(1)[-1]["body"]
    assert ESCALATION_NOTE_MARKER in note and "2 active NMB Connect profiles" in note


def test_a_reply_that_contradicts_the_lookups_is_blocked(settings, mb):
    mb.add_profile()
    mb.add_ticket(1, "300001", [OTP])
    wrong = ("The client is not registered on NMB Connect. Kindly register the client and ask them to log in again.")
    runner, audit = build(settings, mb, ScriptedModel(PROFILE, ASSESS_OTP, decide("reply_and_close", wrong)))
    runner.run()
    assert "says the client is not registered" in audit.last_for_ticket(1)["guardrail_notes"]


def test_asking_for_the_otp_is_blocked(settings, mb):
    mb.add_profile()
    mb.add_ticket(1, "300001", [OTP])
    ask = "Kindly ask the client to send us the OTP they received so that we can check it against the profile."
    runner, audit = build(settings, mb, ScriptedModel(PROFILE, ASSESS_OTP, decide("request_info", ask)))
    runner.run()
    assert "asks for or gives out" in audit.last_for_ticket(1)["guardrail_notes"]


def test_looking_up_someone_else_blocks_the_reply(settings, mb):
    mb.add_profile()
    mb.add_ticket(1, "300001", [OTP])
    mb.profiles.append({**mb.profiles[0], "id": 9, "customer_number": "8600099", "mobile_number": OTHER_MOBILE})
    model = ScriptedModel(call("get_connect_profile", {"mobile": OTHER_MOBILE}, "x"), PROFILE, ASSESS_OTP,
                          decide("reply_and_close", OTP_REPLY))
    runner, audit = build(settings, mb, model)
    runner.run()
    assert "tried to look up mobile" in audit.last_for_ticket(1)["guardrail_notes"]


def test_off_script_reply_waits_for_approval(settings, mb):
    mb.add_profile(mobile=NEW_MOBILE)
    mb.add_ticket(1, "300001", [OTP])
    # the standard outcome keeps the ticket open (another number); the model closes with its own advice
    own = ("The client's profile has another number, <MOBILE_2>. Kindly ask the client to use that number for the "
           "OTP from now on.")
    model = ScriptedModel(call("get_connect_profile", {"cif": CIF}, "p"), ASSESS_OTP,
                          decide("request_info", own, standard=False))
    runner, audit = build(settings, mb, model, mode="live")
    assert runner.run().actions == {"awaiting approval": 1} and not mb.writes
    assert "beyond the standard outcome" in ApprovalStore(audit.db).open_for(1)["why"]


def test_without_claude_a_ticket_without_details_is_asked_and_others_wait(settings, mb):
    mb.add_ticket(1, "300001", ["Client cannot log in to the app, kindly assist"])
    mb.add_ticket(2, "300002", [OTP])
    runner, audit = build(settings, mb, ScriptedModel(), mode="live")
    runner.agent.decide = lambda *a, **k: (_ for _ in ()).throw(AgentError("connection refused"))
    summary = runner.run()
    assert summary.actions == {"request_info": 1} and summary.errors == 1
    assert "CIF or account number" in [e for e in mb.thread_entries(1) if e["type"] == "R"][-1]["body"]


def test_failed_transactions_under_nmb_connect_are_left_to_their_desk(settings, mb):
    mb.add_ticket(1, "300001", [OTP], topic="NMB Connect / USSD / Failed Transactions / ZIPIT")
    mb.add_ticket(2, "300002", [OTP], topic="NMB Connect / USSD / OTP")
    runner, _ = build(settings, mb, ScriptedModel())
    assert [t.number for t in runner.discover(None)] == ["300002"]
