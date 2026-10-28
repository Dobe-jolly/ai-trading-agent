"""Gamma: fade the edge of a range, only in a market the engine calls a range.

Fixed rule, as approved in PLAYBOOKS.md (Phase 2). The range is the 20 bars BEFORE bar t, so the signal
bar can't define its own range.
"""
from __future__ import annotations

from typing import Optional

from ..geometry import TREND_TOL_ATR, classify_regime, find_swings
from . import register
from .base import Bars, Playbook, Signal


@register
class Gamma(Playbook):
    name = "gamma"
    asset_classes = ("forex", "metals", "vol_index")      # vol_index is the negative control
    timeframe = "H4"
    warmup = 60
    stop_atr, target_atr, max_bars = 2.0, 2.0, 12

    WINDOW = 150          # bars of history the regime is read from (as in Alpha)
    SWING_N = 3
    RANGE_BARS = 20
    MIN_WIDTH_ATR, MAX_WIDTH_ATR = 2.0, 6.0
    EDGE_ATR = 0.25       # how close to the edge the bar's extreme must come

    def scan(self, bars: Bars, t: int) -> Optional[Signal]:
        atr = bars.atr[t]
        if not atr or t < self.RANGE_BARS:
            return None
        top = max(bars.highs[t - self.RANGE_BARS:t])
        bottom = min(bars.lows[t - self.RANGE_BARS:t])
        if not self.MIN_WIDTH_ATR <= (top - bottom) / atr <= self.MAX_WIDTH_ATR:
            return None

        lo = max(0, t - self.WINDOW + 1)
        swings = find_swings(bars.highs[lo:t + 1], bars.lows[lo:t + 1], self.SWING_N)
        if classify_regime(swings, TREND_TOL_ATR * atr) != "RANGE":
            return None

        buy = (bars.lows[t] <= bottom + self.EDGE_ATR * atr      # touched the lower edge
               and bars.closes[t] > bottom                        # without breaking down
               and bars.closes[t] > bars.opens[t])                # and closed up
        sell = (bars.highs[t] >= top - self.EDGE_ATR * atr
                and bars.closes[t] < top
                and bars.closes[t] < bars.opens[t])
        if buy == sell:                                           # neither, or an ambiguous bar spanning both edges
            return None
        return Signal(1, "bounce off the range low") if buy else Signal(-1, "rejection at the range high")
