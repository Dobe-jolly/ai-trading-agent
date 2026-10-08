"""Which EcoCash replies wait for a person: the shared approval store (agentcore.approval) with the
EcoCash agent's standard outcome, which is the standard reply for the client's latest link attempt."""
from __future__ import annotations

from typing import Any, Optional

from agentcore import approval as _core
from agentcore.approval import ApprovalStore, MODES, summary, token_map  # noqa: F401

from .decision import Decision


def standard_action(evidence: Any, ticket: Any) -> str:
    """Where the latest link attempt leads without any further reasoning (the standard reply's action)."""
    from .tools import standard_reply

    return standard_reply(evidence.latest, ticket, evidence.link_records)[0]


def approval_reason(setting: str, decision: Decision, expected: Optional[str]) -> Optional[str]:
    return _core.approval_reason(setting, decision, expected,
                                 beyond="the agent reasoned beyond the standard reply for the latest attempt",
                                 leads="the latest attempt alone leads to")
