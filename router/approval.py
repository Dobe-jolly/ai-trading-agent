"""Which router replies wait for a person: the shared approval store. The router's only reply asks the branch what
the problem is; its standard outcome is that request when the owner did not describe the problem."""
from __future__ import annotations

from typing import Any, Optional

from agentcore import approval as _core
from agentcore.approval import ApprovalStore, MODES, summary, token_map  # noqa: F401

from .decision import Decision


def approval_reason(setting: str, decision: Decision, expected: Optional[str]) -> Optional[str]:
    return _core.approval_reason(setting, decision, expected,
                                 beyond="the agent asked for something other than a description of the problem",
                                 leads="the ticket already describes the problem, so the standard outcome is")


def standard_action(ticket: Any) -> Optional[str]:
    """request_info when the owner wrote too little to route the ticket; otherwise the ticket can be routed as it is
    (a request for information then waits for a person)."""
    return "request_info" if not ticket.has_issue_text() else "move"
