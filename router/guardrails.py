"""Code-enforced rules between the model's decision and any change to the ticket.

The model proposes a topic; this module decides whether the ticket is moved, the topic is only suggested to staff
in a note, or nothing happens. A move needs: a topic that exists and is active, is not a catch-all or excluded,
differs from where the ticket is; enough confidence; nobody (other than the agents) working the ticket; and a target
the router may move to on its own (a topic a desk works, or one in ROUTER_MOVE_TO). A ticket is never moved twice.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from agentcore.guardrails import (  # noqa: F401 - shared reply rules, re-exported
    GuardrailResult,
    reply_problems,
)
from agentcore.osticket import fold_topic
from agentcore.pii import Pseudonymizer
from .decision import ESCALATE, MOVE, NO_ACTION, REPLY_ACTIONS, REQUEST_INFO, RouteDecision
from .osticket import Ticket, TicketState
from .tools import Evidence
from .topics import Catalogue


def apply_guardrails(
    decision: RouteDecision,
    ticket: Ticket,
    fresh: Optional[TicketState],
    evidence: Evidence,
    pseudo: Pseudonymizer,
    catalogue: Catalogue,
    *,
    confidence_threshold: float,
    moves_so_far: int,
    max_moves: int,
    allow_request_info: bool = True,
    check_live_state: bool = True,
    max_info_requests: int = 2,
    agent_staff_id: Optional[int] = None,
) -> GuardrailResult:
    notes: list[str] = []

    def to(action: str, note: str, deferred: bool = False) -> GuardrailResult:
        notes.append(note)
        final = replace(decision, action=action, reply_text=decision.reply_text if action in REPLY_ACTIONS else "")
        return GuardrailResult(decision, final, notes, deferred=deferred)

    # 1. The ticket may have changed while the model was working.
    if check_live_state:
        if fresh is None:
            return to(NO_ACTION, "ticket could not be re-read", deferred=True)
        if not fresh.is_open:
            return to(NO_ACTION, f"ticket is no longer open ({fresh.status_name})", deferred=True)
        if fold_topic(fresh.help_topic) != fold_topic(ticket.help_topic):
            return to(NO_ACTION, f"someone filed it under {fresh.help_topic.strip() or '(no topic)'} meanwhile",
                      deferred=True)
        if fresh.last_entry_id != ticket.last_entry_id:
            return to(NO_ACTION, "new thread activity during analysis; will retry next run", deferred=True)

    if decision.action == REQUEST_INFO:
        if not allow_request_info:
            return to(ESCALATE, "request_info is disabled (ALLOW_REQUEST_INFO=false)")
        asked = ticket.agent_replies(agent_staff_id)
        if asked >= max_info_requests:
            return to(ESCALATE, f"the agent has already replied {asked} time(s) on this ticket; a person should take over")
        problems = reply_problems(decision.reply_text, pseudo)
        if problems:
            return to(ESCALATE, "reply blocked: " + "; ".join(problems))
        if decision.confidence < confidence_threshold:
            return to(ESCALATE, f"confidence {decision.confidence:.2f} is below {confidence_threshold:.2f}")
        return GuardrailResult(decision, decision, notes)

    if decision.action != MOVE:
        return GuardrailResult(decision, decision, notes)

    # 2. The target must be a real topic the ticket may go to.
    target = catalogue.find(decision.target_topic)
    if target is None:
        return to(ESCALATE, f"move blocked: {decision.target_topic!r} is not an active help topic")
    if fold_topic(target.path) == fold_topic(ticket.help_topic):
        return to(NO_ACTION, f"the ticket is already under {target.name}")
    if catalogue.is_catch_all(target.path) or catalogue.is_never_to(target.path):
        return to(ESCALATE, f"move blocked: {target.name} is not a topic tickets are routed to")
    if evidence.check_errors:
        return to(NO_ACTION, "move deferred: a lookup failed during analysis; will retry", deferred=True)

    # 3. Who else is on the ticket, and how sure the model is.
    if ticket.moved_before():
        return to(ESCALATE, "move blocked: the router moved this ticket before and it is back here; a person decides")
    if ticket.assignee_id and ticket.assignee_id != agent_staff_id:
        return to(ESCALATE, "move blocked: the ticket is assigned to a person")
    if ticket.handled_by_staff(agent_staff_id):
        return to(ESCALATE, "move blocked: a person has already replied or left a note")
    if decision.confidence < confidence_threshold:
        return to(ESCALATE, f"confidence {decision.confidence:.2f} is below {confidence_threshold:.2f}")
    if not catalogue.may_move_to(target):
        return to(ESCALATE, f"move blocked: {target.name} is not worked by an agent and not in ROUTER_MOVE_TO; "
                            "suggested to staff instead")

    # 4. Rate limit.
    if moves_so_far >= max_moves:
        return to(NO_ACTION, f"move limit of {max_moves} per run reached", deferred=True)
    final = replace(decision, target_topic=target.name)
    return GuardrailResult(decision, final, notes)
