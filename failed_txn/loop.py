"""The reasoning loop: the model reads the masked ticket, calls the lookups, and ends with submit_decision."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from agentcore import loop as _core
from agentcore.loop import AgentError, NUDGE  # noqa: F401 - re-exported
from .attachments import render_attachment
from .decision import SUBMIT_DECISION_TOOL
from .normalize import today_local
from .osticket import Ticket
from .tools import Evidence

log = logging.getLogger(__name__)
MAX_ENTRY_CHARS = 2500
MAX_THREAD_CHARS = 16000  # thread text plus what was read from attachments
ENTRY_LABELS = {"M": "Ticket owner", "R": "Staff reply", "N": "Internal note"}


def render_ticket(ticket: Ticket, as_of: Optional[str] = None, agent_staff_id: Optional[int] = None,
                  context: Optional[str] = None) -> str:
    """The ticket as the model sees it (before masking)."""
    found = ticket.owner_details()
    lines = [
        f"Today: {as_of or today_local().isoformat()}",
        *([f"Operations context: {context}", ""] if context else []),
        f"Ticket #{ticket.number}   Help topic: {ticket.help_topic or '(none)'}   Opened: {ticket.created}"
        + (f"   Raised by: {ticket.org_name}" if ticket.org_name else ""),
        f"Subject: {ticket.subject or '(none)'}",
        "",
        "Form fields (as captured on the ticket):",
    ]
    lines += [f"  {label}: {value}" for label, value in ticket.fields.items()] or ["  (none)"]
    lines += [
        "",
        "Details the ticket owner gave (form, messages and their attachments):",
        "  Account numbers: " + (", ".join(sorted(found.accounts)) or "(none)"),
        "  CIF / customer numbers: " + (", ".join(sorted(found.cifs)) or "(none)"),
        "  References: " + (", ".join(sorted(found.references)) or "(none)"),
        "  Amounts: " + (", ".join(f"{a:,.2f}" for a in sorted(found.amounts)) or "(none)"),
        "  Dates: " + (", ".join(d.isoformat() for d in sorted(found.dates)) or "(none)"),
        # Keys are the last 9 digits; shown as 0 + key so they mask as the same <MOBILE_n> as in the thread.
        "  Mobile numbers: " + (", ".join("0" + m for m in sorted(found.mobiles)) or "(none)"),
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
                who += " [posted by the failed-transactions agent - an earlier automated reply/note]"
        files = "\n".join(render_attachment(a) for a in entry.attachments)
        block = f"--- [{entry.created}] {who}\n{text}" + (f"\n{files}" if files else "")
        if len(block) > budget and not rendered:
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
    instruction = "Find the transaction with the lookups, confirm it, then call submit_decision."
    render = staticmethod(render_ticket)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("decision_tool", SUBMIT_DECISION_TOOL)
        super().__init__(*args, **kwargs)
