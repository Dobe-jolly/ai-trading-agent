"""The router end to end with a scripted model: which tickets it reads, the rules about moves, and what it writes.
All data invented."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from agentcore.audit import AuditLog
from agentcore.loop import AgentError
from agentcore.pii import Pseudonymizer
from agentcore.writer import OsTicketWriter, ShadowWriter
from router import topics
from router.decision import RouteDecision
from router.guardrails import apply_guardrails
from router.loop import TicketAgent
from router.normalize import now_local
from router.notify import Notifier
from router.osticket import ESCALATION_NOTE_MARKER, MOVE_NOTE_MARKER, OsTicket
from router.runner import Router
from router.tools import Evidence, Toolbox
from tests.router.conftest import AGENT_STAFF, OWNERS, TICKET_DB, ScriptedModel, call, route

ZIPIT = "Client acc 86000013101001 sent a ZIPIT of $50 yesterday to CBZ and the money has not arrived"


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
    store = OsTicket(mb, TICKET_DB, topics=s.topics, include_no_topic=s.include_no_topic,
                     exclude_topics=s.exclude_topics, days=s.days)
    catalogue = topics.load(mb, TICKET_DB, descriptions=topics.load_descriptions(s.topic_descriptions_file),
                            owners=OWNERS, catch_alls=s.topics, never_to=s.never_to, move_to=s.move_to)
    toolbox = Toolbox(store, catalogue)
    agent = TicketAgent(model, "test-model", "system prompt", toolbox, max_turns=6, agent_staff_id=AGENT_STAFF)
    writer = OsTicketWriter(store, AGENT_STAFF, "Routing Agent") if mode != "shadow" else ShadowWriter()
    audit = AuditLog(s.state_db)
    notifier = Notifier(audit, s, emails) if emails is not None else None
    return Router(s, store, agent, toolbox, writer, audit, mode, notifier, catalogue=catalogue), audit


SIMILAR = call("find_similar_closed_tickets", {"words": ["zipit"]}, "s")
MOVE_ZIPIT = route("move", "Failed ZIPIT Transaction")


# ---- which tickets ---------------------------------------------------------------------------------
def test_reads_catch_alls_and_tickets_without_a_topic_only(settings, mb):
    mb.add_ticket(1, "200001", [ZIPIT])
    mb.add_ticket(2, "200002", [ZIPIT], topic=None)
    mb.add_ticket(3, "200003", [ZIPIT], topic="Failed ZIPIT Transaction")
    mb.add_ticket(4, "200004", [ZIPIT], created=now_local() - timedelta(days=45))  # older than ROUTER_DAYS
    mb.add_ticket(5, "200005", [ZIPIT], state="closed")
    runner, _ = build(settings, mb, ScriptedModel())
    assert sorted(t.number for t in runner.discover(None)) == ["200001", "200002"]
    runner, _ = build(settings, mb, ScriptedModel(), include_no_topic=False)
    assert [t.number for t in runner.discover(None)] == ["200001"]


# ---- moves -------------------------------------------------------------------------------------------
def test_shadow_decides_and_logs_without_writing(settings, mb):
    mb.add_ticket(1, "200001", [ZIPIT])
    mb.add_closed(11, "100011", "ZIPIT not received", "Failed ZIPIT Transaction")
    model = ScriptedModel(SIMILAR, MOVE_ZIPIT)
    runner, audit = build(settings, mb, model)
    summary = runner.run()
    assert summary.actions == {"move": 1} and not mb.writes
    row = audit.last_for_ticket(1)
    assert row["final_action"] == "move" and row["category"] == "desk_topic"
    assert json.loads(row["account_statuses"]) == {"topic": "Failed ZIPIT Transaction"}
    facts = json.loads(row["facts"])["route"]
    assert facts["from"] == "HelpDesk / Logging Ticket" and facts["desk"] == "Failed transactions"
    prompt = model.calls[0]["messages"][0]["content"]
    assert "86000013101001" not in prompt and "Filed under: HelpDesk / Logging Ticket" in prompt


def test_live_move_files_the_ticket_and_leaves_a_note(settings, mb):
    mb.add_ticket(1, "200001", [ZIPIT])
    runner, audit = build(settings, mb, ScriptedModel(MOVE_ZIPIT), mode="live")
    runner.run()
    assert mb.tickets[1]["topic_id"] == 155 and mb.tickets[1]["help_topic"] == "Failed ZIPIT Transaction"
    note = mb.thread_entries(1)[-1]
    assert note["type"] == "N" and MOVE_NOTE_MARKER in note["body"] and "Failed transactions agent" in note["body"]
    assert json.loads(audit.last_for_ticket(1)["executed"]) == ["moved", "note"]
    assert mb.closed == []  # never closes


def test_a_ticket_moved_back_by_a_person_is_left_alone(settings, mb):
    mb.add_ticket(1, "200001", [ZIPIT])
    runner, audit = build(settings, mb, ScriptedModel(MOVE_ZIPIT), mode="live")
    runner.run()
    mb.tickets[1].update(topic_id=122, help_topic="HelpDesk / Logging Ticket")  # a person files it back
    runner, _ = build(settings, mb, ScriptedModel(), mode="live")
    assert runner.run().skipped == {"moved before; a person filed it back here": 1}


def test_notes_mode_recommends_without_moving(settings, mb):
    mb.add_ticket(1, "200001", [ZIPIT])
    runner, _ = build(settings, mb, ScriptedModel(MOVE_ZIPIT), mode="notes")
    runner.run()
    note = mb.thread_entries(1)[-1]
    assert mb.tickets[1]["topic_id"] == 122 and "Recommended: file this ticket under Failed ZIPIT Transaction" in note["body"]


def test_a_topic_no_agent_works_is_suggested_not_moved(settings, mb):
    mb.add_ticket(1, "200001", ["Client gets no OTP on the NMB Connect app"])
    runner, audit = build(settings, mb, ScriptedModel(route("move", "NMB Connect / OTP")), mode="live")
    runner.run()
    note = mb.thread_entries(1)[-1]
    assert mb.tickets[1]["topic_id"] == 122 and ESCALATION_NOTE_MARKER in note["body"]
    assert "change the help topic to NMB Connect / OTP (the routing agent moves tickets on its own only" in note["body"]
    row = audit.last_for_ticket(1)
    # a suggestion by design, not a safety stop: the console does not count it as one
    assert row["proposed_action"] == row["final_action"] == "escalate" and row["category"] == "staff_topic"
    assert json.loads(row["guardrail_notes"]) == []


def test_a_person_handling_the_ticket_is_not_interrupted(settings, mb):
    mb.add_ticket(1, "200001", [ZIPIT])
    mb.add_entry(1, "N", "Looking into it", staff_id=77)
    runner, _ = build(settings, mb, ScriptedModel(), mode="live")
    assert runner.run().skipped == {"a person is handling it": 1}


def test_request_info_asks_the_branch_and_emails_them(settings, mb):
    mb.add_ticket(1, "200001", ["Please assist"], subject="Urgent")
    ask = ("Kindly describe the problem: what the client was trying to do, on which channel, and the error they saw.")
    emails = RecordingTransport()
    runner, audit = build(settings, mb, ScriptedModel(route("request_info", reply=ask, standard=True)), mode="live",
                          emails=emails, notify_transport="gateway", notif_email_url="http://gw.test/email")
    runner.run()
    reply = mb.thread_entries(1)[-1]
    assert reply["type"] == "R" and "Kindly describe the problem" in reply["body"]
    assert emails.sent[0]["subject"] == "[#200001] Ticket: please describe the problem"
    assert audit.last_for_ticket(1)["category"] == "no_description"


def test_without_claude_an_undescribed_ticket_is_asked_and_others_wait(settings, mb):
    class Down:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise AgentError("connection refused")

    mb.add_ticket(1, "200001", ["Please assist"])
    mb.add_ticket(2, "200002", [ZIPIT])
    runner, audit = build(settings, mb, Down(), mode="live")
    runner.agent.decide = lambda *a, **k: (_ for _ in ()).throw(AgentError("connection refused"))
    summary = runner.run()
    assert summary.actions == {"request_info": 1} and summary.errors == 1
    assert mb.tickets[2]["topic_id"] == 122


def test_the_move_budget_holds_the_rest_for_the_next_pass(settings, mb):
    for i in (1, 2, 3):
        mb.add_ticket(i, f"20000{i}", [ZIPIT])
    runner, _ = build(settings, mb, ScriptedModel(MOVE_ZIPIT, MOVE_ZIPIT, MOVE_ZIPIT), mode="live", max_moves_per_run=2)
    summary = runner.run()
    assert summary.actions == {"move": 2, "no_action": 1}
    assert sorted(t["topic_id"] for t in mb.tickets.values()) == [122, 155, 155]


# ---- the rules, one at a time ------------------------------------------------------------------------
def guarded(settings, mb, decision, **ticket_kw):
    mb.add_ticket(1, "200001", [ZIPIT], **ticket_kw)
    runner, _ = build(settings, mb, ScriptedModel())
    ticket = runner.discover(None)[0]
    mb_store = runner.store
    mb_store.load_thread(ticket)
    pseudo = Pseudonymizer()
    pseudo.mask(ticket.all_text())
    return apply_guardrails(decision, ticket, mb_store.state(ticket), Evidence(), pseudo, runner.catalogue,
                            confidence_threshold=0.85, moves_so_far=0, max_moves=20, agent_staff_id=AGENT_STAFF)


def move(target, conf=0.95):
    return RouteDecision("move", conf, "x", "reason", target_topic=target)


@pytest.mark.parametrize("target,conf,ticket_kw,action,note", [
    ("failed zipit transaction", 0.95, {}, "move", None),
    ("Payroll", 0.95, {}, "escalate", "not an active help topic"),
    ("HelpDesk / Logging Ticket", 0.95, {}, "no_action", "already under"),
    ("HelpDesk / Login", 0.95, {"topic": None}, "escalate", "not worked by an agent"),
    ("Escalated to Vendor", 0.95, {}, "escalate", "not a topic tickets are routed to"),
    ("Failed ZIPIT Transaction", 0.6, {}, "escalate", "below 0.85"),
    ("Failed ZIPIT Transaction", 0.95, {"assignee": 77}, "escalate", "assigned to a person"),
])
def test_move_rules(settings, mb, target, conf, ticket_kw, action, note):
    g = guarded(settings, mb, move(target, conf), **ticket_kw)
    assert g.final.action == action
    if note:
        assert note in g.notes[0]
    else:
        assert g.final.target_topic == "Failed ZIPIT Transaction"


def test_a_ticket_refiled_meanwhile_is_left_for_the_next_pass(settings, mb):
    mb.add_ticket(1, "200001", [ZIPIT])
    runner, _ = build(settings, mb, ScriptedModel())
    ticket = runner.discover(None)[0]
    runner.store.load_thread(ticket)
    mb.tickets[1].update(topic_id=155, help_topic="Failed ZIPIT Transaction")
    g = apply_guardrails(move("Failed ZIPIT Transaction"), ticket, runner.store.state(ticket), Evidence(),
                         Pseudonymizer(), runner.catalogue, confidence_threshold=0.85, moves_so_far=0, max_moves=20)
    assert g.final.action == "no_action" and g.deferred and "meanwhile" in g.notes[0]


def test_move_needs_a_target():
    with pytest.raises(ValueError, match="target_topic is required"):
        RouteDecision.from_tool_input({"action": "move", "confidence": 0.9, "category": "x", "reason": "r"})
    with pytest.raises(ValueError, match="action must be one of"):
        RouteDecision.from_tool_input({"action": "reply_and_close", "confidence": 0.9, "category": "x", "reason": "r"})


def test_asking_about_a_ticket_that_describes_the_problem_waits_for_a_person(settings, mb):
    mb.add_ticket(1, "200001", [ZIPIT])
    ask = "Kindly confirm the date of the ZIPIT and the receiving bank."
    runner, _ = build(settings, mb, ScriptedModel(route("request_info", reply=ask, standard=True)), mode="live")
    assert runner.run().actions == {"awaiting approval": 1} and not mb.writes
    from agentcore.approval import ApprovalStore

    why = ApprovalStore(runner.audit.db).open_for(1)["why"]
    assert "already describes the problem, so the standard outcome is move, not request info" in why
