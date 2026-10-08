"""Which SMS replies wait for a person: the shared approval store (agentcore.approval) with the SMS
agent's standard outcome, which is where the check statuses lead."""
from __future__ import annotations

from typing import Iterable, Optional

from agentcore import approval as _core
from agentcore.approval import ApprovalStore, MODES, summary, token_map  # noqa: F401

from .decision import ESCALATE, REPLY_AND_CLOSE, REQUEST_INFO, Decision
from .sms_checks import ESCALATING_STATUSES

# Statuses the model is told to escalate unless the thread resolves them.
UNSURE_STATUSES = frozenset({"sms_activation_unknown", "results_found"})


def standard_action(statuses: dict[str, str], no_close_statuses: Iterable[str]) -> str:
    """Where the check statuses lead without any further reasoning."""
    values = set(statuses.values())
    if not values:
        return REQUEST_INFO  # no usable account: ask for it
    if values & ESCALATING_STATUSES or values & UNSURE_STATUSES:
        return ESCALATE
    if values & set(no_close_statuses):
        return REQUEST_INFO
    return REPLY_AND_CLOSE


def approval_reason(setting: str, decision: Decision, expected: Optional[str]) -> Optional[str]:
    return _core.approval_reason(setting, decision, expected, beyond="the agent reasoned beyond the standard check results",
                                 leads="the check results alone lead to")
