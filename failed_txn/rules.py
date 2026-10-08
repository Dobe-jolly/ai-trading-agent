"""Decisions without the model (MODEL_FALLBACK=rules), when Claude cannot be reached.

Only the simplest case is decided: a ticket that gives no account and no reference is asked for them. Everything
else needs the model to read the thread and pick the transaction, so it waits for the next pass (or is escalated
when the owner has already answered the agent). The usual rules in code still apply to whatever this returns.
"""
from __future__ import annotations

from typing import Optional

from agentcore.pii import Pseudonymizer
from .decision import ESCALATE, NO_ACTION, REQUEST_INFO, Decision
from .loop import AgentResult
from .osticket import Ticket
from .tools import Evidence

ASK = ("To look into the failed transaction we need the account number it was made from, the date and time, the "
       "amount and, if available, the reference (RRN) or a screenshot of the message the client received. Kindly "
       "reply on this ticket with these details.")


def rules_decide(ticket: Ticket, why: str = "", agent_staff_id: Optional[int] = None) -> Optional[AgentResult]:
    pseudo = Pseudonymizer()
    pseudo.mask(ticket.all_text())
    result = AgentResult(decision=Decision.fallback("rules fallback"), evidence=Evidence(), pseudo=pseudo)
    prefix = f"Claude was unreachable ({why}); decided by the fallback rules. " if why else "Fallback rules. "
    if ticket.agent_replies(agent_staff_id) and not ticket.answered_since_last_message():
        result.decision = Decision(ESCALATE, 1.0, "rules_fallback",
                                   prefix + "The ticket owner answered the agent; the conversation needs reading.")
        return result
    if not ticket.owner_details():
        result.decision = Decision(REQUEST_INFO, 1.0, "missing_account_number", prefix + "No account or reference "
                                   "on the ticket.", ["no account number or reference on the ticket"], ASK)
        return result
    result.decision = Decision(NO_ACTION, 1.0, "rules_fallback", prefix + "Left for the model to pick the transaction.")
    return result
