"""Code-enforced rules between the model's decision and any write to osTicket.

The model proposes; this module decides what is allowed, from what the lookups recorded (not from what the model says
they found). The core rule: a ticket only closes on an assessment of the client on the ticket whose outcome is final
(not registered, registered with the right number, an inactive account, a re-registration that happened, a failed
registration with a known reason the client can fix). When the branch must act first (delete the profile, update the
number), the ticket stays open and is checked again when they reply. Duplicate profiles, a number on someone else's
profile, locked profiles and failing OTP delivery always go to a person.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Optional

from agentcore.guardrails import (  # noqa: F401 - shared reply rules, re-exported
    GuardrailResult,
    has_raw_long_number,
    reply_problems,
)
from agentcore.pii import Pseudonymizer
from .decision import ESCALATE, NO_ACTION, REPLY_ACTIONS, REPLY_AND_CLOSE, REQUEST_INFO, Decision
from .osticket import Ticket, TicketState, topic_matches
from .tools import CLOSABLE, DELETE_OK, KEEP_OPEN, Evidence

SAYS_NOT_REGISTERED = re.compile(r"(?i)\b(not registered|no (?:NMB Connect )?profile|is not on NMB Connect|"
                                 r"isn't registered|has not registered|never registered)\b")
SAYS_DELETE = re.compile(r"(?i)\b(delete|remove)\b[^.]{0,40}\bprofile\b")
SAYS_REGISTERED_OK = re.compile(r"(?i)\b(is registered|already registered|has a profile|registered (?:on|with))\b")


def apply_guardrails(
    decision: Decision,
    ticket: Ticket,
    fresh: Optional[TicketState],
    evidence: Evidence,
    pseudo: Pseudonymizer,
    *,
    confidence_threshold: float,
    closes_so_far: int,
    max_closes: int,
    allow_request_info: bool = True,
    check_live_state: bool = True,
    max_info_requests: int = 2,
    agent_staff_id: Optional[int] = None,
    topics: Optional[list[str]] = None,
    exclude_topics: Optional[list[str]] = None,
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
        if topics and (not topic_matches(fresh.help_topic, topics)
                       or (exclude_topics and topic_matches(fresh.help_topic, exclude_topics))):
            return to(NO_ACTION, f"help topic is now {fresh.help_topic!r}", deferred=True)
        if fresh.last_entry_id != ticket.last_entry_id:
            return to(NO_ACTION, "new thread activity during analysis; will retry next run", deferred=True)

    if decision.action not in REPLY_ACTIONS:
        return GuardrailResult(decision, decision, notes)

    # 2. The model tried to look at someone else.
    if evidence.refused:
        return to(ESCALATE, "reply blocked: the agent tried to look up "
                            f"{', '.join(sorted(set(evidence.refused)))} not given on the ticket")

    assessment = evidence.assessment
    action = decision.action
    if action == REPLY_AND_CLOSE and assessment is not None and assessment.status in KEEP_OPEN:
        # The branch must act first: reply, keep the ticket open, and check again when they answer.
        notes.append(f"kept open: {assessment.status} needs the branch to act; the agent checks again when they reply")
        action = REQUEST_INFO

    if action == REQUEST_INFO:
        if not allow_request_info:
            return to(ESCALATE, "request_info is disabled (ALLOW_REQUEST_INFO=false)")
        asked = ticket.agent_replies(agent_staff_id)
        if asked >= max_info_requests:
            return to(ESCALATE, f"the agent has already replied {asked} time(s) on this ticket; a person should take over")

    # 3. The text that will be posted (OTPs and passwords may be named here, never asked for or given out).
    problems = reply_problems(decision.reply_text, pseudo, secret_topic=True)
    if problems:
        return to(ESCALATE, "reply blocked: " + "; ".join(problems))

    # 4. Confidence.
    if decision.confidence < confidence_threshold:
        return to(ESCALATE, f"confidence {decision.confidence:.2f} is below {confidence_threshold:.2f}")

    # 5. The reply must say what the lookups say.
    text = decision.reply_text or ""
    if assessment is not None:
        status = assessment.status
        if SAYS_NOT_REGISTERED.search(text) and status not in ("not_registered", "no_attempt_found",
                                                               "registration_failed_email", "registration_failed_temporary",
                                                               "needs_details"):
            return to(ESCALATE, f"reply blocked: it says the client is not registered, but the assessment is {status}")
        if SAYS_DELETE.search(text) and status not in DELETE_OK:
            return to(ESCALATE, f"reply blocked: it asks for the profile to be deleted, but the assessment is {status}")
        if status == "not_registered" and SAYS_REGISTERED_OK.search(text) and not SAYS_NOT_REGISTERED.search(text):
            return to(ESCALATE, "reply blocked: it says the client is registered, but no profile was found")

    if action == REPLY_AND_CLOSE:
        # 6. A close stands on lookups that worked and an assessment of this client with a final outcome.
        if evidence.check_errors:
            return to(NO_ACTION, "close blocked: a lookup failed during analysis; will retry", deferred=True)
        if assessment is None:
            return to(ESCALATE, "close blocked: the client's issue was not assessed")
        if assessment.status not in CLOSABLE:
            return to(ESCALATE, f"close blocked: the assessment is {assessment.status}, which needs a person")
        if assessment.status != "how_to" and assessment.cif is None and assessment.account is None:
            return to(ESCALATE, "close blocked: the assessment is not tied to a CIF or account on the ticket")
        # 7. Rate limit.
        if closes_so_far >= max_closes:
            return to(NO_ACTION, f"close limit of {max_closes} per run reached", deferred=True)

    if action != decision.action:
        return GuardrailResult(decision, replace(decision, action=action), notes)
    return GuardrailResult(decision, decision, notes)
