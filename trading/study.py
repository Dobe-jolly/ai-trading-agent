"""Do regime, sweeps, gaps and structure breaks predict what price does next?

For each symbol the study walks through history. At bar t it shows `analyze()` only the
candles up to t (the same function the model's get_structure tool uses), records which
signals are on, then measures what happened next: entry at the open of bar t+1, exit at the
close of bar t+N, divided by the ATR at t so symbols are comparable.

What guards against fooling ourselves:
  - No lookahead: the analysis window ends at t, the outcome starts at t+1.
  - Baseline: a signal is judged against the same symbol's average move over the same
    period, so a trending or drifting market doesn't look like skill.
  - De-clustering: after a signal fires on a symbol, it is ignored for the longest horizon,
    so one event isn't counted many times.
  - Out-of-sample: the last 40% of each history is scored separately. A real effect should
    point the same way there.
  - Placebo: events at random bars with random directions go through the same pipeline. If
    the placebo looks significant, the study is broken, not the market.
  - Many tests are run, so some will look good by chance. The report says how many.
"""
from __future__ import annotations

import json
import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .geometry import analyze

HORIZONS = (4, 12, 24)
WINDOW = 300
STEP = 2
SWEEP_MAX_AGE = 2        # a sweep or break counts as "on" for this many bars after it happens
OOS_FRACTION = 0.4       # the last 40% of each history is the out-of-sample half
PLACEBO_RATE = 0.10

# A cell is a candidate only if it clears all of these.
MIN_EVENTS = 60
MIN_T = 3.0
MIN_OOS_EVENTS = 20
MIN_OOS_T = 1.65


# ---- asset classes ---------------------------------------------------------------


def asset_class(symbol: str) -> str:
    if symbol.startswith(("Boom", "Crash")):
        return "boom_crash"
    if symbol.startswith("Step"):
        return "step"
    if symbol.startswith("Volatility"):
        return "vol_index"
    if symbol.startswith(("XAU", "XAG")):
        return "metals"
    return "forex"


# ---- observations ----------------------------------------------------------------


@dataclass
class Obs:
    symbol: str
    index: int
    half: int                          # 0 = first 60% of the history, 1 = last 40%
    signals: dict[str, int]            # signal name -> implied direction (+1 up, -1 down, 0 none)
    fwd: dict[int, float]              # horizon -> move in ATRs, next open to close N bars later


def signals_from(a: dict, rng: random.Random) -> dict[str, int]:
    """Which signals are on, and the direction each one implies, from an analyze() result."""
    out: dict[str, int] = {}
    regime = a["regime"]
    if regime == "BULLISH_TREND":
        out["regime:bullish"] = 1
    elif regime == "BEARISH_TREND":
        out["regime:bearish"] = -1
    elif regime == "RANGE":
        out["regime:range"] = 0

    sweep = a["recent_sweep"]
    if sweep and sweep["bars_ago"] <= SWEEP_MAX_AGE:
        d = 1 if sweep["direction"] == "bullish" else -1
        out[f"sweep:{sweep['direction']}"] = d
        if sweep["source"] == "equal":
            out[f"sweep_equal:{sweep['direction']}"] = d
        if regime == "BULLISH_TREND":
            out[f"sweep_{'with' if d == 1 else 'against'}_trend:{sweep['direction']}"] = d
        elif regime == "BEARISH_TREND":
            out[f"sweep_{'with' if d == -1 else 'against'}_trend:{sweep['direction']}"] = d

    if a["fvg_zone_active"] and a["fvg_direction"]:
        out[f"fvg:{a['fvg_direction']}"] = 1 if a["fvg_direction"] == "bullish" else -1

    brk = a["last_break"]
    if brk and brk["bars_ago"] <= SWEEP_MAX_AGE:
        out[f"{brk['type'].lower()}:{brk['direction']}"] = 1 if brk["direction"] == "up" else -1

    if rng.random() < PLACEBO_RATE:
        out["placebo:random"] = rng.choice((-1, 1))
    return out


def collect(
    symbol: str, candles: list, window: int = WINDOW, step: int = STEP,
    horizons: Iterable[int] = HORIZONS, seed: int = 0,
) -> list[Obs]:
    """Walk through one symbol's history, recording signals at bar t and the outcome after it."""
    horizons = tuple(horizons)
    opens = [c.open for c in candles]
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    last_t = len(candles) - 1 - max(horizons)
    split = int(len(candles) * (1 - OOS_FRACTION))
    rng = random.Random(f"{seed}:{symbol}")
    obs: list[Obs] = []
    for t in range(window - 1, last_t + 1, step):
        lo = t - window + 1
        a = analyze(highs[lo:t + 1], lows[lo:t + 1], closes[lo:t + 1])
        if "error" in a or not a["atr"]:
            continue
        entry = opens[t + 1]
        obs.append(Obs(
            symbol=symbol, index=t, half=int(t >= split),
            signals=signals_from(a, rng),
            fwd={h: (closes[t + h] - entry) / a["atr"] for h in horizons},
        ))
    return obs


# ---- evaluation ------------------------------------------------------------------


@dataclass
class Row:
    group: str
    signal: str
    horizon: int
    direction: int
    n: int = 0
    mean: float = 0.0          # excess move in ATRs, in the signal's direction (abs move if no direction)
    t: float = 0.0
    hit: Optional[float] = None
    base_hit: Optional[float] = None
    n_oos: int = 0
    mean_oos: float = 0.0
    t_oos: float = 0.0
    verdict: str = "none"


def _baselines(obs: list[Obs], horizons: tuple[int, ...]) -> dict:
    """Per (symbol, half, horizon): average move, share of up moves, average absolute move."""
    cells: dict = {}
    for o in obs:
        for h in horizons:
            cells.setdefault((o.symbol, o.half, h), []).append(o.fwd[h])
    return {
        key: (statistics.fmean(v), sum(x > 0 for x in v) / len(v), statistics.fmean(abs(x) for x in v))
        for key, v in cells.items()
    }


def _declustered(obs: list[Obs], gap: int) -> dict:
    """signal -> its events, skipping any within `gap` bars of the last kept one on that symbol."""
    by_symbol: dict[str, list[Obs]] = {}
    for o in obs:
        by_symbol.setdefault(o.symbol, []).append(o)
    events: dict[str, list[Obs]] = {}
    for rows in by_symbol.values():
        last: dict[str, int] = {}
        for o in sorted(rows, key=lambda r: r.index):
            for name in o.signals:
                if name in last and o.index - last[name] < gap:
                    continue
                last[name] = o.index
                events.setdefault(name, []).append(o)
    return events


def _t(values: list[float]) -> tuple[float, float]:
    if len(values) < 2:
        return (values[0] if values else 0.0), 0.0
    mean, sd = statistics.fmean(values), statistics.stdev(values)
    return mean, (mean / (sd / math.sqrt(len(values))) if sd > 0 else 0.0)


def evaluate(obs: list[Obs], horizons: Iterable[int] = HORIZONS) -> list[Row]:
    horizons = tuple(horizons)
    base = _baselines(obs, horizons)
    events = _declustered(obs, gap=max(horizons))
    rows: list[Row] = []
    for name, evs in events.items():
        for group in sorted({asset_class(e.symbol) for e in evs}):
            in_group = [e for e in evs if asset_class(e.symbol) == group]
            directions = {e.signals[name] for e in in_group}
            if not name.startswith("placebo") and len(directions) != 1:
                raise ValueError(f"signal {name!r} implied different directions: {directions}")
            d = 0 if name.startswith("placebo") else directions.pop()
            for h in horizons:
                rows.append(_row(group, name, h, d, in_group, base))
    for r in rows:
        r.verdict = _verdict(r)
    return rows


def _excess(o: Obs, name: str, h: int, base: dict) -> tuple[float, Optional[bool], Optional[float]]:
    """(excess move in ATRs, did it go the signal's way, how often that happens anyway)."""
    mean, p_up, mean_abs = base[(o.symbol, o.half, h)]
    d = o.signals[name]
    if d == 0:   # no direction (range regime): did price move more than usual?
        return abs(o.fwd[h]) - mean_abs, None, None
    return d * (o.fwd[h] - mean), d * o.fwd[h] > 0, (p_up if d > 0 else 1 - p_up)


def _row(group: str, name: str, h: int, d: int, evs: list[Obs], base: dict) -> Row:
    scored = [_excess(e, name, h, base) for e in evs]
    mean, t = _t([s[0] for s in scored])
    oos = [s[0] for s, e in zip(scored, evs) if e.half == 1]
    mean_oos, t_oos = _t(oos)
    hits = [s[1] for s in scored if s[1] is not None]
    return Row(
        group=group, signal=name, horizon=h, direction=d, n=len(evs), mean=mean, t=t,
        hit=sum(hits) / len(hits) if hits else None,
        base_hit=statistics.fmean(s[2] for s in scored if s[2] is not None) if hits else None,
        n_oos=len(oos), mean_oos=mean_oos, t_oos=t_oos,
    )


def _verdict(r: Row) -> str:
    if r.signal.startswith("placebo"):
        return "placebo"
    if (r.n >= MIN_EVENTS and abs(r.t) >= MIN_T and r.n_oos >= MIN_OOS_EVENTS
            and r.mean * r.mean_oos > 0 and abs(r.t_oos) >= MIN_OOS_T):
        return "CANDIDATE"
    return "none"


# ---- report ----------------------------------------------------------------------


@dataclass
class Study:
    timeframe: str
    horizons: tuple[int, ...]
    rows: list[Row]
    symbols: list[str]
    bars: dict[str, int] = field(default_factory=dict)
    cost_atr: dict[str, float] = field(default_factory=dict)

    @property
    def tests(self) -> int:
        return sum(1 for r in self.rows if r.verdict != "placebo")

    def expected_false_gate_passes(self) -> float:
        """Rows expected to clear the |t| gate by luck alone, if nothing is real (two-sided normal)."""
        return self.tests * math.erfc(MIN_T / math.sqrt(2))

    def text(self, top: int = 20) -> str:
        real = [r for r in self.rows if r.verdict != "placebo"]
        placebo = sorted((r for r in self.rows if r.verdict == "placebo"), key=lambda r: -abs(r.t))
        lines = [
            f"Signal study: {self.timeframe}, {len(self.symbols)} symbols, horizons {list(self.horizons)} bars, "
            f"{self.tests} tests",
            f"Gate for CANDIDATE: n>={MIN_EVENTS}, |t|>={MIN_T}, out-of-sample same sign with |t|>={MIN_OOS_T}.",
            f"By luck alone about {self.expected_false_gate_passes():.1f} rows would pass the |t| gate "
            "(fewer after the out-of-sample check).",
            "Excess is in ATRs, in the signal's direction, after subtracting that symbol's average move. "
            "Negative means fading the signal would have done better.",
            "t-stats are generous: symbols in a group move together, so events are less independent than "
            "they look. The baseline is the average over all bars, signal bars included, so a signal that "
            "is on most of the time (a regime) has its excess pulled toward zero.",
            "",
        ]
        if self.cost_atr:
            costs = sorted(self.cost_atr.items(), key=lambda kv: -kv[1])
            lines.append(
                f"Spread as a share of one ATR (an edge must beat this): median "
                f"{statistics.median(self.cost_atr.values()):.2f}; costliest: "
                + ", ".join(f"{s} {c:.2f}" for s, c in costs[:5]))
            lines.append("")
        cands = [r for r in real if r.verdict == "CANDIDATE"]
        lines.append(f"CANDIDATES: {len(cands)}")
        lines += [_fmt(r) for r in sorted(cands, key=lambda r: -abs(r.t))] or ["  none"]
        lines += ["", f"Strongest {top} by |t| (candidate or not):", _HEADER]
        lines += [_fmt(r) for r in sorted(real, key=lambda r: -abs(r.t))[:top]]
        lines += ["", "Placebo (random events, random direction); these should look like noise:", _HEADER]
        lines += [_fmt(r) for r in placebo[:4]]
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "timeframe": self.timeframe, "horizons": list(self.horizons), "symbols": self.symbols,
            "bars": self.bars, "cost_atr": self.cost_atr, "tests": self.tests,
            "expected_false_gate_passes": self.expected_false_gate_passes(),
            "rows": [r.__dict__ for r in self.rows],
        }

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"study_{self.timeframe}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
        path.write_text(json.dumps(self.to_json(), indent=1), encoding="utf-8")
        return path


_HEADER = (f"  {'group':<11}{'signal':<28}{'h':>3} {'n':>5} {'excess':>7} {'t':>6} "
           f"{'hit%':>5} {'base%':>6} | {'n_oos':>5} {'excess':>7} {'t':>6}  verdict")


def _fmt(r: Row) -> str:
    hit = f"{r.hit * 100:5.1f} {r.base_hit * 100:6.1f}" if r.hit is not None else f"{'':>5} {'':>6}"
    return (f"  {r.group:<11}{r.signal:<28}{r.horizon:>3} {r.n:>5} {r.mean:>+7.3f} {r.t:>+6.2f} {hit} | "
            f"{r.n_oos:>5} {r.mean_oos:>+7.3f} {r.t_oos:>+6.2f}  {r.verdict}")


def run(
    candles_by_symbol: dict[str, list], timeframe: str, horizons: Iterable[int] = HORIZONS,
    window: int = WINDOW, step: int = STEP, cost_atr: Optional[dict[str, float]] = None,
) -> Study:
    horizons = tuple(horizons)
    obs: list[Obs] = []
    for symbol, candles in candles_by_symbol.items():
        obs += collect(symbol, candles, window, step, horizons)
    return Study(
        timeframe=timeframe, horizons=horizons, rows=evaluate(obs, horizons),
        symbols=list(candles_by_symbol), bars={s: len(c) for s, c in candles_by_symbol.items()},
        cost_atr=cost_atr or {},
    )
