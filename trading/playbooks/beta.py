"""Beta: trade the first London-morning breakout of the Asian-session range.

Fixed rule, as approved in PLAYBOOKS.md. Bar times are taken as UTC (true for Deriv's servers).
"""
from __future__ import annotations

from typing import Optional

from . import register
from .base import Bars, Playbook, Signal


@register
class Beta(Playbook):
    name = "beta"
    asset_classes = ("forex", "metals")
    timeframe = "H1"
    warmup = 48
    stop_atr, target_atr, max_bars = 1.5, 3.0, 12

    LAST_ASIAN_HOUR = 6            # the range is built from 00:00 to 06:59 UTC
    TRADE_HOURS = range(7, 11)     # breakouts count from 07:00 to 10:59 UTC
    MIN_ASIAN_BARS = 4             # a day with fewer Asian bars (a data gap) is skipped
    MIN_WIDTH_ATR = 0.5            # a range narrower than this is noise...
    MAX_WIDTH_ATR = 2.5            # ...and one wider than this is not a quiet session

    def scan(self, bars: Bars, t: int) -> Optional[Signal]:
        now = bars.times[t] if t < len(bars.times) else None
        atr = bars.atr[t]
        if now is None or not atr or now.hour not in self.TRADE_HOURS:
            return None

        day = now.date()
        asian, earlier = [], []                     # today's bars before t: the range bars, and the trading-hours bars
        j = t - 1
        while j >= 0 and bars.times[j] is not None and bars.times[j].date() == day:
            (asian if bars.times[j].hour <= self.LAST_ASIAN_HOUR else earlier).append(j)
            j -= 1
        if len(asian) < self.MIN_ASIAN_BARS:
            return None

        high = max(bars.highs[i] for i in asian)
        low = min(bars.lows[i] for i in asian)
        if not (self.MIN_WIDTH_ATR * atr <= high - low <= self.MAX_WIDTH_ATR * atr):
            return None
        if any(bars.closes[i] > high or bars.closes[i] < low for i in earlier):
            return None                             # the day's first breakout has already happened

        if bars.closes[t] > high:
            return Signal(1, "broke the Asian range upward")
        if bars.closes[t] < low:
            return Signal(-1, "broke the Asian range downward")
        return None
