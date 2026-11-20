"""Allowlist of setups that cleared a pre-registered gate (placebo + OOS).

Paper and live OPEN decisions are hard-blocked unless the decision names a playbook
on this list for the symbol's asset class. Research CLIs (playbooks, study, study-b,
timing, score-reads) never consult this module.

Re-enable after a real PASS (see PLAYBOOKS.md):
  1. Confirm the cell cleared the gate and any registered follow-up diagnostics.
  2. Append an ApprovedSetup(playbook=..., asset_class=...) below.
  3. Ship the change. Do not flip TRADING_REQUIRE_APPROVED_SETUP=false to "try free-form
     again" — that reopens the failure mode Fix 1 closes.

Currently empty: Alpha–Epsilon and Study B all failed; no survivors.
"""
from __future__ import annotations

from dataclasses import dataclass

from .study import asset_class


@dataclass(frozen=True)
class ApprovedSetup:
    playbook: str       # registered playbook / study name, e.g. "alpha"
    asset_class: str    # study.asset_class value, e.g. "forex"


# Only cells that PASSed the pre-registered gate (and follow-ups). Empty = halt all opens.
APPROVED: tuple[ApprovedSetup, ...] = ()


def approved_pairs() -> frozenset[tuple[str, str]]:
    return frozenset((a.playbook, a.asset_class) for a in APPROVED)


def summarize_approved() -> str:
    if not APPROVED:
        return "(none — every free-form open is blocked)"
    return ", ".join(f"{a.playbook}/{a.asset_class}" for a in APPROVED)


def open_allowed(playbook: str, symbol: str) -> tuple[bool, str]:
    """Whether an OPEN may proceed. Returns (ok, reason_if_blocked)."""
    name = (playbook or "").strip().lower()
    group = asset_class(symbol)
    approved = approved_pairs()
    if not approved:
        return False, (
            "no approved setups: every playbook/study cell failed the pre-registered "
            "gate (placebo + OOS). Free-form opens are hard-blocked. "
            "Use action=wait (close/modify still allowed). "
            "Re-enable by adding a PASS to trading/survivors.py APPROVED — "
            "see PLAYBOOKS.md"
        )
    if not name:
        return False, (
            f"open requires playbook= one of the approved survivors for {group} "
            f"({summarize_approved()}); free-form / empty playbook is blocked"
        )
    if (name, group) not in approved:
        allowed_here = sorted(p for p, c in approved if c == group) or ["(none for this class)"]
        return False, (
            f"playbook {name!r} is not approved for {group} on {symbol}; "
            f"allowed here: {', '.join(allowed_here)}"
        )
    return True, ""
