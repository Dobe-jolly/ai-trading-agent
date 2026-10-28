"""Replays a playbook over historical bars and records every trade it would have made, in R.

Conventions, all chosen to be pessimistic where the candle data can't tell:
  - A signal at the close of bar t is entered at the OPEN of bar t+1. Never earlier.
  - One trade at a time per symbol. The next signal is looked for after the trade closes.
  - If the stop and the target both fall inside one bar, the stop is assumed to have come first.
  - If a bar opens beyond the stop, the fill is at that open (a gap), which is worse than the stop.
  - One full spread is charged per trade, expressed in R (spread / stop distance).
  - R is measured against the initial stop distance: a stop-out is about -1R before costs.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from .base import Bars, Playbook, Signal

OOS_FRACTION = 0.4        # the last 40% of each history is the out-of-sample half


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_index: int
    exit_index: int
    direction: int
    r: float                  # net of the spread
    reason: str               # stop | target | time | end
    half: int                 # 0 = first 60% of the history, 1 = last 40%


@dataclass
class SimResult:
    trades: list[Trade]
    signals: int
    free_bars: int            # bars on which a signal could have been taken (flat and warmed up)


def simulate(symbol: str, bars: Bars, playbook: Playbook, spread: float = 0.0) -> SimResult:
    n = len(bars)
    split = int(n * (1 - OOS_FRACTION))
    trades: list[Trade] = []
    signals = free = 0
    t = playbook.warmup
    while t < n - 2:
        free += 1
        sig = playbook.scan(bars, t)
        a = bars.atr[t]
        if sig is None or not a:
            t += 1
            continue
        signals += 1
        trade = _trade(symbol, bars, playbook, sig, t, a, spread, split)
        trades.append(trade)
        t = max(trade.exit_index, t + 1)
    return SimResult(trades, signals, free)


def _trade(symbol: str, bars: Bars, pb: Playbook, sig: Signal, t: int, a: float,
           spread: float, split: int) -> Trade:
    d = sig.direction
    entry_i = t + 1
    entry = bars.opens[entry_i]
    risk, reward = pb.stop_atr * a, pb.target_atr * a
    stop, target = entry - d * risk, entry + d * reward
    last = min(entry_i + pb.max_bars - 1, len(bars) - 1)
    cost = spread / risk

    for j in range(entry_i, last + 1):
        o, hi, lo = bars.opens[j], bars.highs[j], bars.lows[j]
        if j > entry_i:                                   # a gap over the level fills at the open
            if d * (o - stop) <= 0:
                return Trade(symbol, entry_i, j, d, d * (o - entry) / risk - cost, "stop", int(entry_i >= split))
            if d * (o - target) >= 0:
                return Trade(symbol, entry_i, j, d, d * (o - entry) / risk - cost, "target", int(entry_i >= split))
        stop_hit = lo <= stop if d > 0 else hi >= stop
        target_hit = hi >= target if d > 0 else lo <= target
        if stop_hit:                                      # stop wins a tie within one bar
            return Trade(symbol, entry_i, j, d, -1.0 - cost, "stop", int(entry_i >= split))
        if target_hit:
            return Trade(symbol, entry_i, j, d, reward / risk - cost, "target", int(entry_i >= split))

    reason = "time" if last == entry_i + pb.max_bars - 1 else "end"
    return Trade(symbol, entry_i, last, d, d * (bars.closes[last] - entry) / risk - cost, reason, int(entry_i >= split))


def lookahead_violations(playbook: Playbook, bars: Bars, step: int = 5) -> list[int]:
    """Bars at which the rule's answer depends on data after bar t.

    Runs the rule on the full history and again on a copy cut off at t. A rule that only reads
    bars[0..t] gives the same answer both times; any difference means it looked ahead. Every
    playbook has to come back empty before its results mean anything. (Not meaningful for rules
    that carry random state, such as the placebo.)
    """
    bad = []
    for t in range(playbook.warmup, len(bars) - 1, step):
        cut = Bars(bars.opens[:t + 1], bars.highs[:t + 1], bars.lows[:t + 1], bars.closes[:t + 1],
                   bars.times[:t + 1])
        if playbook.scan(bars, t) != playbook.scan(cut, t):
            bad.append(t)
    return bad


class Placebo(Playbook):
    """Enters at random bars in a random direction with the SAME stop, target and time limit as the
    playbook it shadows. Whatever it earns is what the trade geometry and the costs earn on their
    own, so a real playbook has to beat it, not zero."""

    name = "placebo"

    def __init__(self, like: Playbook, rate: float, seed: str) -> None:
        self.asset_classes = like.asset_classes
        self.warmup, self.stop_atr, self.target_atr, self.max_bars = (
            like.warmup, like.stop_atr, like.target_atr, like.max_bars)
        self._rate = rate
        self._rng = random.Random(seed)

    def scan(self, bars: Bars, t: int) -> Optional[Signal]:
        if self._rng.random() >= self._rate:
            return None
        return Signal(self._rng.choice((-1, 1)), "placebo")
