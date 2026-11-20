"""Study B: daily time-series momentum (TSMOM).

Rules and gate are fixed in PLAYBOOKS.md ("Study B") before any result exists. This module is a
fixed-hold returns simulator (not the stop/target playbook machine): signal at D1 close t, enter at
the open of t+1, exit at the close of t+H, costs = one spread + registered overnight drag, result in
ATR units at t. Placebo = random direction at the same entry bars. An always-long diagnostic runs
only after a PASS (cannot rescue a FAIL).
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

from .playbooks.base import atr_series
from .study import asset_class

OOS_FRACTION = 0.4
SEED_DEFAULT = "0"

# Registered costs (PLAYBOOKS.md). Swap drag is fixed for this first run even if MT5 reports swaps.
SWAP_ATR_PER_DAY = {
    "forex": 0.015,
    "metals": 0.015,
    "vol_index": 0.0,
    "step": 0.0,
    "boom_crash": 0.0,
}

# Gate — same numbers as playbooks/evaluate.py, measured in ATR units.
MIN_TRADES = 100
MIN_T = 3.0
MIN_OOS_TRADES = 40
MIN_OOS_T = 1.65
MIN_EDGE_OVER_PLACEBO = 0.05
MIN_EDGE_OVER_ALWAYS_LONG = 0.05

REAL_CLASSES = ("forex", "metals")
CONTROL_CLASSES = ("vol_index",)

# Static spreads in price units when MT5 is not available (from backtest._SPREAD_PIPS × point).
DEFAULT_SPREADS: dict[str, float] = {
    "EURUSD": 0.00012, "GBPUSD": 0.00015, "AUDUSD": 0.00015, "NZDUSD": 0.00020,
    "USDJPY": 0.015, "USDCHF": 0.00020, "USDCAD": 0.00020,
    "EURJPY": 0.020, "EURGBP": 0.00020,
    "XAUUSD": 0.30, "XAGUSD": 0.030,
    "Volatility 10 Index": 0.05, "Volatility 25 Index": 0.05,
    "Volatility 50 Index": 0.05, "Volatility 75 Index": 0.05,
    "Volatility 100 Index": 0.10,
}

FOREX_SYMBOLS = (
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD", "EURJPY", "EURGBP",
)
METALS_SYMBOLS = ("XAUUSD", "XAGUSD")
# Used only when MT5 is up; otherwise the control is seeded random walks.
VOL_SYMBOLS = (
    "Volatility 10 Index", "Volatility 25 Index", "Volatility 50 Index",
    "Volatility 75 Index", "Volatility 100 Index",
)

# Yahoo symbols when MT5 D1 is unavailable (metals = futures proxies; registered in PLAYBOOKS.md).
YAHOO_MAP: dict[str, str] = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X", "USDCHF": "USDCHF=X",
    "AUDUSD": "AUDUSD=X", "NZDUSD": "NZDUSD=X", "USDCAD": "USDCAD=X", "EURJPY": "EURJPY=X",
    "EURGBP": "EURGBP=X", "XAUUSD": "GC=F", "XAGUSD": "SI=F",
}


@dataclass(frozen=True)
class Variant:
    name: str
    lookback: int
    skip: int
    hold: int

    @property
    def warmup(self) -> int:
        return self.lookback + self.skip + 14  # ATR needs 14 bars; lookback window needs L+S


VARIANTS: tuple[Variant, ...] = (
    Variant("tsmom_12_1", lookback=252, skip=21, hold=21),
    Variant("tsmom_63", lookback=63, skip=0, hold=21),
)


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_index: int
    exit_index: int
    direction: int
    r: float          # ATR units, net of costs
    half: int         # 0 = first 60%, 1 = last 40%


@dataclass
class Stats:
    n: int = 0
    wins: int = 0
    avg_r: float = 0.0
    t: float = 0.0
    total_r: float = 0.0
    n_oos: int = 0
    avg_r_oos: float = 0.0
    t_oos: float = 0.0

    @property
    def win_pct(self) -> float:
        return self.wins / self.n * 100 if self.n else 0.0


@dataclass
class Cell:
    variant: str
    group: str
    symbols: list[str]
    real: Stats
    placebo: Stats
    always_long: Stats
    verdict: str
    failed: list[str]
    always_long_ok: Optional[bool] = None
    always_long_notes: list[str] = field(default_factory=list)
    per_symbol: dict[str, tuple[int, float]] = field(default_factory=dict)


@dataclass
class Report:
    data_source: str
    cells: list[Cell]
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [
            "Study B: D1 time-series momentum. Result in ATR units after one spread + registered overnight drag.",
            f"Data source: {self.data_source}",
            f"Gate per cell: >={MIN_TRADES} trades, avg > 0, t >= {MIN_T:g}; OOS >={MIN_OOS_TRADES}, avg > 0, "
            f"t >= {MIN_OOS_T:g}; beat random placebo by >={MIN_EDGE_OVER_PLACEBO:g} ATR. "
            f"A PASS still needs always-long edge >={MIN_EDGE_OVER_ALWAYS_LONG:g} ATR (full + OOS) to be a candidate.",
            "Forex pairs share a dollar factor — pooled t-stats overstate independence.",
            "",
            f"  {'variant':<12}{'class':<12}{'n':>6}{'win%':>6}{'avg':>8}{'t':>7} | "
            f"{'oos n':>5}{'oos':>8}{'oos t':>7} | {'plac':>8}{'A-long':>8}  verdict",
        ]
        for c in self.cells:
            al = f"{c.always_long.avg_r:+.3f}" if c.always_long.n else "   n/a"
            lines.append(
                f"  {c.variant:<12}{c.group:<12}{c.real.n:>6}{c.real.win_pct:>6.1f}"
                f"{c.real.avg_r:>+8.3f}{c.real.t:>+7.2f} | "
                f"{c.real.n_oos:>5}{c.real.avg_r_oos:>+8.3f}{c.real.t_oos:>+7.2f} | "
                f"{c.placebo.avg_r:>+8.3f}{al:>8}  {c.verdict}"
            )
            for reason in c.failed:
                lines.append(f"      - {reason}")
            if c.always_long_ok is False:
                for note in c.always_long_notes:
                    lines.append(f"      - always-long: {note}")
            elif c.always_long_ok is True:
                lines.append("      - always-long diagnostic: cleared")
        candidates = [c for c in self.cells
                      if c.verdict == "PASS" and c.group in REAL_CLASSES and c.always_long_ok]
        lines += ["", f"CANDIDATES: {', '.join(f'{c.variant}/{c.group}' for c in candidates) or 'none'}"]
        for note in self.notes:
            lines.append(f"Note: {note}")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "data_source": self.data_source,
            "notes": self.notes,
            "cells": [
                {
                    "variant": c.variant, "group": c.group, "symbols": c.symbols,
                    "verdict": c.verdict, "failed": c.failed,
                    "always_long_ok": c.always_long_ok, "always_long_notes": c.always_long_notes,
                    "real": c.real.__dict__, "placebo": c.placebo.__dict__,
                    "always_long": c.always_long.__dict__, "per_symbol": c.per_symbol,
                }
                for c in self.cells
            ],
        }

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"study_b_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
        path.write_text(json.dumps(self.to_json(), indent=1), encoding="utf-8")
        return path


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
    return Stats(
        n=len(rs), wins=sum(r > 0 for r in rs), avg_r=avg, t=t, total_r=sum(rs),
        n_oos=len(oos), avg_r_oos=avg_oos, t_oos=t_oos,
    )


def judge(real: Stats, placebo: Stats) -> tuple[str, list[str]]:
    failed = []
    if real.n < MIN_TRADES:
        failed.append(f"only {real.n} trades (need {MIN_TRADES})")
    if real.avg_r <= 0:
        failed.append(f"average {real.avg_r:+.3f} ATR is not above zero")
    if real.t < MIN_T:
        failed.append(f"t {real.t:+.2f} is below {MIN_T}")
    if real.n_oos < MIN_OOS_TRADES:
        failed.append(f"only {real.n_oos} out-of-sample trades (need {MIN_OOS_TRADES})")
    if real.avg_r_oos <= 0:
        failed.append(f"out-of-sample average {real.avg_r_oos:+.3f} ATR is not above zero")
    if real.t_oos < MIN_OOS_T:
        failed.append(f"out-of-sample t {real.t_oos:+.2f} is below {MIN_OOS_T}")
    if real.avg_r - placebo.avg_r < MIN_EDGE_OVER_PLACEBO:
        failed.append(
            f"beats the placebo by only {real.avg_r - placebo.avg_r:+.3f} ATR "
            f"(real {real.avg_r:+.3f}, placebo {placebo.avg_r:+.3f}; need {MIN_EDGE_OVER_PLACEBO})"
        )
    return ("FAIL" if failed else "PASS"), failed


def always_long_diagnostic(real: Stats, always_long: Stats) -> tuple[bool, list[str]]:
    notes = []
    if always_long.n < MIN_TRADES:
        notes.append(f"always-long only {always_long.n} trades (need {MIN_TRADES})")
    if real.avg_r - always_long.avg_r < MIN_EDGE_OVER_ALWAYS_LONG:
        notes.append(
            f"beats always-long by only {real.avg_r - always_long.avg_r:+.3f} ATR "
            f"(need {MIN_EDGE_OVER_ALWAYS_LONG})"
        )
    if always_long.n_oos < MIN_OOS_TRADES:
        notes.append(f"always-long only {always_long.n_oos} OOS trades")
    elif real.avg_r_oos - always_long.avg_r_oos < MIN_EDGE_OVER_ALWAYS_LONG:
        notes.append(
            f"OOS beats always-long by only {real.avg_r_oos - always_long.avg_r_oos:+.3f} ATR "
            f"(need {MIN_EDGE_OVER_ALWAYS_LONG})"
        )
    return (not notes), notes


def _signal_direction(closes: list[float], t: int, variant: Variant) -> Optional[int]:
    end = t - variant.skip
    start = end - variant.lookback
    if start < 0 or end < 0 or closes[start] == 0:
        return None
    ret = closes[end] / closes[start] - 1.0
    if ret > 0:
        return 1
    if ret < 0:
        return -1
    return None


def simulate_symbol(
    symbol: str,
    candles: list,
    variant: Variant,
    spread: float,
    *,
    mode: str = "rule",
    seed: str = SEED_DEFAULT,
) -> list[Trade]:
    """Replay one symbol. mode: 'rule' | 'placebo' | 'always_long'."""
    n = len(candles)
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr = atr_series(highs, lows, closes)
    split = int(n * (1 - OOS_FRACTION))
    group = asset_class(symbol)
    swap_day = SWAP_ATR_PER_DAY.get(group, 0.015)
    rng = random.Random(f"{seed}:{variant.name}:{symbol}:{mode}")
    trades: list[Trade] = []
    t = variant.warmup
    while t < n - variant.hold - 1:
        a = atr[t]
        direction = _signal_direction(closes, t, variant)
        if direction is None or not a or a <= 0:
            t += 1
            continue
        if mode == "placebo":
            direction = rng.choice((-1, 1))
        elif mode == "always_long":
            direction = 1
        # else keep rule direction
        entry_i = t + 1
        exit_i = t + variant.hold
        if exit_i >= n:
            break
        entry = opens[entry_i]
        exit_px = closes[exit_i]
        cost = (spread / a) + swap_day * variant.hold
        r = direction * (exit_px - entry) / a - cost
        trades.append(Trade(symbol, entry_i, exit_i, direction, r, int(entry_i >= split)))
        t = exit_i + 1
    return trades


def run(
    candles_by_symbol: dict[str, list],
    spreads: Optional[dict[str, float]] = None,
    *,
    seed: str = SEED_DEFAULT,
    data_source: str = "unknown",
    variants: Iterable[Variant] = VARIANTS,
) -> Report:
    spreads = spreads or {}
    notes = [
        "Forex pooled t-stats are not independent (shared USD factor).",
        f"Overnight drag: {SWAP_ATR_PER_DAY['forex']} ATR/day on forex/metals × hold; 0 on vol_index.",
    ]
    cells: list[Cell] = []
    for variant in variants:
        by_class: dict[str, dict[str, list[Trade]]] = {}
        symbols_by_class: dict[str, list[str]] = {}
        per_symbol: dict[str, dict[str, tuple[int, float]]] = {}
        for symbol, candles in candles_by_symbol.items():
            group = asset_class(symbol)
            if group not in REAL_CLASSES + CONTROL_CLASSES:
                continue
            if len(candles) < variant.warmup + variant.hold + 50:
                continue
            spread = spreads.get(symbol, DEFAULT_SPREADS.get(symbol, 0.0))
            real = simulate_symbol(symbol, candles, variant, spread, mode="rule", seed=seed)
            plac = simulate_symbol(symbol, candles, variant, spread, mode="placebo", seed=seed)
            along = simulate_symbol(symbol, candles, variant, spread, mode="always_long", seed=seed)
            by_class.setdefault(group, {"real": [], "placebo": [], "always_long": []})
            by_class[group]["real"].extend(real)
            by_class[group]["placebo"].extend(plac)
            by_class[group]["always_long"].extend(along)
            symbols_by_class.setdefault(group, []).append(symbol)
            s = stats(real)
            per_symbol.setdefault(group, {})[symbol] = (s.n, round(s.avg_r, 3))
        for group in sorted(by_class):
            real_s = stats(by_class[group]["real"])
            plac_s = stats(by_class[group]["placebo"])
            along_s = stats(by_class[group]["always_long"])
            verdict, failed = judge(real_s, plac_s)
            al_ok: Optional[bool] = None
            al_notes: list[str] = []
            if verdict == "PASS" and group in REAL_CLASSES:
                al_ok, al_notes = always_long_diagnostic(real_s, along_s)
            elif group in CONTROL_CLASSES and verdict == "PASS":
                notes.append(
                    f"CONTROL ALARM: {variant.name}/{group} PASSed the gate — treat as a machine fault, not a finding."
                )
            cells.append(Cell(
                variant=variant.name, group=group, symbols=symbols_by_class[group],
                real=real_s, placebo=plac_s, always_long=along_s,
                verdict=verdict, failed=failed,
                always_long_ok=al_ok, always_long_notes=al_notes,
                per_symbol=per_symbol.get(group, {}),
            ))
    return Report(data_source=data_source, cells=cells, notes=notes)


# ---- data loading ----------------------------------------------------------------


def random_walk_candles(n: int, seed: int, start: float = 100.0, sigma: float = 1.0) -> list:
    """Constant-vol random walk for the Volatility negative control when Deriv D1 is unavailable."""
    from types import SimpleNamespace
    from datetime import timedelta
    rng = random.Random(seed)
    price = start
    out = []
    day0 = datetime(2015, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        new = price + rng.gauss(0, sigma)
        hi = max(price, new) + abs(rng.gauss(0, sigma * 0.3))
        lo = min(price, new) - abs(rng.gauss(0, sigma * 0.3))
        out.append(SimpleNamespace(
            time=day0 + timedelta(days=i), open=price, high=hi, low=lo, close=new, volume=0,
        ))
        price = new
    return out


def fetch_yahoo_d1(symbol: str, years: int = 20) -> list:
    """Download daily bars from Yahoo Finance. Returns Candle-like namespaces oldest-first."""
    import time
    import urllib.request
    from types import SimpleNamespace

    yahoo = YAHOO_MAP.get(symbol)
    if not yahoo:
        raise KeyError(f"no Yahoo mapping for {symbol}")
    end = int(time.time())
    start = end - years * 365 * 24 * 3600
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo}"
        f"?interval=1d&period1={start}&period2={end}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    result = payload["chart"]["result"][0]
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append(SimpleNamespace(
            time=datetime.fromtimestamp(t, tz=timezone.utc),
            open=float(o), high=float(h), low=float(l), close=float(c),
            volume=int(q["volume"][i] or 0),
        ))
    return out


def load_candles_mt5(broker, symbols: list[str], bars: int = 5000) -> tuple[dict[str, list], dict[str, float], str]:
    candles, spreads = {}, {}
    for sym in symbols:
        data = broker.get_candles(sym, "D1", bars)
        if len(data) < 400:
            continue
        candles[sym] = data
        info = broker.get_symbol_info(sym)
        spreads[sym] = (info.ask - info.bid) if info else DEFAULT_SPREADS.get(sym, 0.0)
    return candles, spreads, "mt5"


def load_candles_yahoo(
    symbols: list[str],
    *,
    years: int = 20,
    include_synthetic_vol: bool = True,
    vol_bars: int = 4000,
) -> tuple[dict[str, list], dict[str, float], str]:
    candles, spreads = {}, {}
    for sym in symbols:
        if asset_class(sym) == "vol_index":
            continue
        if sym not in YAHOO_MAP:
            continue
        data = fetch_yahoo_d1(sym, years=years)
        if len(data) < 400:
            continue
        candles[sym] = data
        spreads[sym] = DEFAULT_SPREADS.get(sym, 0.0)
    if include_synthetic_vol:
        for i, name in enumerate(VOL_SYMBOLS):
            candles[name] = random_walk_candles(vol_bars, seed=1000 + i)
            spreads[name] = DEFAULT_SPREADS.get(name, 0.05)
    return candles, spreads, "yahoo+synthetic_vol_control"
