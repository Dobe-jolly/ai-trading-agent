"""Which failed-transaction replies wait for a person: the shared approval store with this desk's standard outcome,
which is the standard outcome for the confirmed transaction."""
from __future__ import annotations

from typing import Any, Optional

from agentcore import approval as _core
from agentcore.approval import ApprovalStore, MODES, summary, token_map  # noqa: F401

from .decision import Decision


def standard_action(evidence: Any) -> str:
    """Where the confirmed transaction leads without any further reasoning."""
    from .tools import standard_reply

    return standard_reply(evidence.subject, evidence.debit, evidence.status)[0]


def approval_reason(setting: str, decision: Decision, expected: Optional[str]) -> Optional[str]:
    return _core.approval_reason(setting, decision, expected,
                                 beyond="the agent reasoned beyond the standard outcome for the confirmed transaction",
                                 leads="the confirmed transaction alone leads to")
