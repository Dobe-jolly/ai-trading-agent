"""Scores a playbook's simulated trades and decides pass or fail by a gate fixed in advance.

The gate is not tuned after seeing results. For each asset class a playbook is meant for it must,
all at once:
  - have at least MIN_TRADES trades, an average R above zero, and t >= MIN_T;
  - hold up out of sample (last 40% of each history): at least MIN_OOS_TRADES trades, average R
    above zero, t >= MIN_OOS_T;
  - beat a placebo that trades the same geometry at random by at least MIN_EDGE_OVER_PLACEBO R.
The placebo is there because stops, targets and spreads give any entry rule a baseline expectancy;
what matters is how much of the result comes from the rule's timing and not from the geometry.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..study import asset_class
from .base import Bars, Playbook
from .simulate import Placebo, Trade, simulate

MIN_TRADES = 100
MIN_T = 3.0
MIN_OOS_TRADES = 40
MIN_OOS_T = 1.65
MIN_EDGE_OVER_PLACEBO = 0.05


@dataclass
class Stats:
    n: int = 0
    wins: int = 0
    avg_r: float = 0.0
    t: float = 0.0
    total_r: float = 0.0
    profit_factor: Optional[float] = None
    n_oos: int = 0
    avg_r_oos: float = 0.0
    t_oos: float = 0.0

    @property
    def win_pct(self) -> float:
        return self.wins / self.n * 100 if self.n else 0.0


def _mean_t(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, 0.0
    sd = statistics.stdev(values)
    return mean, (mean / (sd / math.sqrt(len(values))) if sd > 0 else 0.0)


def stats(trades: list[Trade]) -> Stats:
    rs = [t.r for t in trades]
    oos = [t.r for t in trades if t.half == 1]
    avg, t = _mean_t(rs)
    avg_oos, t_oos = _mean_t(oos)
    gains, losses = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    return Stats(
        n=len(rs), wins=sum(r > 0 for r in rs), avg_r=avg, t=t, total_r=sum(rs),
        profit_factor=(gains / losses) if losses > 0 else None,
        n_oos=len(oos), avg_r_oos=avg_oos, t_oos=t_oos,
    )


def judge(real: Stats, placebo: Stats) -> tuple[str, list[str]]:
    """PASS only if every condition holds. Returns the verdict and the conditions that failed."""
    failed = []
    if real.n < MIN_TRADES:
        failed.append(f"only {real.n} trades (need {MIN_TRADES})")
    if real.avg_r <= 0:
        failed.append(f"average R {real.avg_r:+.3f} is not above zero")
    if real.t < MIN_T:
        failed.append(f"t {real.t:+.2f} is below {MIN_T}")
    if real.n_oos < MIN_OOS_TRADES:
        failed.append(f"only {real.n_oos} out-of-sample trades (need {MIN_OOS_TRADES})")
    if real.avg_r_oos <= 0:
        failed.append(f"out-of-sample average R {real.avg_r_oos:+.3f} is not above zero")
    if real.t_oos < MIN_OOS_T:
        failed.append(f"out-of-sample t {real.t_oos:+.2f} is below {MIN_OOS_T}")
    if real.avg_r - placebo.avg_r < MIN_EDGE_OVER_PLACEBO:
        failed.append(f"beats the placebo by only {real.avg_r - placebo.avg_r:+.3f} R "
                      f"(real {real.avg_r:+.3f}, placebo {placebo.avg_r:+.3f}; need {MIN_EDGE_OVER_PLACEBO})")
    return ("FAIL" if failed else "PASS"), failed


@dataclass
class Row:
    group: str
    symbols: list[str]
    real: Stats
    placebo: Stats
    verdict: str
    failed: list[str]
    per_symbol: dict[str, tuple[int, float]] = field(default_factory=dict)   # symbol -> (trades, average R)


@dataclass
class Report:
    playbook: str
    timeframe: str
    rows: list[Row]
    stop_atr: float
    target_atr: float
    max_bars: int

    def passed(self) -> list[Row]:
        return [r for r in self.rows if r.verdict == "PASS"]

    def text(self) -> str:
        lines = [
            f"Playbook {self.playbook} on {self.timeframe}: stop {self.stop_atr:g} ATR, target {self.target_atr:g} ATR, "
            f"time limit {self.max_bars} bars. Results in R (1R = the initial stop), after one spread per trade.",
            f"Gate per asset class: >={MIN_TRADES} trades, avg R > 0, t >= {MIN_T:g}; out of sample >={MIN_OOS_TRADES} "
            f"trades, avg R > 0, t >= {MIN_OOS_T:g}; and at least {MIN_EDGE_OVER_PLACEBO:g} R better than the placebo.",
            "",
            f"  {'asset class':<12}{'trades':>7}{'win%':>6}{'avg R':>8}{'t':>7}{'PF':>6} | {'oos n':>6}{'oos R':>8}{'oos t':>7} | "
            f"{'placebo R':>10}  verdict",
        ]
        for r in self.rows:
            pf = f"{r.real.profit_factor:.2f}" if r.real.profit_factor is not None else "-"
            lines.append(
                f"  {r.group:<12}{r.real.n:>7}{r.real.win_pct:>6.1f}{r.real.avg_r:>+8.3f}{r.real.t:>+7.2f}{pf:>6} | "
                f"{r.real.n_oos:>6}{r.real.avg_r_oos:>+8.3f}{r.real.t_oos:>+7.2f} | {r.placebo.avg_r:>+10.3f}  {r.verdict}")
            for reason in r.failed:
                lines.append(f"      - {reason}")
        passed = self.passed()
        lines += ["", f"PASSED: {', '.join(r.group for r in passed) if passed else 'none'}"]
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "playbook": self.playbook, "timeframe": self.timeframe, "stop_atr": self.stop_atr,
            "target_atr": self.target_atr, "max_bars": self.max_bars,
            "rows": [{"group": r.group, "symbols": r.symbols, "verdict": r.verdict, "failed": r.failed,
                      "real": r.real.__dict__, "placebo": r.placebo.__dict__, "per_symbol": r.per_symbol}
                     for r in self.rows],
        }

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"playbook_{self.playbook}_{self.timeframe}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
        path.write_text(json.dumps(self.to_json(), indent=1), encoding="utf-8")
        return path


def run_playbook(
    playbook: Playbook, candles_by_symbol: dict[str, list], spreads: Optional[dict[str, float]] = None,
    seed: str = "0",
) -> Report:
    """Simulate the playbook on every symbol it is meant for, plus a placebo on each, and judge per class."""
    spreads = spreads or {}
    real_by_class: dict[str, list[Trade]] = {}
    placebo_by_class: dict[str, list[Trade]] = {}
    symbols_by_class: dict[str, list[str]] = {}
    per_symbol: dict[str, dict[str, tuple[int, float]]] = {}
    for symbol, candles in candles_by_symbol.items():
        group = asset_class(symbol)
        if group not in playbook.asset_classes:
            continue
        bars = Bars.from_candles(candles)
        spread = spreads.get(symbol, 0.0)
        real = simulate(symbol, bars, playbook, spread)
        rate = real.signals / real.free_bars if real.free_bars else 0.0
        fake = simulate(symbol, bars, Placebo(playbook, rate, f"{seed}:{symbol}"), spread)
        real_by_class.setdefault(group, []).extend(real.trades)
        placebo_by_class.setdefault(group, []).extend(fake.trades)
        symbols_by_class.setdefault(group, []).append(symbol)
        s = stats(real.trades)
        per_symbol.setdefault(group, {})[symbol] = (s.n, round(s.avg_r, 3))
    rows = []
    for group in sorted(real_by_class):
        real, placebo = stats(real_by_class[group]), stats(placebo_by_class[group])
        verdict, failed = judge(real, placebo)
        rows.append(Row(group, symbols_by_class[group], real, placebo, verdict, failed, per_symbol[group]))
    return Report(playbook.name, playbook.timeframe, rows, playbook.stop_atr, playbook.target_atr, playbook.max_bars)
