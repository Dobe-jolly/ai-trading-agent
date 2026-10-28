"""Theta: fade a statistical extreme (buy a fresh z-score of -2 or lower, sell +2 or higher).

Fixed rule, as approved in PLAYBOOKS.md (Phase 2). No filter of any kind: this measures raw reversion.
"""
from __future__ import annotations

from math import sqrt
from typing import Optional

from . import register
from .base import Bars, Playbook, Signal


def zscore(closes: list[float], t: int, length: int) -> Optional[float]:
    """Close at t against the mean and (population) standard deviation of the `length` closes ending at t."""
    if t < length - 1:
        return None
    window = closes[t - length + 1:t + 1]
    mean = sum(window) / length
    var = sum((c - mean) ** 2 for c in window) / length
    if var <= 0:
        return None
    return (closes[t] - mean) / sqrt(var)


@register
class Theta(Playbook):
    name = "theta"
    asset_classes = ("forex", "metals", "vol_index")      # vol_index is the negative control
    timeframe = "H4"
    warmup = 60
    stop_atr, target_atr, max_bars = 2.0, 2.0, 12

    LENGTH = 20
    Z = 2.0

    def scan(self, bars: Bars, t: int) -> Optional[Signal]:
        if not bars.atr[t]:
            return None
        z, before = zscore(bars.closes, t, self.LENGTH), zscore(bars.closes, t - 1, self.LENGTH)
        if z is None or before is None:
            return None
        if z <= -self.Z and before > -self.Z:
            return Signal(1, f"fresh extreme low, z {z:.2f}")
        if z >= self.Z and before < self.Z:
            return Signal(-1, f"fresh extreme high, z {z:.2f}")
        return None
