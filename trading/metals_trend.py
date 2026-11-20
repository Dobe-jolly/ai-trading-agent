"""Study C: metals trend sleeve (long-biased D1).

Rules and gate are fixed in PLAYBOOKS.md ("Study C") before any result exists. Fixed-hold ATR-unit
simulator: long when close > SMA(200), else flat; never short. Jump-stops N/A (no stop). First-pass
gate requires beating a random-long placebo and an always-long sleeve (metals drift analogue).
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
from .study import asset_class

OOS_FRACTION = 0.4
SEED_DEFAULT = "0"

SMA_N = 200
HOLD = 21
WARMUP = SMA_N + 14
SWAP_ATR_PER_DAY = 0.015

MIN_TRADES = 100
MIN_T = 3.0
MIN_OOS_TRADES = 40
MIN_OOS_T = 1.65
MIN_EDGE = 0.05

METALS = ("XAUUSD", "XAGUSD")
DEFAULT_SPREADS = {"XAUUSD": 0.30, "XAGUSD": 0.03}
YAHOO_MAP = {"XAUUSD": "GC=F", "XAGUSD": "SI=F"}


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_index: int
    exit_index: int
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
            "Study C: metals trend sleeve (long-biased D1). SMA(200) filter, hold 21, ATR units after spread + overnight drag.",
            f"Data source: {self.data_source}",
            f"Gate: >={MIN_TRADES} trades, avg > 0, t >= {MIN_T:g}; OOS >={MIN_OOS_TRADES}, avg > 0, t >= {MIN_OOS_T:g}; "
            f"beat random-long placebo by >={MIN_EDGE:g} ATR; beat always-long by >={MIN_EDGE:g} ATR (full + OOS).",
            "Jump-stops: N/A (no stop). Drift analogue: always-long in the first gate.",
            "",
            f"  {'sleeve':<14}{'n':>6}{'win%':>6}{'avg':>8}{'t':>7} | {'oos n':>5}{'oos':>8}{'oos t':>7}",
            f"  {'trend_200':<14}{self.real.n:>6}{self.real.win_pct:>6.1f}{self.real.avg_r:>+8.3f}{self.real.t:>+7.2f} | "
            f"{self.real.n_oos:>5}{self.real.avg_r_oos:>+8.3f}{self.real.t_oos:>+7.2f}",
            f"  {'random_long':<14}{self.placebo.n:>6}{self.placebo.win_pct:>6.1f}{self.placebo.avg_r:>+8.3f}{self.placebo.t:>+7.2f} | "
            f"{self.placebo.n_oos:>5}{self.placebo.avg_r_oos:>+8.3f}{self.placebo.t_oos:>+7.2f}",
            f"  {'always_long':<14}{self.always_long.n:>6}{self.always_long.win_pct:>6.1f}{self.always_long.avg_r:>+8.3f}{self.always_long.t:>+7.2f} | "
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
            "params": {"sma": SMA_N, "hold": HOLD, "swap_atr_per_day": SWAP_ATR_PER_DAY},
        }

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"study_metals_trend_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
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
            f"beats random-long placebo by only {real.avg_r - placebo.avg_r:+.3f} ATR "
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


def _sma(closes: list[float], t: int, n: int = SMA_N) -> Optional[float]:
    if t + 1 < n:
        return None
    window = closes[t - n + 1:t + 1]
    return sum(window) / n


def _cost(spread: float, atr: float) -> float:
    return (spread / atr) + SWAP_ATR_PER_DAY * HOLD


def simulate_symbol(
    symbol: str,
    candles: list,
    spread: float,
    *,
    mode: str = "trend",
    seed: str = SEED_DEFAULT,
    fire_rate: Optional[float] = None,
) -> tuple[list[Trade], int, int]:
    """mode: 'trend' | 'always_long' | 'placebo' (random long at fire_rate). Returns trades, signals, free."""
    n = len(candles)
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr = atr_series(highs, lows, closes)
    split = int(n * (1 - OOS_FRACTION))
    rng = random.Random(f"{seed}:{symbol}:{mode}")
    trades: list[Trade] = []
    signals = free = 0
    t = WARMUP
    while t < n - HOLD - 1:
        free += 1
        a = atr[t]
        if not a or a <= 0:
            t += 1
            continue
        take = False
        if mode == "trend":
            sma = _sma(closes, t)
            take = sma is not None and closes[t] > sma
        elif mode == "always_long":
            take = True
        elif mode == "placebo":
            rate = fire_rate if fire_rate is not None else 0.0
            take = rng.random() < rate
        else:
            raise ValueError(mode)
        if not take:
            t += 1
            continue
        signals += 1
        entry_i = t + 1
        exit_i = t + HOLD
        if exit_i >= n:
            break
        r = (closes[exit_i] - opens[entry_i]) / a - _cost(spread, a)
        trades.append(Trade(symbol, entry_i, exit_i, r, int(entry_i >= split)))
        t = exit_i + 1
    return trades, signals, free


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
        if asset_class(symbol) != "metals":
            continue
        if len(candles) < WARMUP + HOLD + 50:
            continue
        spread = spreads.get(symbol, DEFAULT_SPREADS.get(symbol, 0.0))
        real, signals, free = simulate_symbol(symbol, candles, spread, mode="trend", seed=seed)
        rate = signals / free if free else 0.0
        plac, _, _ = simulate_symbol(symbol, candles, spread, mode="placebo", seed=seed, fire_rate=rate)
        along, _, _ = simulate_symbol(symbol, candles, spread, mode="always_long", seed=seed)
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
            "Long-biased only (never short).",
            f"Overnight drag {SWAP_ATR_PER_DAY} ATR/day × {HOLD} on metals.",
            "Always-long is in the first gate (metals drift analogue of Boom/Crash drift placebo).",
        ],
    )


def fetch_yahoo_d1(symbol: str, years: int = 20) -> list:
    import time
    import urllib.request
    from types import SimpleNamespace

    yahoo = YAHOO_MAP[symbol]
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
            open=float(o), high=float(h), low=float(l), close=float(c), volume=int(q["volume"][i] or 0),
        ))
    return out


def load_candles(
    *,
    source: str = "auto",
    years: int = 20,
    bars: int = 5000,
    settings=None,
) -> tuple[dict[str, list], dict[str, float], str]:
    candles, spreads = {}, {}
    if source in ("auto", "mt5") and settings is not None:
        try:
            from .broker.metatrader import MT5Broker
            broker = MT5Broker()
            broker.connect(settings.mt5_login, settings.mt5_password, settings.mt5_server)
            try:
                for sym in METALS:
                    data = broker.get_candles(sym, "D1", bars)
                    if len(data) < WARMUP + HOLD + 50:
                        continue
                    candles[sym] = data
                    info = broker.get_symbol_info(sym)
                    spreads[sym] = (info.ask - info.bid) if info else DEFAULT_SPREADS[sym]
            finally:
                broker.disconnect()
            if candles:
                return candles, spreads, "mt5"
        except Exception:
            if source == "mt5":
                raise
            candles, spreads = {}, {}
    for sym in METALS:
        data = fetch_yahoo_d1(sym, years=years)
        if len(data) < WARMUP + HOLD + 50:
            continue
        candles[sym] = data
        spreads[sym] = DEFAULT_SPREADS[sym]
    return candles, spreads, "yahoo_gc_si_futures"
