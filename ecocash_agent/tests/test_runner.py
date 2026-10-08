"""Whole passes over EcoCash tickets, against the fake Metabase and a scripted model. All data invented."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

from agentcore.audit import AuditLog
from agent.ecocash import AttemptStore, BankAccounts, LinkRecords
from agent.loop import TicketAgent
from agent.normalize import now_local
from agent.notify import Notifier
from agent.osticket import ESCALATION_NOTE_MARKER, OsTicket
from agent.runner import Runner
from agent.tools import Toolbox
from agentcore.writer import OsTicketWriter, ShadowWriter
from tests.conftest import (ACCOUNT, AGENT_STAFF, ECOCASH_DB, MOBILE, POSTILION_DB, STANDBY_DB, TICKET_DB,
                            ScriptedModel, decision, find, response, tool_block)


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
    store = OsTicket(mb, TICKET_DB, topics=s.ecocash_topics, keywords=s.ecocash_keywords,
                     exclude_topics=s.ecocash_exclude_topics)
    toolbox = Toolbox(AttemptStore(mb, ECOCASH_DB), LinkRecords(mb, POSTILION_DB), BankAccounts(mb, STANDBY_DB))
    agent = TicketAgent(model, "test-model", "system prompt", toolbox, max_turns=6, agent_staff_id=AGENT_STAFF)
    writer = OsTicketWriter(store, AGENT_STAFF, "EcoCash Agent") if mode != "shadow" else ShadowWriter()
    audit = AuditLog(s.state_db)
    notifier = Notifier(audit, s, emails) if emails is not None else None
    return Runner(s, store, agent, toolbox, writer, audit, mode, notifier), audit


CLOSED_REPLY = ("EcoCash rejected the link because the client's EcoCash wallet on <MOBILE_1> is closed. Kindly advise "
                "the client to visit an EcoCash shop to reactivate the wallet, then link the account again.")
DELINK_REPLY = ("EcoCash rejected the link because account <NUMBER_1> is already linked to an EcoCash wallet. Kindly delink "
                "the account on the Universal dashboard, then ask the client to link it again.")


def test_shadow_run_masks_everything_and_writes_nothing(settings, mb):
    mb.add_ticket(1, "100001", ["Client 0771234567 cannot link, acc 86000013101001"])
    mb.add_attempt("wallet_closed")
    model = ScriptedModel(find(), decision("reply_and_close", CLOSED_REPLY, category="wallet_closed"))
    runner, audit = build(settings, mb, model)
    summary = runner.run()
    assert summary.actions == {"reply_and_close": 1} and not mb.writes
    prompt = model.calls[0]["messages"][0]["content"]
    assert "<MOBILE_1>" in prompt and "<NUMBER_1>" in prompt and "0771234567" not in prompt and "86000013101001" not in prompt
    assert "Details the ticket owner gave" in prompt and "Help topic: EcoCash" in prompt
    tool_result = model.calls[1]["messages"][-1]["content"][0]["content"]
    assert "wallet_closed" in tool_result and "771234567" not in tool_result and "TENDAI" not in tool_result
    row = audit.last_for_ticket(1)
    raw = json.dumps(dict(row))
    assert row["final_action"] == "reply_and_close" and "771234567" not in raw and "86000013101001" not in raw
    assert json.loads(row["account_statuses"]) == {"latest": "wallet_closed"}
    assert json.loads(row["facts"])["latest"]["via"] == "client on USSD"


def test_live_close_posts_the_real_numbers_and_emails_the_branch(settings, mb):
    mb.add_ticket(1, "100001", ["Client 0771234567 cannot link"])
    mb.add_attempt("wallet_closed")
    emails = RecordingTransport()
    model = ScriptedModel(find(), decision("reply_and_close", CLOSED_REPLY, category="wallet_closed"))
    runner, audit = build(settings, mb, model, mode="live", emails=emails)
    runner.run()
    reply = mb.thread_entries(1)[-1]
    assert reply["type"] == "R" and "0771234567" in reply["body"] and mb.closed == ["100001"]
    assert len(emails.sent) == 1 and "[#100001] EcoCash ticket resolved" == emails.sent[0]["subject"]


def test_already_linked_asks_the_branch_to_delink_and_keeps_it_open(settings, mb):
    mb.add_ticket(1, "100001", ["Client 0771234567 cannot link"])
    mb.add_attempt("already_linked")
    model = ScriptedModel(find(), decision("reply_and_close", DELINK_REPLY, category="already_linked"))
    runner, audit = build(settings, mb, model, mode="live")
    runner.run()
    row = audit.last_for_ticket(1)
    assert row["proposed_action"] == "reply_and_close" and row["final_action"] == "request_info"
    assert mb.closed == [] and "Universal dashboard" in mb.thread_entries(1)[-1]["body"]
    # Next pass: waiting on the branch, so nothing happens and the model is not called.
    runner2, _ = build(settings, mb, ScriptedModel(), mode="live")
    assert runner2.run().skipped == {"waiting on ticket owner": 1}


def test_delink_then_link_confirmed_by_the_attempts_closes(settings, mb):
    mb.add_ticket(1, "100001", ["Client 0771234567 cannot link"], created=now_local() - timedelta(days=1))
    mb.add_entry(1, "R", DELINK_REPLY.replace("<NUMBER_1>", ACCOUNT), staff_id=AGENT_STAFF,
                 created=now_local() - timedelta(hours=20))
    mb.add_entry(1, "M", "Done, delinked and the client has tried again", created=now_local() - timedelta(hours=1))
    mb.add_attempt("already_linked", now_local() - timedelta(hours=22))
    mb.add_attempt("unlinked", now_local() - timedelta(hours=3), operation="UNLINK", via="VBS")
    mb.add_attempt("linked", now_local() - timedelta(hours=2))
    reply = "Account <NUMBER_1> was linked to EcoCash on <MOBILE_1> successfully today. The client can now use it."
    model = ScriptedModel(find(), decision("reply_and_close", reply, category="linked"))
    runner, audit = build(settings, mb, model, mode="live")
    runner.run()
    assert mb.closed == ["100001"] and audit.last_for_ticket(1)["final_action"] == "reply_and_close"
    result = json.loads(model.calls[1]["messages"][-1]["content"][0]["content"])
    assert result["summary"]["status"] == "linked" and [a["outcome"] for a in result["attempts"]] == [
        "linked", "unlinked", "failed"]


def test_escalation_note_lists_the_attempts_and_the_next_step(settings, mb):
    mb.add_ticket(1, "100001", ["Client 0771234567 cannot delink"])
    mb.add_attempt("account_rejected", operation="UNLINK", via="VBS")
    model = ScriptedModel(find(), decision("escalate", category="account_rejected"))
    runner, _ = build(settings, mb, model, mode="live")
    runner.run()
    note = mb.thread_entries(1)[-1]
    assert note["type"] == "N" and note["body"].startswith("<p>" + ESCALATION_NOTE_MARKER)
    assert "Account number rejected by EcoCash (EcoCash TC30174)" in note["body"] and "263771234567" in note["body"]
    assert "Confirm with EcoCash which account the wallet is linked to" in note["body"]


def test_ticket_found_by_content_without_an_attempt_costs_no_model_call(settings, mb):
    mb.add_ticket(2, "100002", ["My ecocash statement is wrong, 0771234567"], topic="General / Other", subject="Statement")
    runner, audit = build(settings, mb, ScriptedModel())
    summary = runner.run()
    assert summary.skipped == {"not an EcoCash link case": 1}
    assert audit.last_for_ticket(2)["category"] == "not_ecocash_case"
    runner2, _ = build(settings, mb, ScriptedModel())
    assert runner2.run().skipped == {"unchanged since last decision": 1}  # looked at again only when it changes


def test_ticket_found_by_content_with_an_attempt_is_handled(settings, mb):
    mb.add_ticket(2, "100002", ["Client's ecocash link fails, 0771234567"], topic="General / Other", subject="Link issue")
    mb.add_attempt("number_not_ecocash")
    reply = "EcoCash rejected the link because <MOBILE_1> is not registered on EcoCash. Kindly advise the client to register."
    model = ScriptedModel(find(), decision("reply_and_close", reply))
    runner, _ = build(settings, mb, model)
    assert runner.run().actions == {"reply_and_close": 1}
    assert "picked up because the subject or a message mentions EcoCash" in model.calls[0]["messages"][0]["content"]


def test_lookup_with_a_number_the_owner_never_gave_cannot_reply(settings, mb):
    mb.add_ticket(1, "100001", ["Client 0771234567 cannot link"])
    mb.add_attempt("wallet_closed", mobile="263782000111")
    model = ScriptedModel(response(tool_block("find_ecocash_attempts", {"mobile": "0782000111"}, "f")),
                          decision("reply_and_close", CLOSED_REPLY))
    runner, audit = build(settings, mb, model)
    runner.run()
    row = audit.last_for_ticket(1)
    assert row["final_action"] == "escalate" and "not given on the ticket" in row["guardrail_notes"]


def test_rules_fallback_when_the_model_is_down(settings, mb):
    import anthropic
    import httpx

    class Down:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))

    mb.add_ticket(1, "100001", ["Client 0771234567 cannot link"])
    mb.add_attempt("wallet_closed")
    runner, audit = build(settings, mb, Down(), mode="live")
    runner.run()
    row = audit.last_for_ticket(1)
    assert row["category"] == "rules_fallback" and row["final_action"] == "reply_and_close"
    assert "visit an EcoCash shop" in mb.thread_entries(1)[-1]["body"] and mb.closed == ["100001"]


def test_request_expired_is_escalated_without_the_model(settings, mb):
    mb.add_ticket(1, "100001", ["Client 0771234567 cannot link"], created=now_local() - timedelta(days=5))
    mb.add_entry(1, "R", "Kindly delink the account first.", staff_id=AGENT_STAFF, created=now_local() - timedelta(days=4))
    runner, audit = build(settings, mb, ScriptedModel(), mode="live")
    assert runner.run().actions == {"escalate": 1}
    assert audit.last_for_ticket(1)["category"] == "request_info_timeout"
    assert mb.thread_entries(1)[-1]["type"] == "N"


def test_closed_ticket_replay_compares_with_what_staff_posted(settings, mb, tmp_path):
    mb.add_ticket(5, "100005", ["Client 0771234567 cannot link"], state="closed", created=now_local() - timedelta(days=2))
    mb.add_entry(5, "R", "The wallet is closed, please visit an EcoCash shop.", staff_id=12)
    mb.add_attempt("wallet_closed", now_local() - timedelta(days=2, hours=1))
    model = ScriptedModel(find(), decision("reply_and_close", CLOSED_REPLY, category="wallet_closed"))
    runner, audit = build(settings, mb, model)
    out = runner.audit_closed(now_local().date() - timedelta(days=7), None, False, tmp_path)
    rows = out.read_text().splitlines()
    assert rows[0].startswith("ticket_num,closed_on,help_topic,status,agent_action") and "wallet_closed" in rows[1]
    assert "visit an EcoCash shop" in rows[1]  # the staff reply, for comparison
    prompt = model.calls[0]["messages"][0]["content"]
    assert "please visit an EcoCash shop" not in prompt  # the agent only sees what came before the desk answered


def test_lookup_and_check_commands(settings, mb, monkeypatch, capsys, tmp_path):
    from agent import __main__ as cli

    mb.add_attempt("already_linked")
    mb.add_ticket(1, "100001", ["Client 0771234567 cannot link"])
    monkeypatch.setattr(cli, "build_metabase", lambda s: mb)
    monkeypatch.setenv("METABASE_API_KEY", "k")
    env = ["--env-file", str(tmp_path / "none.env")]
    assert cli.main(env + ["lookup", "--mobile", "0771234567"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "already_linked" and out["suggested_action"] == "request_info"
    assert "TENDAI" not in json.dumps(out) and out["bank_link_record"] == []
    cli.main(env + ["check"])
    text = capsys.readouterr().out
    assert "[osticket] 1 open EcoCash ticket(s): 1 by topic, 0 by content" in text
    assert "[ecocash] ecocash_link_attempts: recording since" in text and "[postilion] eftc_ecocash readable" in text
