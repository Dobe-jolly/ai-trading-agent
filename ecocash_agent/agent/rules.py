"""Decisions without the model (MODEL_FALLBACK=rules), when Claude cannot be reached.

Only the simple cases: a ticket under an EcoCash topic gets the standard reply for the latest
link attempt of the details the owner gave. A ticket where the owner has answered the agent is
escalated, since that conversation needs reading. Tickets found by content wait for the model.
The usual rules in code still apply to whatever this returns.
"""
from __future__ import annotations

from typing import Optional

from .decision import ESCALATE, NO_ACTION, Decision
from .loop import AgentResult
from .osticket import Ticket
from .pii import Pseudonymizer
from .tools import Evidence, Toolbox, standard_reply


def rules_decide(ticket: Ticket, toolbox: Toolbox, why: str = "", agent_staff_id: Optional[int] = None) -> AgentResult:
    pseudo = Pseudonymizer()
    pseudo.mask(ticket.all_text())  # tokens for the values on the ticket, as the model would have
    evidence = Evidence()
    result = AgentResult(decision=Decision.fallback("rules fallback"), evidence=evidence, pseudo=pseudo)
    prefix = f"Claude was unreachable ({why}); decided by the fallback rules. " if why else "Fallback rules. "
    if not ticket.topic_match:
        result.decision = Decision(NO_ACTION, 1.0, "rules_fallback", prefix + "Ticket found by content; left for the model.")
        return result
    if ticket.agent_replies(agent_staff_id) and not ticket.answered_since_last_message():
        result.decision = Decision(ESCALATE, 1.0, "rules_fallback",
                                   prefix + "The ticket owner answered the agent; the conversation needs reading.")
        return result
    found = ticket.owner_identifiers()
    for kind, name in (("mobiles", "mobile"), ("accounts", "account_number"), ("ids", "id_number")):
        for value in sorted(getattr(found, kind))[:2]:
            toolbox.run("find_ecocash_attempts", {name: value}, pseudo, evidence, ticket)
    latest = evidence.latest
    action, reply = standard_reply(latest, ticket, evidence.link_records)
    status = evidence.status
    if not reply:
        result.decision = Decision(ESCALATE, 1.0, "rules_fallback", prefix + f"Latest attempt: {status}; needs a person.")
        return result
    result.decision = Decision(action, 1.0, "rules_fallback", prefix + f"Latest attempt: {status}. Standard reply.",
                               [f"latest attempt status: {status}"], pseudo.mask(reply))
    return result
