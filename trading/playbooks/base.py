"""What a playbook is, and the bar data it works from.

A playbook is a deterministic rule: given the bars up to and including bar t, it says "buy",
"sell" or nothing. It never sees bar t+1. The test machine enters at the open of bar t+1.

Stops and targets are expressed in ATRs measured at bar t, so the same rule means the same thing
on EURUSD and on a synthetic index, and the geometry of a trade is fixed before it is placed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

ATR_PERIOD = 14


def atr_series(highs: list[float], lows: list[float], closes: list[float],
               period: int = ATR_PERIOD) -> list[Optional[float]]:
    """Wilder ATR at every bar, None until there are `period` true ranges. Uses only past bars."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n <= period:
        return out
    trs = [highs[0] - lows[0]] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, n)
    ]
    value = sum(trs[1:period + 1]) / period
    out[period] = value
    for i in range(period + 1, n):
        value = (value * (period - 1) + trs[i]) / period
        out[i] = value
    return out


@dataclass
class Bars:
    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    times: list = field(default_factory=list)
    atr: list[Optional[float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.atr:
            self.atr = atr_series(self.highs, self.lows, self.closes)

    def __len__(self) -> int:
        return len(self.closes)

    @classmethod
    def from_candles(cls, candles: list) -> "Bars":
        return cls(
            opens=[c.open for c in candles], highs=[c.high for c in candles],
            lows=[c.low for c in candles], closes=[c.close for c in candles],
            times=[getattr(c, "time", None) for c in candles],
        )


@dataclass(frozen=True)
class Signal:
    direction: int                  # +1 buy, -1 sell
    note: str = ""


class Playbook:
    """Subclass and set the fixed trade geometry and the markets the rule is meant for.

    The geometry is part of the rule and is not tuned afterwards: stop and target distances in
    ATRs, and a time limit in bars after which the trade is closed at the market.
    """
    name: str = ""
    asset_classes: tuple[str, ...] = ()      # values of study.asset_class this rule is meant for
    timeframe: str = "H1"
    warmup: int = 60                         # bars needed before the rule can speak
    stop_atr: float = 1.5
    target_atr: float = 3.0
    max_bars: int = 48

    def scan(self, bars: Bars, t: int) -> Optional[Signal]:
        """Look at bars[0..t] only. Return a Signal or None."""
        raise NotImplementedError
