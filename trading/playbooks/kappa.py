"""Kappa: after a retrace covers a multi-timeframe fair value gap, trade with the trend.

Fixed rule, as written in PLAYBOOKS.md before any replay. The market is Volatility 100 (1s)
on the 15-minute chart. The daily regime has to be a trend. The level is a fair value gap
that overlaps a same-direction gap on at least one other of D1, H4, H1 and M15. In an
uptrend the retrace comes down into that bullish gap and the first up-close is a buy. A
downtrend is the mirror.

The stop, target and time limit are the conventional Alpha geometry translated onto M15
(2R, eight hours). They are not fitted.
"""
from __future__ import annotations

from typing import Optional

from ..fvg_align import LOOKBACK_M15, continuation_at
from . import register
from .base import Bars, Playbook, Signal

SYMBOL = "Volatility 100 (1s) Index"


@register
class Kappa(Playbook):
    name = "kappa"
    asset_classes = ("vol_index",)
    symbols = (SYMBOL,)
    timeframe = "M15"
    warmup = LOOKBACK_M15
    stop_atr, target_atr, max_bars = 1.5, 3.0, 32

    def scan(self, bars: Bars, t: int) -> Optional[Signal]:
        found = continuation_at(bars, t)
        if found is None:
            return None
        direction, zone = found
        joined = "+".join(zone.timeframes)
        return Signal(
            direction,
            f"retrace covered {joined} {zone.direction} gap {zone.bottom:.6f}-{zone.top:.6f}",
        )
