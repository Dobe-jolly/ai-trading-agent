"""Rules-only decision: the Java agent's behaviour, used when the model cannot be reached.

The agent replaces the Java service, so a model outage must not leave tickets unattended.
This runs the same checks the Java ran (form accounts, form phone, requested alert types) and
decides as the Java would, except that it never closes on a data problem in the ticket itself
(no account, invalid account or phone): those are escalated for a person, since asking the
owner for the right value needs the model.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from .decision import ESCALATE, REPLY_AND_CLOSE, Decision
from .loop import AgentResult
from .osticket import Ticket
from agentcore.pii import Pseudonymizer
from .sms_checks import ESCALATING_STATUSES, SmsChecks
from .tools import Evidence

CATEGORY = "rules_fallback"


def rules_decide(ticket: Ticket, checks: SmsChecks, no_close_statuses: frozenset,
                 as_of: Optional[date] = None, why: str = "model unavailable") -> AgentResult:
    pseudo = Pseudonymizer()
    evidence = Evidence()
    accounts = ticket.required_accounts

    def result(decision: Decision) -> AgentResult:
        return AgentResult(decision=decision, evidence=evidence, pseudo=pseudo)

    if not accounts:
        return result(Decision(ESCALATE, 1.0, CATEGORY,
                               f"Rules fallback ({why}): the ticket has no usable account number on the form."))
    for account in accounts:
        evidence.account_results[account] = checks.check_account(
            account, ticket.requested_alert_types, ticket.phone_number, as_of=as_of)
        evidence.phones_used[account] = ticket.phone_number
        evidence.tools_used.append("check_sms_alert_account")

    statuses = evidence.statuses
    template = evidence.template_reply(accounts, ticket.currency) or ""
    summary = ", ".join(f"{pseudo.mask(a)}={s}" for a, s in statuses.items())
    blocking = {a: s for a, s in statuses.items() if s in ESCALATING_STATUSES or s in no_close_statuses}
    if blocking:
        return result(Decision(ESCALATE, 1.0, CATEGORY,
                               f"Rules fallback ({why}): statuses {summary}; not closable without a person.",
                               evidence=[pseudo.mask(template)] if template else []))
    return result(Decision(REPLY_AND_CLOSE, 1.0, CATEGORY,
                           f"Rules fallback ({why}): statuses {summary}; closed with the standard reply.",
                           evidence=[], reply_text=pseudo.mask(template)))
