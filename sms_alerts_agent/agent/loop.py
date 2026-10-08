"""The reasoning loop: the model reads the masked ticket, calls the checks, and ends with submit_decision."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .attachments import render_attachment
from .decision import SUBMIT_DECISION_TOOL
from .normalize import today_local
from .osticket import Ticket
from agentcore.pii import Pseudonymizer
from .tools import Evidence, Toolbox

from agentcore import loop as _core
from agentcore.loop import AgentError, NUDGE  # noqa: F401 - re-exported

log = logging.getLogger(__name__)
MAX_ENTRY_CHARS = 2500
MAX_THREAD_CHARS = 16000  # thread text plus what was read from attachments
ENTRY_LABELS = {"M": "Ticket owner", "R": "Staff reply", "N": "Internal note"}


def render_ticket(ticket: Ticket, as_of: Optional[str] = None, agent_staff_id: Optional[int] = None,
                  context: Optional[str] = None) -> str:
    """The ticket as the model sees it (before masking)."""
    accounts = ticket.required_accounts
    lines = [
        f"Today: {as_of or today_local().isoformat()}",
        *([f"Operations context: {context}", ""] if context else []),
        f"Ticket #{ticket.number}   Help topic: {ticket.help_topic or '(none)'}   Opened: {ticket.created}"
        + (f"   Raised by: {ticket.org_name}" if ticket.org_name else ""),
        f"Subject: {ticket.subject or '(none)'}",
        *([] if ticket.topic_match else [
            "",
            "Filed under another help topic: this ticket is not under SMS Alerts. It was picked up because a first "
            f"read found it to be about SMS alerts{' and something else as well' if ticket.triage_kind == 'mixed' else ''}"
            f" ({ticket.found_by or 'run on request'}). The form below belongs to that topic, so the account and "
            "phone are probably in the subject, the messages or the attachments.",
        ]),
        "",
        "Form fields (as captured on the ticket):",
        f"  Phone to receive alerts: {ticket.phone_number or '(empty)'}",
        f"  CIF: {'(captured, not shown)' if ticket.cif else '(empty)'}",
        f"  Account number: {ticket.account_number or '(empty)'}",
        f"  Account number 2: {ticket.account_number_2 or '(empty)'}",
        f"  Currency: {ticket.currency or '(empty)'}",
        f"  Salaries or Debits: {ticket.alert_type_field or '(empty)'}",
        "",
        f"Requested alert types (from the form and topic): {', '.join(t.lower() for t in ticket.requested_alert_types)}",
        "Accounts the standard process checks: " + (", ".join(accounts) if accounts else "(none - no usable account number on the form)"),
        "",
        "Thread, oldest first:",
    ]
    budget = MAX_THREAD_CHARS
    rendered: list[str] = []
    for entry in reversed(ticket.entries):  # newest first so recent messages survive truncation
        text = entry.body[:MAX_ENTRY_CHARS] + (" [...truncated]" if len(entry.body) > MAX_ENTRY_CHARS else "")
        who = ENTRY_LABELS.get(entry.type, entry.type)
        if entry.type != "M":
            who += f" ({entry.poster})"
            if agent_staff_id is not None and entry.staff_id == agent_staff_id:
                who += " [posted by the SMS agent - an earlier automated reply/note]"
        files = "\n".join(render_attachment(a) for a in entry.attachments)
        block = f"--- [{entry.created}] {who}\n{text}" + (f"\n{files}" if files else "")
        if len(block) > budget and not rendered:  # the newest entry is always shown, cut to fit
            block = block[:budget] + " [...truncated]"
        elif len(block) > budget:
            rendered.append("--- [older entries omitted]")
            break
        budget -= len(block)
        rendered.append(block)
    if not rendered:
        rendered.append("(no thread entries)")
    return "\n".join(lines + list(reversed(rendered)))


@dataclass
class AgentResult(_core.AgentResult):
    evidence: Evidence = field(default_factory=Evidence)


class TicketAgent(_core.TicketAgent):
    result_type = AgentResult
    instruction = 'Investigate with the checks, then call submit_decision.'
    render = staticmethod(render_ticket)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("decision_tool", SUBMIT_DECISION_TOOL)
        super().__init__(*args, **kwargs)
