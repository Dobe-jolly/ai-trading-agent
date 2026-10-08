"""End to end: scripted model + fake Metabase through the real loop, guardrails, runner and writes."""
from __future__ import annotations

import json
from dataclasses import replace

import anthropic
import httpx

from agent.audit import AuditLog
from agent.delivery import SocketDeliveryChecker
from agent.loop import TicketAgent
from agent.osticket import ESCALATION_NOTE_MARKER, OsTicket
from agent.runner import Runner
from agent.sms_checks import Databases, SmsChecks
from agent.tools import Toolbox
from agent.writer import OsTicketWriter, ShadowWriter
from tests.conftest import (
    AGENT_STAFF, CUSTOMERS_DB, DISPATCH_DB, SOCKET_DB, STANDBY_DB, TICKET_DB, ScriptedModel, check, decision,
    response, text_block, tool_block,
)

ACCT = "00012345678901"
CLOSE_REPLY = ("The number on the ticket matches Intellect and the SMS gateway confirmed the alerts for account "
               "<NUMBER_1> were delivered. Kindly advise the client to check the SMS spam or blocked folder.")


def build(settings, mb, model, mode="shadow", fake_today=None):
    socket = SocketDeliveryChecker(mb, window_minutes=60, dispatch_lag_minutes=1080)
    checks = SmsChecks(mb, Databases(STANDBY_DB, CUSTOMERS_DB, SOCKET_DB, DISPATCH_DB), socket)
    toolbox = Toolbox(checks)
    agent = TicketAgent(model, "test-model", "system prompt", toolbox, max_turns=6, agent_staff_id=AGENT_STAFF)
    store = OsTicket(mb, TICKET_DB)
    writer = OsTicketWriter(store, AGENT_STAFF, "SMS Agent") if mode != "shadow" else ShadowWriter()
    audit = AuditLog(settings.state_db)
    return Runner(replace(settings, mode=mode), store, agent, toolbox, writer, audit, mode), audit


def seed(mb, phone="0772 123 456", account="12345678901", messages=("Client not receiving debit alerts",)):
    mb.add_ticket(7, "482913", {"phone_number": phone, "account_number": account, "currency": "USD",
                                "type": "Debits"}, list(messages))
    from datetime import datetime, timedelta
    recent = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 10:00:00")
    sent = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 10:03:00")
    mb.add_account(ACCT, triggers=[mb.trigger(recent)])
    mb.socket_rows = [mb.socket_row(sent)]


def test_shadow_run_masks_everything_and_writes_nothing(settings, mb):
    seed(mb)
    model = ScriptedModel(check(), decision("reply_and_close", CLOSE_REPLY))
    runner, audit = build(settings, mb, model)
    summary = runner.run()
    assert summary.actions == {"reply_and_close": 1} and mb.writes == []
    seen = json.dumps([c["messages"] for c in model.calls])
    assert "12345678901" not in seen and "0772" not in seen and "263772123456" not in seen
    assert "<NUMBER_1>" in seen and "<MOBILE_1>" in seen
    # The check ran on the real account.
    assert any("IACLINK_ACTUAL_ACNUM = '00012345678901'" in sql for _, sql in mb.sql_log)
    row = audit.last_for_ticket(7)
    assert json.loads(row["account_statuses"]) == {"<NUMBER_1>": "delivered_check_spam"}
    assert "12345678901" not in row["transcript"] and "12345678901" not in (row["template_reply"] or "")


def test_live_close_posts_unmasked_reply_and_closes(settings, mb):
    seed(mb)
    runner, _ = build(settings, mb, ScriptedModel(check(), decision("reply_and_close", CLOSE_REPLY)), mode="live")
    runner.run()
    insert = mb.writes[0]
    assert "'R'" in insert and "00012345678901" in insert and "<NUMBER_1>" not in insert
    assert mb.tickets[7]["state"] == "closed" and mb.tickets[7]["staff_id"] == AGENT_STAFF


def test_model_cannot_close_a_gateway_failure(settings, mb):
    seed(mb)
    mb.socket_rows = [dict(mb.socket_rows[0], external_status="FAILED_TO_SEND")]
    runner, audit = build(settings, mb, ScriptedModel(check(), decision("reply_and_close", CLOSE_REPLY)), mode="live")
    runner.run()
    assert mb.tickets[7]["state"] == "open"
    note = mb.thread_entries(7)[-1]
    assert note["type"] == "N" and ESCALATION_NOTE_MARKER in note["body"] and "socket_failed" in note["body"]
    assert "00012345678901" in note["body"]  # internal note shows staff the real account
    # Next run: already escalated, the model is not called.
    runner2, _ = build(settings, mb, ScriptedModel(), mode="live")
    assert runner2.run().skipped == {"already escalated": 1}


def test_wrong_phone_then_corrected_on_the_thread(settings, mb):
    """The user's scenario: invalid phone -> agent asks -> owner replies with the right number -> agent closes."""
    seed(mb, phone="07721234")  # too short; masked as <NUMBER_1>, so the account is <NUMBER_2>
    # Run 1: the check says invalid_ticket_phone; the model asks for the number.
    ask = ("The phone number on the ticket (<NUMBER_1>) is not a valid mobile number. "
           "Kindly reply with the client's correct mobile number so we can check the alerts.")
    model1 = ScriptedModel(check(account="<NUMBER_2>"), decision("request_info", ask))
    runner, audit = build(settings, mb, model1, mode="live")
    runner.run()
    assert mb.tickets[7]["state"] == "open" and mb.thread_entries(7)[-1]["type"] == "R"
    assert json.loads(audit.last_for_ticket(7)["account_statuses"]) == {"<NUMBER_2>": "invalid_ticket_phone"}
    assert "07721234" in mb.thread_entries(7)[-1]["body"]  # token filled in with the real value

    # Run 2: waiting on the owner; nothing happens.
    runner2, _ = build(settings, mb, ScriptedModel(), mode="live")
    assert runner2.run().skipped == {"waiting on ticket owner": 1}

    # The owner answers on the thread.
    mb.add_entry(7, "M", "Apologies, the correct number is 0772 123 456")

    # Run 3: the model reads the whole thread and re-checks with the corrected number.
    model3 = ScriptedModel(check(account="<NUMBER_2>", phone="<MOBILE_1>"),
                           decision("reply_and_close", CLOSE_REPLY.replace("<NUMBER_1>", "<NUMBER_2>")))
    runner3, _ = build(settings, mb, model3, mode="live")
    runner3.run()
    first_prompt = model3.calls[0]["messages"][0]["content"]
    assert "posted by the SMS agent" in first_prompt and "<MOBILE_1>" in first_prompt
    assert mb.tickets[7]["state"] == "closed"


def test_corrected_account_from_thread_is_used(settings, mb):
    seed(mb, account="", messages=("Client has no alerts on acc 12345678901, cell 0772123456",))
    model = ScriptedModel(check(account="<NUMBER_1>"), decision("reply_and_close", CLOSE_REPLY))
    runner, _ = build(settings, mb, model, mode="live")
    runner.run()
    assert mb.tickets[7]["state"] == "closed"


def test_rules_fallback_when_model_is_down(settings, mb):
    seed(mb)

    class Down(ScriptedModel):
        def create(self, **kwargs):
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.test"))

    runner, audit = build(settings, mb, Down(), mode="live")
    runner.run()
    assert mb.tickets[7]["state"] == "closed"
    assert "SMS gateway confirmed" in mb.thread_entries(7)[-1]["body"]
    assert audit.last_for_ticket(7)["category"] == "rules_fallback"


def test_no_fallback_leaves_ticket_for_retry(settings, mb):
    seed(mb)

    class Down(ScriptedModel):
        def create(self, **kwargs):
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.test"))

    runner, audit = build(replace(settings, model_fallback="none"), mb, Down(), mode="live")
    summary = runner.run()
    assert summary.errors == 1 and mb.writes == [] and audit.last_for_ticket(7) is None


def test_template_reply_source_posts_java_wording(settings, mb):
    seed(mb)
    runner, _ = build(replace(settings, reply_source="template"), mb,
                      ScriptedModel(check(), decision("reply_and_close", CLOSE_REPLY)), mode="live")
    runner.run()
    assert "Kindly advise the client to check the SMS spam folder or blocked SMS folder, and to confirm" in mb.writes[0]


def test_invalid_decision_then_nudge_and_turn_limit(settings, mb):
    seed(mb)
    model = ScriptedModel(
        response(tool_block("submit_decision", {"action": "close_it", "confidence": 1, "category": "x",
                                                "reason": "r", "evidence": []}, "a")),
        response(text_block("thinking")),
        decision("escalate"),
    )
    runner, audit = build(settings, mb, model)
    runner.run()
    assert "Invalid decision" in model.calls[1]["messages"][-1]["content"][0]["content"]
    assert audit.last_for_ticket(7)["final_action"] == "escalate"


def test_unchanged_ticket_is_skipped_and_new_message_reopens(settings, mb):
    seed(mb)
    runner, _ = build(settings, mb, ScriptedModel(check(), decision("escalate")))
    runner.run()
    runner2, _ = build(settings, mb, ScriptedModel())
    assert runner2.run().skipped == {"unchanged since last decision": 1}
    mb.add_entry(7, "M", "Still nothing")
    runner3, _ = build(settings, mb, ScriptedModel(check(), decision("escalate")))
    assert runner3.run().decided == 1


def test_kill_switch(settings, mb):
    seed(mb)
    settings.kill_switch_file.touch()
    runner, _ = build(settings, mb, ScriptedModel(), mode="live")
    assert runner.run().decided == 0 and mb.writes == []


def test_closed_ticket_audit_replay(settings, mb, tmp_path):
    seed(mb)
    mb.tickets[7].update(state="closed", closed="2026-10-07 12:00:00", status_id=3)
    mb.add_entry(7, "R", "The number on the ticket matches the number maintained on Intellect.", staff_id=AGENT_STAFF)
    model = ScriptedModel(check(), decision("reply_and_close", CLOSE_REPLY))
    runner, audit = build(settings, mb, model)
    out = runner.audit_closed(__import__("datetime").date(2026, 10, 1), 10, True, tmp_path)
    prompt = model.calls[0]["messages"][0]["content"]
    assert "matches the number maintained" not in prompt  # the agent only sees what came before the desk replied
    rows = out.read_text().splitlines()
    assert len(rows) == 2 and "reply_and_close" in rows[1] and "matches the number maintained on Intellect" in rows[1]
    assert mb.writes == []
