"""Alpha: buy the pullback to the last higher low in an uptrend (sell the mirror in a downtrend).

Fixed rule, as approved in PLAYBOOKS.md. Uses the repo's own definitions, so this is the structure the
model is shown by get_structure: swings need 3 bars each side, trend comes from geometry.classify_regime.
"""
from __future__ import annotations

from typing import Optional

from ..geometry import TREND_TOL_ATR, classify_regime, find_swings
from . import register
from .base import Bars, Playbook, Signal


@register
class Alpha(Playbook):
    name = "alpha"
    asset_classes = ("forex", "metals")
    timeframe = "H4"
    warmup = 60
    stop_atr, target_atr, max_bars = 1.5, 3.0, 30

    WINDOW = 150          # bars of history the structure is read from
    SWING_N = 3           # bars each side that confirm a swing (the engine's default)
    NEAR_ATR = 1.0        # how close to the swing the bar's extreme must come

    def scan(self, bars: Bars, t: int) -> Optional[Signal]:
        atr = bars.atr[t]
        if not atr:
            return None
        lo = max(0, t - self.WINDOW + 1)
        swings = find_swings(bars.highs[lo:t + 1], bars.lows[lo:t + 1], self.SWING_N)
        regime = classify_regime(swings, TREND_TOL_ATR * atr)

        if regime == "BULLISH_TREND":
            swing_low = next((s for s in reversed(swings) if s.kind == "low"), None)
            if (swing_low is not None
                    and bars.lows[t] <= swing_low.price + self.NEAR_ATR * atr   # came back to the higher low
                    and bars.closes[t] > swing_low.price                         # and held above it
                    and bars.closes[t] > bars.opens[t]):                         # closing up
                return Signal(1, "pullback to the last higher low")
        elif regime == "BEARISH_TREND":
            swing_high = next((s for s in reversed(swings) if s.kind == "high"), None)
            if (swing_high is not None
                    and bars.highs[t] >= swing_high.price - self.NEAR_ATR * atr
                    and bars.closes[t] < swing_high.price
                    and bars.closes[t] < bars.opens[t]):
                return Signal(-1, "pullback to the last lower high")
        return None
