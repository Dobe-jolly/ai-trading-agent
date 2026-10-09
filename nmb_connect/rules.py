"""Decisions without the model (MODEL_FALLBACK=rules), when Claude cannot be reached.

Only the simplest case is decided: a ticket that gives no CIF, account or mobile is asked for them. Everything else
needs the model to read the thread and name the problem, so it waits for the next pass (or is escalated when the
owner has already answered the agent). The usual rules in code still apply to whatever this returns.
"""
from __future__ import annotations

from typing import Optional

from agentcore.pii import Pseudonymizer
from .decision import ESCALATE, NO_ACTION, REQUEST_INFO, Decision
from .loop import AgentResult
from .osticket import Ticket
from .tools import Evidence

ASK = ("To check the client's NMB Connect profile we need the client's CIF or account number and the mobile number "
       "they use, and a screenshot of the error if there is one. Kindly reply on this ticket with these details.")


def rules_decide(ticket: Ticket, why: str = "", agent_staff_id: Optional[int] = None) -> Optional[AgentResult]:
    pseudo = Pseudonymizer()
    pseudo.mask(ticket.all_text())
    result = AgentResult(decision=Decision.fallback("rules fallback"), evidence=Evidence(), pseudo=pseudo)
    prefix = f"Claude was unreachable ({why}); decided by the fallback rules. " if why else "Fallback rules. "
    if ticket.agent_replies(agent_staff_id) and not ticket.answered_since_last_message():
        result.decision = Decision(ESCALATE, 1.0, "rules_fallback",
                                   prefix + "The ticket owner answered the agent; the conversation needs reading.")
        return result
    owner = ticket.owner_details()
    if not (owner.cifs or owner.accounts or owner.mobiles):
        result.decision = Decision(REQUEST_INFO, 1.0, "needs_details", prefix + "No CIF, account or mobile on the "
                                   "ticket.", ["no CIF, account number or mobile on the ticket"], ASK)
        return result
    result.decision = Decision(NO_ACTION, 1.0, "rules_fallback", prefix + "Left for the model to name the problem.")
    return result
