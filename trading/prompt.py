"""Fills the limits into the system prompt from the settings.

The prompt tells the model the same numbers the guardrails enforce. Keeping them as {{TOKENS}} in
the prompt file, filled from the settings at start-up, means changing a limit in .env changes both
at once, and the two can't drift apart.
"""
from __future__ import annotations

import re

from .config import Settings


def tokens(s: Settings) -> dict[str, str]:
    return {
        "MAX_POSITIONS": str(s.max_positions),
        "MAX_NEW_OPENS": str(s.max_new_opens_per_scan),
        "MAX_SAME_DIRECTION": str(s.max_same_direction or s.max_positions),
        "MAX_SYNTHETIC": str(s.max_synthetic_positions),
        "MIN_STOP_ATR": f"{s.min_stop_atr:g}",
        "MAX_RISK_PCT": f"{s.max_risk_pct:g}",
        "MAX_TOTAL_RISK_PCT": f"{s.max_total_risk_pct:g}",
        "DAILY_LOSS_LIMIT_PCT": f"{s.daily_loss_limit_pct:g}",
        "LOCKOUT_HOURS": f"{s.lockout_hours:g}",
    }


def render_prompt(text: str, s: Settings) -> str:
    """Replace each {{TOKEN}}. An unknown token is an error, so a typo can't reach the model."""
    values = tokens(s)
    unknown = sorted(set(re.findall(r"\{\{(\w+)\}\}", text)) - set(values))
    if unknown:
        raise ValueError(f"unknown token(s) in the system prompt: {', '.join(unknown)}")
    for name, value in values.items():
        text = text.replace("{{" + name + "}}", value)
    return text
