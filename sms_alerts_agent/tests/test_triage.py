"""SMS alerts problems filed under other help topics: triage, what the agent may do, and caching."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from agent.decision import ESCALATE, REPLY_AND_CLOSE, Decision
from agent.guardrails import apply_guardrails
from agent.osticket import ESCALATION_NOTE_MARKER, Ticket, ThreadEntry, TicketState, open_tickets_sql
from agent.pii import Pseudonymizer
from agent.tools import Evidence
from agent.triage import Triage
from tests.conftest import AGENT_STAFF, ScriptedModel, check, decision, response, tool_block
from tests.test_agent_runner import ACCT, CLOSE_REPLY, build

MESSAGE = "Client 0772 123 456 is not receiving debit alerts on account 12345678901, kindly assist"


def verdict(kind: str, confidence: float = 0.95, reason: str = "The client is not receiving debit alerts."):
    return response(tool_block("submit_triage", {"kind": kind, "confidence": confidence, "reason": reason}, "t"))


def with_triage(runner, audit, model, settings, **kw):
    runner.triage = Triage(model, "triage-model", audit.db, agent_staff_id=AGENT_STAFF)
    runner.s = replace(runner.s, scan_other_topics=True, sms_topics=("SMS Alerts",), exclude_topics=("EcoCash",), **kw)
    return runner


def seed_delivered(mb):
    recent = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 10:00:00")
    sent = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 10:03:00")
    mb.add_account(ACCT, triggers=[mb.trigger(recent)])
    mb.socket_rows = [mb.socket_row(sent)]


def test_query_scans_other_topics_and_keeps_tickets_without_one():
    q = open_tickets_sql(["SMS Alerts"], "2026-09-08 00:00:00", ["EcoCash"])
    assert "LEFT JOIN ost_help_topic ht ON" in q and "t.created >= '2026-09-08 00:00:00'" in q
    assert "NOT (UPPER(" in q and "'ECOCASH'" in q and "MAX(CASE WHEN f.name = 'subject'" in q
    only_sms = open_tickets_sql(["SMS Alerts"], None, ["EcoCash"])
    assert "LEFT JOIN ost_help_topic ht ON" not in only_sms and "t.created >=" not in only_sms


def test_sms_issue_under_another_topic_is_handled(settings, mb):
    mb.add_ticket(8, "500100", {}, [MESSAGE], topic="Account Services", subject="Alerts")
    seed_delivered(mb)
    agent_model = ScriptedModel(check("<NUMBER_1>", "debits", phone="<MOBILE_1>"), decision("reply_and_close", CLOSE_REPLY))
    runner, audit = build(settings, mb, agent_model, mode="live")
    triage_model = ScriptedModel(verdict("sms_alerts"))
    with_triage(runner, audit, triage_model, settings)
    summary = runner.run()
    assert summary.actions == {"reply_and_close": 1} and summary.triage == {"picked (sms_alerts)": 1}
    assert mb.tickets[8]["state"] == "closed"
    triage_text = triage_model.calls[0]["messages"][0]["content"]
    assert "Help topic: Account Services" in triage_text and "0772" not in triage_text
    prompt = agent_model.calls[0]["messages"][0]["content"]
    assert "Filed under another help topic" in prompt and "Subject: Alerts" in prompt
    queue = audit.db.execute("SELECT help_topic, state, detail FROM queue_state").fetchone()
    assert queue["help_topic"] == "Account Services" and queue["state"] == "reply_and_close"
    assert "another topic" in queue["detail"]


def test_mixed_ticket_gets_a_note_not_a_reply(settings, mb):
    mb.add_ticket(8, "500100", {}, [MESSAGE + ". Also her card was retained at the ATM."], topic="Cards")
    seed_delivered(mb)
    agent_model = ScriptedModel(check("<NUMBER_1>", "debits", phone="<MOBILE_1>"), decision("reply_and_close", CLOSE_REPLY))
    runner, audit = build(settings, mb, agent_model, mode="live")
    with_triage(runner, audit, ScriptedModel(verdict("mixed")), settings)
    summary = runner.run()
    assert summary.actions == {"escalate": 1} and mb.tickets[8]["state"] == "open"
    note = mb.thread_entries(8)[-1]
    assert note["type"] == "N" and ESCALATION_NOTE_MARKER in note["body"] and "under the help topic &#x27;Cards&#x27;" in note["body"]
    row = audit.last_for_ticket(8)
    assert "also raises something other than SMS alerts" in row["guardrail_notes"]


def test_other_tickets_are_left_alone_and_read_again_only_after_a_new_message(settings, mb):
    mb.add_ticket(8, "500100", {}, ["Client wants a new cheque book"], topic="Account Services")
    runner, audit = build(settings, mb, ScriptedModel())  # the ticket agent is never called
    triage_model = ScriptedModel(verdict("other", reason="Cheque book request."), verdict("other"))
    with_triage(runner, audit, triage_model, settings)
    assert runner.run().triage == {"other": 1}
    assert runner.run().triage == {"other": 1} and len(triage_model.calls) == 1  # cached
    mb.add_entry(8, "N", "internal", staff_id=AGENT_STAFF)  # the agent's own entries do not count
    runner.run()
    assert len(triage_model.calls) == 1
    mb.add_entry(8, "M", "Sorry, she also says SMS alerts stopped")
    runner.run()
    assert len(triage_model.calls) == 2


def test_tickets_staff_are_working_on_excluded_topics_and_old_tickets_cost_nothing(settings, mb):
    mb.add_ticket(8, "500100", {}, [MESSAGE], topic="Account Services")
    mb.add_entry(8, "R", "We are looking into it", staff_id=77)
    mb.add_ticket(9, "500101", {}, [MESSAGE], topic="EcoCash / Linking")
    mb.add_ticket(10, "500102", {}, [MESSAGE], topic="General",
                  created=(datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d 09:00:00"))
    runner, audit = build(settings, mb, ScriptedModel())
    triage_model = ScriptedModel()
    with_triage(runner, audit, triage_model, settings)
    summary = runner.run()
    assert triage_model.calls == [] and summary.triage == {"handled_by_staff": 1} and summary.seen == 0


def test_triage_budget_leaves_the_rest_for_the_next_pass(settings, mb):
    mb.add_ticket(8, "500100", {}, ["Cheque book"], topic="Account Services")
    mb.add_ticket(9, "500101", {}, ["Statement"], topic="Account Services")
    runner, audit = build(settings, mb, ScriptedModel())
    with_triage(runner, audit, ScriptedModel(verdict("other"), verdict("other")), settings, triage_max_per_run=1)
    assert runner.run().triage == {"other": 1, "left for the next pass": 1}
    assert runner.run().triage == {"other": 2}


def test_unsure_triage_is_not_picked(settings, mb):
    mb.add_ticket(8, "500100", {}, ["sms issue?"], topic="General")
    runner, audit = build(settings, mb, ScriptedModel())
    with_triage(runner, audit, ScriptedModel(verdict("sms_alerts", confidence=0.5)), settings)
    assert runner.run().triage == {"unsure": 1}


def test_guardrail_replies_only_on_pure_sms_tickets_from_other_topics():
    p = Pseudonymizer()
    acct = "00012345678901"
    reply = f"The number on account {p.mask(acct)} matches Intellect. Kindly advise the client to check the spam folder."
    ev = Evidence()
    ev.account_results[acct] = {"status": "delivered_check_spam", "reply": "r"}
    t = Ticket(1, "100001", "Account Services", "", None, None, None, None, None, None, thread_id=9,
               entries=[ThreadEntry(10, "M", None, "x", f"acc {acct} no alerts", "")], topic_match=False)
    fresh = TicketState(1, "open", "Open", "Account Services", 10)

    def run(kind):
        t.triage_kind = kind
        return apply_guardrails(Decision(REPLY_AND_CLOSE, 0.95, "x", "r", reply_text=reply), t, fresh, ev, p,
                                confidence_threshold=0.85, closes_so_far=0, max_closes=20, agent_staff_id=525)

    assert run("sms_alerts").final.action == REPLY_AND_CLOSE
    assert run("mixed").final.action == ESCALATE and run(None).final.action == ESCALATE
    moved = apply_guardrails(Decision(REPLY_AND_CLOSE, 0.95, "x", "r", reply_text=reply), t,
                             TicketState(1, "open", "Open", "SMS Alerts", 10), ev, p,
                             confidence_threshold=0.85, closes_so_far=0, max_closes=20)
    assert moved.deferred
