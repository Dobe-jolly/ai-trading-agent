"""Study D: vol-targeted trend on liquid futures.

Rules and gate are fixed in PLAYBOOKS.md ("Study D") before any result exists. Fixed-hold ATR-unit
simulator: 12-1 TSMOM sign, size = min(2, 0.10/σ_63_ann), hold 21. Jump-stops N/A (no stop).
First-pass gate requires beating a random-direction placebo (same weights) and an always-long sleeve
(same weights).
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

from .playbooks.base import atr_series

OOS_FRACTION = 0.4
SEED_DEFAULT = "0"

LOOKBACK = 252
SKIP = 21
HOLD = 21
VOL_WINDOW = 63
TARGET_VOL = 0.10
MAX_WEIGHT = 2.0
FUTURES_FRICTION_ATR = 0.05
WARMUP = LOOKBACK + SKIP + max(14, VOL_WINDOW)

MIN_TRADES = 100
MIN_T = 3.0
MIN_OOS_TRADES = 40
MIN_OOS_T = 1.65
MIN_EDGE = 0.05

# Registered Yahoo continuous futures universe (PLAYBOOKS.md).
UNIVERSE: tuple[tuple[str, str], ...] = (
    ("ES", "ES=F"),
    ("NQ", "NQ=F"),
    ("ZN", "ZN=F"),
    ("CL", "CL=F"),
    ("GC", "GC=F"),
    ("SI", "SI=F"),
    ("HG", "HG=F"),
    ("6E", "6E=F"),
    ("6J", "6J=F"),
    ("ZC", "ZC=F"),
)

DEFAULT_SPREADS: dict[str, float] = {
    "ES": 0.25,
    "NQ": 0.50,
    "ZN": 0.015625,
    "CL": 0.02,
    "GC": 0.10,
    "SI": 0.005,
    "HG": 0.0005,
    "6E": 0.00005,
    "6J": 0.0000005,
    "ZC": 0.25,
}


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_index: int
    exit_index: int
    direction: int
    weight: float
    r: float
    half: int


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
class Report:
    data_source: str
    real: Stats
    placebo: Stats
    always_long: Stats
    verdict: str
    failed: list[str]
    symbols: list[str]
    per_symbol: dict[str, tuple[int, float]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [
            "Study D: vol-targeted futures trend (12-1 sign, w=min(2, 0.10/σ63), hold 21, ATR units).",
            f"Data source: {self.data_source}",
            f"Gate: >={MIN_TRADES} trades, avg > 0, t >= {MIN_T:g}; OOS >={MIN_OOS_TRADES}, avg > 0, "
            f"t >= {MIN_OOS_T:g}; beat random-direction placebo by >={MIN_EDGE:g} ATR; "
            f"beat always-long (same weights) by >={MIN_EDGE:g} ATR (full + OOS).",
            "Jump-stops: N/A (no stop). Drift analogue: always-long with same vol weights in the first gate.",
            "",
            f"  {'sleeve':<14}{'n':>6}{'win%':>6}{'avg':>8}{'t':>7} | {'oos n':>5}{'oos':>8}{'oos t':>7}",
            f"  {'vol_tsmom_12_1':<14}{self.real.n:>6}{self.real.win_pct:>6.1f}"
            f"{self.real.avg_r:>+8.3f}{self.real.t:>+7.2f} | "
            f"{self.real.n_oos:>5}{self.real.avg_r_oos:>+8.3f}{self.real.t_oos:>+7.2f}",
            f"  {'random_dir':<14}{self.placebo.n:>6}{self.placebo.win_pct:>6.1f}"
            f"{self.placebo.avg_r:>+8.3f}{self.placebo.t:>+7.2f} | "
            f"{self.placebo.n_oos:>5}{self.placebo.avg_r_oos:>+8.3f}{self.placebo.t_oos:>+7.2f}",
            f"  {'always_long':<14}{self.always_long.n:>6}{self.always_long.win_pct:>6.1f}"
            f"{self.always_long.avg_r:>+8.3f}{self.always_long.t:>+7.2f} | "
            f"{self.always_long.n_oos:>5}{self.always_long.avg_r_oos:>+8.3f}{self.always_long.t_oos:>+7.2f}",
            "",
            f"VERDICT: {self.verdict}",
        ]
        for f in self.failed:
            lines.append(f"  - {f}")
        for note in self.notes:
            lines.append(f"Note: {note}")
        if self.per_symbol:
            lines.append("Per symbol (n, avg ATR): " + ", ".join(
                f"{s}={n}/{a:+.3f}" for s, (n, a) in sorted(self.per_symbol.items())
            ))
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "data_source": self.data_source, "verdict": self.verdict, "failed": self.failed,
            "notes": self.notes, "symbols": self.symbols, "per_symbol": self.per_symbol,
            "real": self.real.__dict__, "placebo": self.placebo.__dict__,
            "always_long": self.always_long.__dict__,
            "params": {
                "lookback": LOOKBACK, "skip": SKIP, "hold": HOLD, "vol_window": VOL_WINDOW,
                "target_vol": TARGET_VOL, "max_weight": MAX_WEIGHT,
                "futures_friction_atr": FUTURES_FRICTION_ATR,
            },
        }

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"study_vol_futures_trend_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
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


def stats_of(trades: list[Trade]) -> Stats:
    rs = [t.r for t in trades]
    oos = [t.r for t in trades if t.half == 1]
    avg, t = _mean_t(rs)
    avg_oos, t_oos = _mean_t(oos)
    return Stats(
        n=len(rs), wins=sum(r > 0 for r in rs), avg_r=avg, t=t, total_r=sum(rs),
        n_oos=len(oos), avg_r_oos=avg_oos, t_oos=t_oos,
    )


def judge(real: Stats, placebo: Stats, always_long: Stats) -> tuple[str, list[str]]:
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
    if real.avg_r - placebo.avg_r < MIN_EDGE:
        failed.append(
            f"beats random-direction placebo by only {real.avg_r - placebo.avg_r:+.3f} ATR "
            f"(real {real.avg_r:+.3f}, placebo {placebo.avg_r:+.3f}; need {MIN_EDGE})"
        )
    if real.avg_r - always_long.avg_r < MIN_EDGE:
        failed.append(
            f"beats always-long by only {real.avg_r - always_long.avg_r:+.3f} ATR "
            f"(real {real.avg_r:+.3f}, always-long {always_long.avg_r:+.3f}; need {MIN_EDGE})"
        )
    if always_long.n_oos >= MIN_OOS_TRADES and real.avg_r_oos - always_long.avg_r_oos < MIN_EDGE:
        failed.append(
            f"OOS beats always-long by only {real.avg_r_oos - always_long.avg_r_oos:+.3f} ATR "
            f"(need {MIN_EDGE})"
        )
    elif always_long.n_oos < MIN_OOS_TRADES:
        failed.append(f"always-long only {always_long.n_oos} OOS trades (need {MIN_OOS_TRADES})")
    return ("FAIL" if failed else "PASS"), failed


def _ann_vol(closes: list[float], t: int, window: int = VOL_WINDOW) -> Optional[float]:
    if t + 1 < window + 1:
        return None
    rets = []
    for i in range(t - window + 1, t + 1):
        prev, cur = closes[i - 1], closes[i]
        if prev <= 0 or cur <= 0:
            return None
        rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return None
    sd = statistics.stdev(rets)
    return sd * math.sqrt(252)


def _signal_direction(closes: list[float], t: int) -> Optional[int]:
    i1 = t - SKIP
    i0 = t - SKIP - LOOKBACK
    if i0 < 0 or closes[i0] <= 0:
        return None
    ret = closes[i1] / closes[i0] - 1.0
    if ret > 0:
        return 1
    if ret < 0:
        return -1
    return None


def _cost(spread: float, atr: float, weight: float) -> float:
    return weight * (spread / atr) + FUTURES_FRICTION_ATR


def simulate_symbol(
    symbol: str,
    candles: list,
    spread: float,
    *,
    mode: str = "trend",
    seed: str = SEED_DEFAULT,
) -> list[Trade]:
    """mode: 'trend' | 'always_long' | 'placebo' (random direction, same entry bars / weights)."""
    n = len(candles)
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr = atr_series(highs, lows, closes)
    split = int(n * (1 - OOS_FRACTION))
    rng = random.Random(f"{seed}:{symbol}:{mode}")
    trades: list[Trade] = []
    t = WARMUP
    while t < n - HOLD - 1:
        a = atr[t]
        if not a or a <= 0:
            t += 1
            continue
        sigma = _ann_vol(closes, t)
        if sigma is None or sigma <= 1e-8:
            t += 1
            continue
        weight = min(MAX_WEIGHT, TARGET_VOL / sigma)
        direction: Optional[int] = None
        if mode == "trend":
            direction = _signal_direction(closes, t)
        elif mode == "always_long":
            direction = 1
        elif mode == "placebo":
            # Same entry calendar as the rule: only trade when the rule would have a signal.
            if _signal_direction(closes, t) is None:
                t += 1
                continue
            direction = 1 if rng.random() < 0.5 else -1
        else:
            raise ValueError(mode)
        if direction is None:
            t += 1
            continue
        entry_i = t + 1
        exit_i = t + HOLD
        if exit_i >= n:
            break
        r = direction * weight * (closes[exit_i] - opens[entry_i]) / a - _cost(spread, a, weight)
        trades.append(Trade(symbol, entry_i, exit_i, direction, weight, r, int(entry_i >= split)))
        t = exit_i + 1
    return trades


def run(
    candles_by_symbol: dict[str, list],
    spreads: Optional[dict[str, float]] = None,
    *,
    seed: str = SEED_DEFAULT,
    data_source: str = "unknown",
) -> Report:
    spreads = spreads or {}
    real_all: list[Trade] = []
    plac_all: list[Trade] = []
    along_all: list[Trade] = []
    symbols: list[str] = []
    per_symbol: dict[str, tuple[int, float]] = {}
    for symbol, candles in candles_by_symbol.items():
        if len(candles) < WARMUP + HOLD + 50:
            continue
        spread = spreads.get(symbol, DEFAULT_SPREADS.get(symbol, 0.0))
        real = simulate_symbol(symbol, candles, spread, mode="trend", seed=seed)
        plac = simulate_symbol(symbol, candles, spread, mode="placebo", seed=seed)
        along = simulate_symbol(symbol, candles, spread, mode="always_long", seed=seed)
        real_all.extend(real)
        plac_all.extend(plac)
        along_all.extend(along)
        symbols.append(symbol)
        s = stats_of(real)
        per_symbol[symbol] = (s.n, round(s.avg_r, 3))
    real_s, plac_s, along_s = stats_of(real_all), stats_of(plac_all), stats_of(along_all)
    verdict, failed = judge(real_s, plac_s, along_s)
    return Report(
        data_source=data_source, real=real_s, placebo=plac_s, always_long=along_s,
        verdict=verdict, failed=failed, symbols=symbols, per_symbol=per_symbol,
        notes=[
            "Vol-targeted 12-1 TSMOM on Yahoo continuous futures proxies.",
            f"Friction {FUTURES_FRICTION_ATR} ATR/trade + weight×spread/ATR; no FX overnight swap.",
            "Always-long uses the same vol weights (risk-on drift analogue in the first gate).",
            "Pooled futures cell — contracts share macro factors; pooled t overstates independence.",
        ],
    )


def fetch_yahoo_d1(yahoo: str, years: int = 20) -> list:
    import time
    import urllib.request
    from types import SimpleNamespace

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


def load_candles(*, years: int = 20) -> tuple[dict[str, list], dict[str, float], str]:
    candles, spreads = {}, {}
    for sym, yahoo in UNIVERSE:
        data = fetch_yahoo_d1(yahoo, years=years)
        if len(data) < WARMUP + HOLD + 50:
            continue
        candles[sym] = data
        spreads[sym] = DEFAULT_SPREADS[sym]
    return candles, spreads, "yahoo_continuous_futures"
