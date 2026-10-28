"""Study A: do equal-level sweeps reverse at particular times of day?

Rules and gate are fixed in PLAYBOOKS.md ("Study A") before any result exists. The event is the sweep
candle itself, as `geometry.analyze` reports it (the same view the model gets from get_structure):
a wick through an equal high or low with a close back inside, on bar t. A swept low implies up, a
swept high implies down. The outcome is the move from the open of bar t+1 to the close of bar t+h in
ATRs at t, judged against the same symbol's average move in the SAME time slot and half of the
history, so a slot's own drift is not mistaken for skill.

Guards, as in the signal study: no lookahead (analysis ends at t, outcome starts at t+1),
de-clustering, an out-of-sample half, a random placebo through the same pipeline, and volatility
indices as a negative control.
"""
from __future__ import annotations

import json
import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .geometry import analyze
from .playbooks.base import atr_series
from .study import asset_class

HORIZONS = (4, 12)
WINDOW = 300
DECLUSTER = 12            # after a kept event on a symbol, events within this many bars are ignored
OOS_FRACTION = 0.4
PLACEBO_RATE = 0.10

# UTC hour ranges [start, end)
SLOTS = (("asian", 0, 7), ("london", 7, 12), ("new_york", 12, 21), ("late", 21, 24))
REAL_CLASSES = ("forex", "metals")
CONTROL_CLASSES = ("vol_index",)

MIN_EVENTS = 100
MIN_T = 3.0
MIN_OOS_EVENTS = 40
MIN_OOS_T = 1.65


def slot_of(when: Optional[datetime]) -> Optional[str]:
    if when is None:
        return None
    for name, start, end in SLOTS:
        if start <= when.hour < end:
            return name
    return None


@dataclass
class Event:
    symbol: str
    index: int
    half: int                      # 0 = first 60% of the history, 1 = last 40%
    slot: str
    direction: int                 # +1 up (a low was swept), -1 down (a high was swept)
    fwd: dict[int, float]          # horizon -> move in ATRs, next open to close h bars later
    cost_atr: float                # the spread as a share of the ATR at t


@dataclass
class Baseline:
    """Per (half, slot, horizon): sum of forward moves, how many, and how many were up."""
    total: dict = field(default_factory=dict)
    count: dict = field(default_factory=dict)
    ups: dict = field(default_factory=dict)

    def add(self, half: int, slot: str, h: int, move: float) -> None:
        key = (half, slot, h)
        self.total[key] = self.total.get(key, 0.0) + move
        self.count[key] = self.count.get(key, 0) + 1
        self.ups[key] = self.ups.get(key, 0) + (move > 0)

    def mean(self, half: int, slot: str, h: int) -> float:
        key = (half, slot, h)
        return self.total[key] / self.count[key]

    def p_up(self, half: int, slot: str, h: int) -> float:
        key = (half, slot, h)
        return self.ups[key] / self.count[key]


@dataclass
class SymbolData:
    symbol: str
    events: list[Event]
    placebo: list[Event]
    baseline: Baseline


def collect(symbol: str, candles: list, spread: float = 0.0, window: int = WINDOW,
            horizons: tuple[int, ...] = HORIZONS, seed: str = "0") -> SymbolData:
    """Walk one symbol's history bar by bar, recording sweep events, placebo events and the slot baseline."""
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr = atr_series(highs, lows, closes)
    n = len(candles)
    last_t = n - 1 - max(horizons)
    split = int(n * (1 - OOS_FRACTION))
    rng = random.Random(f"{seed}:{symbol}")
    baseline = Baseline()
    events: list[Event] = []
    placebo: list[Event] = []
    last_kept = {"real": -10**9, "placebo": -10**9}

    for t in range(window - 1, last_t + 1):
        slot = slot_of(candles[t].time)
        a = atr[t]
        if slot is None or not a:
            continue
        half = int(t >= split)
        entry = opens[t + 1]
        fwd = {h: (closes[t + h] - entry) / a for h in horizons}
        for h in horizons:
            baseline.add(half, slot, h, fwd[h])
        cost = spread / a

        if rng.random() < PLACEBO_RATE and t - last_kept["placebo"] >= DECLUSTER:
            last_kept["placebo"] = t
            placebo.append(Event(symbol, t, half, slot, rng.choice((-1, 1)), fwd, cost))

        lo = t - window + 1
        found = analyze(highs[lo:t + 1], lows[lo:t + 1], closes[lo:t + 1])
        sweep = found.get("recent_sweep") if "error" not in found else None
        if (sweep and sweep["bars_ago"] == 0 and sweep["source"] == "equal"
                and t - last_kept["real"] >= DECLUSTER):
            last_kept["real"] = t
            direction = 1 if sweep["direction"] == "bullish" else -1
            events.append(Event(symbol, t, half, slot, direction, fwd, cost))
    return SymbolData(symbol, events, placebo, baseline)


# ---- evaluation ------------------------------------------------------------------


def _t(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    if len(values) < 2:
        return values[0], 0.0
    mean, sd = statistics.fmean(values), statistics.stdev(values)
    return mean, (mean / (sd / math.sqrt(len(values))) if sd > 0 else 0.0)


@dataclass
class Row:
    kind: str                      # real | control | placebo
    group: str
    slot: str
    horizon: int
    n: int = 0
    mean: float = 0.0              # excess move in ATRs, in the event's direction
    t: float = 0.0
    hit: float = 0.0
    base_hit: float = 0.0
    n_oos: int = 0
    mean_oos: float = 0.0
    t_oos: float = 0.0
    cost_atr: float = 0.0
    verdict: str = "none"


def _verdict(r: Row) -> str:
    if r.kind == "placebo":
        return "placebo" if not _clears(r) else "PLACEBO CLEARS GATE (study broken)"
    if _clears(r):
        return "CANDIDATE" if r.kind == "real" else "CONTROL CLEARS GATE (study broken)"
    if r.n >= MIN_EVENTS and r.t <= -MIN_T and r.n_oos >= MIN_OOS_EVENTS and r.mean_oos < 0 and r.t_oos <= -MIN_OOS_T:
        return "hint: continues"
    return "none"


def _clears(r: Row) -> bool:
    return (r.n >= MIN_EVENTS and r.mean > 0 and r.t >= MIN_T
            and r.n_oos >= MIN_OOS_EVENTS and r.mean_oos > 0 and r.t_oos >= MIN_OOS_T
            and r.mean > r.cost_atr)


def make_row(kind: str, group: str, slot: str, h: int, evs: list[Event], baselines: dict[str, Baseline]) -> Row:
    excess, hits, base_hits, oos = [], [], [], []
    for e in evs:
        b = baselines[e.symbol]
        x = e.direction * (e.fwd[h] - b.mean(e.half, e.slot, h))
        excess.append(x)
        hits.append(e.direction * e.fwd[h] > 0)
        p = b.p_up(e.half, e.slot, h)
        base_hits.append(p if e.direction > 0 else 1 - p)
        if e.half == 1:
            oos.append(x)
    mean, t = _t(excess)
    mean_oos, t_oos = _t(oos)
    row = Row(
        kind=kind, group=group, slot=slot, horizon=h, n=len(evs), mean=mean, t=t,
        hit=sum(hits) / len(hits) if hits else 0.0,
        base_hit=statistics.fmean(base_hits) if base_hits else 0.0,
        n_oos=len(oos), mean_oos=mean_oos, t_oos=t_oos,
        cost_atr=statistics.fmean(e.cost_atr for e in evs) if evs else 0.0,
    )
    row.verdict = _verdict(row)
    return row


def evaluate(data: list[SymbolData], horizons: tuple[int, ...] = HORIZONS) -> list[Row]:
    baselines = {d.symbol: d.baseline for d in data}
    rows: list[Row] = []
    for group in REAL_CLASSES + CONTROL_CLASSES:
        members = [d for d in data if asset_class(d.symbol) == group]
        if not members:
            continue
        kind = "real" if group in REAL_CLASSES else "control"
        for slot, _, _ in SLOTS:
            for h in horizons:
                real = [e for d in members for e in d.events if e.slot == slot]
                fake = [e for d in members for e in d.placebo if e.slot == slot]
                rows.append(make_row(kind, group, slot, h, real, baselines))
                rows.append(make_row("placebo", group, slot, h, fake, baselines))
    return rows


@dataclass
class Study:
    rows: list[Row]
    symbols: list[str]
    bars: dict[str, int] = field(default_factory=dict)

    @property
    def tests(self) -> int:
        return sum(1 for r in self.rows if r.kind != "placebo")

    def text(self) -> str:
        real = [r for r in self.rows if r.kind == "real"]
        control = [r for r in self.rows if r.kind == "control"]
        placebo = [r for r in self.rows if r.kind == "placebo"]
        lines = [
            f"Study A: equal-level sweeps by time of day, H1, {len(self.symbols)} symbols, horizons {list(HORIZONS)} bars.",
            f"{self.tests} cells ({len(real)} real, {len(control)} control) plus {len(placebo)} placebo cells.",
            f"Gate: n>={MIN_EVENTS}, excess>0 with t>={MIN_T}, out of sample n>={MIN_OOS_EVENTS} with excess>0 and t>={MIN_OOS_T}, "
            "and excess above one spread (cost, in ATRs).",
            "Excess is in ATRs in the sweep's direction (low swept = up), minus that symbol's average move in the same slot and half.",
            "",
        ]
        for title, rows in (("REAL", real), ("CONTROL (Volatility indices, should fail)", control), ("PLACEBO (random events, should fail)", placebo)):
            lines += [title, _HEADER] + [_fmt(r) for r in rows] + [""]
        cands = [r for r in real if r.verdict == "CANDIDATE"]
        lines.append(f"CANDIDATES: {len(cands)}")
        lines += [_fmt(r) for r in cands] or ["  none"]
        broken = [r for r in control + placebo if "CLEARS" in r.verdict]
        if broken:
            lines.append("STUDY BROKEN: a control or placebo cell cleared the gate; nothing here can be acted on.")
        hints = [r for r in real if r.verdict.startswith("hint")]
        if hints:
            lines += ["Hints (sweeps continue; need their own registered test):"] + [_fmt(r) for r in hints]
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {"symbols": self.symbols, "bars": self.bars, "tests": self.tests, "rows": [r.__dict__ for r in self.rows]}

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"timing_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
        path.write_text(json.dumps(self.to_json(), indent=1), encoding="utf-8")
        return path


_HEADER = (f"  {'group':<10}{'slot':<9}{'h':>3} {'n':>5} {'excess':>7} {'t':>6} {'hit%':>5} {'base%':>6} "
           f"{'cost':>5} | {'n_oos':>5} {'excess':>7} {'t':>6}  verdict")


def _fmt(r: Row) -> str:
    return (f"  {r.group:<10}{r.slot:<9}{r.horizon:>3} {r.n:>5} {r.mean:>+7.3f} {r.t:>+6.2f} {r.hit * 100:5.1f} "
            f"{r.base_hit * 100:6.1f} {r.cost_atr:5.2f} | {r.n_oos:>5} {r.mean_oos:>+7.3f} {r.t_oos:>+6.2f}  {r.verdict}")


def run(candles_by_symbol: dict[str, list], spreads: Optional[dict[str, float]] = None) -> Study:
    spreads = spreads or {}
    data = [collect(sym, candles, spreads.get(sym, 0.0)) for sym, candles in candles_by_symbol.items()]
    return Study(rows=evaluate(data), symbols=list(candles_by_symbol),
                 bars={s: len(c) for s, c in candles_by_symbol.items()})
