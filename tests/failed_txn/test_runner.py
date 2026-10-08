"""A pass of the failed-transactions desk end to end, with a scripted model. All data invented."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

from agentcore.approval import ApprovalStore
from agentcore.audit import AuditLog
from agentcore.loop import AgentError
from agentcore.writer import OsTicketWriter, ShadowWriter
from failed_txn.loop import TicketAgent
from failed_txn.normalize import now_local
from failed_txn.notify import Notifier
from failed_txn.osticket import ESCALATION_NOTE_MARKER, OsTicket
from failed_txn.runner import Runner
from failed_txn.sources import Connect, CoreBanking, InternetBanking, Postilion
from failed_txn.tools import Toolbox
from tests.failed_txn.conftest import (
    AGENT_STAFF, BILLERS_DB, CONECT_DB, IB_DB, POSTOFFICE_DB, RRN, STANDBY_DB, TICKET_DB, ScriptedModel, call, decide,
)


class RecordingTransport:
    channel = "gateway"

    def __init__(self):
        self.sent = []

    def send(self, **kw):
        self.sent.append(kw)

    def close(self):
        pass


def build(settings, mb, model, mode="shadow", emails=None):
    s = replace(settings, mode=mode)
    store = OsTicket(mb, TICKET_DB, topics=s.topics, exclude_topics=s.exclude_topics)
    toolbox = Toolbox(Postilion(mb, POSTOFFICE_DB), Connect(mb, CONECT_DB, BILLERS_DB), InternetBanking(mb, IB_DB),
                      CoreBanking(mb, STANDBY_DB))
    agent = TicketAgent(model, "test-model", "system prompt", toolbox, max_turns=8, agent_staff_id=AGENT_STAFF)
    writer = OsTicketWriter(store, AGENT_STAFF, "Txn Agent") if mode != "shadow" else ShadowWriter()
    audit = AuditLog(s.state_db)
    notifier = Notifier(audit, s, emails) if emails is not None else None
    return Runner(s, store, agent, toolbox, writer, audit, mode, notifier), audit


DECLINED_REPLY = ("The ZIPIT of USD 20.00 was declined (insufficient funds) and no money left the client's account "
                  "<NUMBER_1>. Kindly advise the client to try again once the account is funded.")
SEARCH = call("find_switch_transactions", {"account_number": "<NUMBER_1>", "amount": "20"}, "s")
CONFIRM = call("confirm_transaction", {"transaction_id": "T1"}, "c")


def seed(mb, rsp="51", reversed_flag="0"):
    mb.add_ticket(1, "200001", ["Client acc 86000013101001 tried a ZIPIT of $20 yesterday, says it failed"])
    mb.add_switch(20, rsp=rsp, reversed_flag=reversed_flag)


def test_shadow_pass_masks_everything_and_writes_nothing(settings, mb):
    seed(mb)
    model = ScriptedModel(SEARCH, CONFIRM, decide("reply_and_close", DECLINED_REPLY, "declined", standard=True))
    runner, audit = build(settings, mb, model)
    summary = runner.run()
    assert summary.actions == {"reply_and_close": 1} and not mb.writes
    prompt = model.calls[0]["messages"][0]["content"]
    assert "<NUMBER_1>" in prompt and "86000013101001" not in prompt and "Amounts: 20.00" in prompt
    tool_result = model.calls[1]["messages"][-1]["content"][0]["content"]
    assert "declined" in tool_result and RRN not in tool_result
    row = audit.last_for_ticket(1)
    assert json.loads(row["account_statuses"]) == {"transaction": "declined"}
    assert "86000013101001" not in json.dumps(dict(row))
    assert "declined (insufficient funds)" in row["template_reply"] and "86000013101001" not in row["template_reply"]


def test_live_close_posts_the_reply_and_emails_the_branch(settings, mb):
    seed(mb)
    emails = RecordingTransport()
    s = replace(settings, notify_transport="gateway", notif_email_url="http://gw.test/email")
    model = ScriptedModel(SEARCH, CONFIRM, decide("reply_and_close", DECLINED_REPLY, "declined", standard=True))
    runner, audit = build(s, mb, model, mode="live", emails=emails)
    runner.run()
    reply = mb.thread_entries(1)[-1]
    assert reply["type"] == "R" and "86000013101001" in reply["body"] and mb.closed == ["200001"]
    assert emails.sent and emails.sent[0]["subject"] == "[#200001] Failed transaction ticket resolved"


def test_an_uncertain_switch_answer_is_escalated_with_a_note_for_operations(settings, mb):
    seed(mb, rsp="91")
    model = ScriptedModel(SEARCH, CONFIRM, decide("reply_and_close", "The ZIPIT of USD 20.00 did not go through. "
                                                  "Kindly advise the client to try again.", "uncertain"))
    runner, audit = build(settings, mb, model, mode="live")
    runner.run()
    note = mb.thread_entries(1)[-1]
    assert note["type"] == "N" and ESCALATION_NOTE_MARKER in note["body"] and mb.closed == []
    assert "uncertain" in note["body"] and "Operations" in note["body"] and RRN in note["body"]
    assert "close blocked" in audit.last_for_ticket(1)["guardrail_notes"]


def test_an_off_script_reply_waits_for_approval(settings, mb):
    seed(mb)
    reply = ("The ZIPIT of USD 20.00 was declined (insufficient funds) and no money left the client's account. Kindly "
             "also advise the client to update the mobile number on file at the branch.")
    model = ScriptedModel(SEARCH, CONFIRM, decide("reply_and_close", reply, "declined", standard=False))
    runner, audit = build(settings, mb, model, mode="live")
    assert runner.run().actions == {"awaiting approval": 1} and not mb.writes
    pending = ApprovalStore(audit.db).open_for(1)
    assert "beyond the standard outcome" in pending["why"]


def test_nothing_found_asks_the_branch_then_waits(settings, mb):
    mb.add_ticket(1, "200001", ["Client acc 86000013101001 says the ZESA token never came"], topic="FAILED ZESA")
    ask = ("We could not find the ZESA payment on account <NUMBER_1>. Kindly share the date and time, the amount and a "
           "screenshot of the message the client received.")
    model = ScriptedModel(call("find_nmb_connect_transactions", {"account_number": "<NUMBER_1>", "kind": "bill"}),
                          decide("request_info", ask, "not_found", standard=True))
    runner, audit = build(settings, mb, model, mode="live")
    runner.run()
    assert mb.thread_entries(1)[-1]["type"] == "R" and json.loads(audit.last_for_ticket(1)["account_statuses"]) == \
        {"transaction": "not_found"}
    assert runner.run().skipped == {"waiting on ticket owner": 1}


def test_tickets_under_other_topics_are_not_touched(settings, mb):
    mb.add_ticket(1, "200001", ["acc 86000013101001 cannot link EcoCash"], topic="Ecocash-Registration")
    mb.add_ticket(2, "200002", ["acc 86000013101001 B2W failed"], topic="FAILED ECOCASH B2W / WB2")
    mb.add_ticket(3, "200003", ["acc 86000013101001 airtime failed"],
                  topic="NMB Connect / USSD / Failed Transactions / Airtime")
    runner, _ = build(settings, mb, ScriptedModel())
    assert sorted(t.number for t in runner.discover(None)) == ["200002", "200003"]


def test_without_claude_a_ticket_with_no_details_is_asked_and_others_wait(settings, mb):
    class Down:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise AgentError("connection refused")

    mb.add_ticket(1, "200001", ["My client's ZIPIT failed, please help"])
    mb.add_ticket(2, "200002", ["acc 86000013101001 ZIPIT failed"], created=now_local() - timedelta(hours=1))
    runner, audit = build(settings, mb, Down(), mode="live")
    runner.agent.decide = lambda *a, **k: (_ for _ in ()).throw(AgentError("connection refused"))
    summary = runner.run()
    assert summary.actions == {"request_info": 1} and summary.errors == 1
    assert audit.last_for_ticket(1)["category"] == "missing_account_number"
