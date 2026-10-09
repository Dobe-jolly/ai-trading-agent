"""Which NMB Connect replies wait for a person: the shared approval store with this desk's standard outcome, which
is the standard outcome of the assessment."""
from __future__ import annotations

from typing import Any, Optional

from agentcore import approval as _core
from agentcore.approval import ApprovalStore, MODES, summary, token_map  # noqa: F401

from .decision import Decision


def standard_action(evidence: Any) -> Optional[str]:
    """Where the assessment leads without any further reasoning (None when nothing was assessed)."""
    assessment = getattr(evidence, "assessment", None)
    return assessment.action if assessment is not None else None


def approval_reason(setting: str, decision: Decision, expected: Optional[str]) -> Optional[str]:
    return _core.approval_reason(setting, decision, expected,
                                 beyond="the agent reasoned beyond the standard outcome of the assessment",
                                 leads="the assessment alone leads to")
