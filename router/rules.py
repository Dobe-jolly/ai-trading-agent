"""Decisions without the model (MODEL_FALLBACK=rules), when Claude cannot be reached.

Routing needs the ticket read, so the router never moves a ticket without the model. The one decision it makes on
its own: a ticket whose owner wrote nothing about the problem is asked to describe it. Everything else waits for
the next pass. The usual rules in code still apply to whatever this returns.
"""
from __future__ import annotations

from typing import Optional

from agentcore.pii import Pseudonymizer
from .decision import NO_ACTION, REQUEST_INFO, RouteDecision
from .loop import AgentResult
from .osticket import Ticket
from .tools import Evidence

ASK = ("Kindly describe the problem so that we can send this ticket to the team that handles it: what the client was "
       "trying to do, on which channel (branch, NMB Connect app, USSD, internet banking, card or EcoCash), and the "
       "error they saw. A screenshot of the error helps.")


def rules_decide(ticket: Ticket, why: str = "", agent_staff_id: Optional[int] = None) -> Optional[AgentResult]:
    pseudo = Pseudonymizer()
    pseudo.mask(ticket.all_text())
    prefix = f"Claude was unreachable ({why}); decided by the fallback rules. " if why else "Fallback rules. "
    result = AgentResult(decision=RouteDecision.fallback("rules fallback"), evidence=Evidence(), pseudo=pseudo)
    if not ticket.has_issue_text() and not ticket.agent_replies(agent_staff_id):
        result.decision = RouteDecision(REQUEST_INFO, 1.0, "no_description", prefix + "The ticket does not describe "
                                        "the problem.", ["no description of the problem on the ticket"], ASK,
                                        standard=True)
        return result
    result.decision = RouteDecision(NO_ACTION, 1.0, "rules_fallback", prefix + "Left for the model to read.")
    return result
