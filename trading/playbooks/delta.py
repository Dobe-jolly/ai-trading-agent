"""Delta: fade a spike on Boom and Crash (sell after an up spike, buy after a down spike).

Fixed rule, as approved in PLAYBOOKS.md (Phase 3). The rule reads the bar, not the symbol name: Boom's spikes
are up and Crash's are down.
"""
from __future__ import annotations

from typing import Optional

from . import register
from .base import Bars, Playbook, Signal


@register
class Delta(Playbook):
    name = "delta"
    asset_classes = ("boom_crash",)
    timeframe = "M5"
    warmup = 60
    stop_atr, target_atr, max_bars = 2.0, 2.0, 12

    SPIKE_ATR = 3.0       # body size, in ATRs measured at the previous bar

    def scan(self, bars: Bars, t: int) -> Optional[Signal]:
        if t < 1 or not bars.atr[t] or not bars.atr[t - 1]:
            return None
        body = bars.closes[t] - bars.opens[t]
        if abs(body) < self.SPIKE_ATR * bars.atr[t - 1]:
            return None
        return Signal(-1, "fade an up spike") if body > 0 else Signal(1, "fade a down spike")
