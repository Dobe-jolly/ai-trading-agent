"""Study D: cross-asset risk-on/off (equity → FX).

Rules and gate are fixed in PLAYBOOKS.md ("Study D") before any result exists. Equity lookback
signal maps to fixed FX risk-on directions; fixed-hold ATR-unit simulator. Jump-stops N/A;
Boom/Crash drift placebo N/A. Gate numbers match Phase 0; always-risk-on diagnostic after PASS only.
"""
from __future__ import annotations

import json
import math
import random
import statistics
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from .playbooks.base import atr_series
from .playbooks.evaluate import (
    MIN_EDGE_OVER_PLACEBO, MIN_OOS_T, MIN_OOS_TRADES, MIN_T, MIN_TRADES, Stats, judge,
)

OOS_FRACTION = 0.4
SEED_DEFAULT = "0"
LOOKBACK = 21
HOLD = 5
OVERNIGHT_ATR_PER_DAY = 0.015
WARMUP = LOOKBACK + 30

# Pair → risk-on direction (registered in PLAYBOOKS.md).
RISK_ON_DIR: dict[str, int] = {
    "AUDUSD": 1,
    "NZDUSD": 1,
    "USDJPY": 1,
    "USDCHF": 1,
    "EURJPY": 1,
}
FX_SYMBOLS = tuple(RISK_ON_DIR.keys())

YAHOO_FX: dict[str, str] = {s: f"{s}=X" for s in FX_SYMBOLS}
EQUITY_TICKERS = ("^GSPC", "SPY")  # preferred then fallback

DEFAULT_SPREADS: dict[str, float] = {
    "AUDUSD": 0.00015, "NZDUSD": 0.00020,
    "USDJPY": 0.015, "USDCHF": 0.00020, "EURJPY": 0.020,
}

MIN_EDGE_OVER_ALWAYS_RISKON = MIN_EDGE_OVER_PLACEBO


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_index: int
    exit_index: int
    direction: int
    r: float
    half: int
    equity_sign: int


@dataclass
class Cell:
    group: str
    symbols: list[str]
    real: Stats
    placebo: Stats
    always_riskon: Stats
    verdict: str
    failed: list[str]
    always_riskon_ok: Optional[bool] = None
    always_riskon_notes: list[str] = field(default_factory=list)
    per_symbol: dict[str, tuple[int, float]] = field(default_factory=dict)


@dataclass
class Report:
    data_source: str
    equity_ticker: str
    cell: Cell
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        c = self.cell
        lines = [
            f"Study D: eqrisk_{LOOKBACK}_{HOLD}. Equity→FX risk-on/off; R after spread + "
            f"{OVERNIGHT_ATR_PER_DAY:g}×H overnight.",
            f"Data source: {self.data_source}; equity={self.equity_ticker}",
            f"Gate: >={MIN_TRADES} trades, avg > 0, t >= {MIN_T:g}; OOS >={MIN_OOS_TRADES}, avg > 0, "
            f"t >= {MIN_OOS_T:g}; beat random placebo by >={MIN_EDGE_OVER_PLACEBO:g} R. "
            f"PASS still needs always-risk-on edge >={MIN_EDGE_OVER_ALWAYS_RISKON:g} (full + OOS).",
            "Jump-stops N/A (no stop). Boom/Crash drift placebo N/A. Shared equity + USD/JPY factors.",
            "",
            f"  {'class':<8}{'n':>6}{'win%':>6}{'avg':>8}{'t':>7} | "
            f"{'oos n':>5}{'oos':>8}{'oos t':>7} | {'plac':>8}{'A-ron':>8}  verdict",
            f"  {c.group:<8}{c.real.n:>6}{c.real.win_pct:>6.1f}{c.real.avg_r:>+8.3f}{c.real.t:>+7.2f} | "
            f"{c.real.n_oos:>5}{c.real.avg_r_oos:>+8.3f}{c.real.t_oos:>+7.2f} | "
            f"{c.placebo.avg_r:>+8.3f}{c.always_riskon.avg_r:>+8.3f}  {c.verdict}",
        ]
        for reason in c.failed:
            lines.append(f"      - {reason}")
        if c.always_riskon_ok is False:
            for note in c.always_riskon_notes:
                lines.append(f"      - always-risk-on: {note}")
        elif c.always_riskon_ok is True:
            lines.append("      - always-risk-on diagnostic: cleared")
        cand = "eqrisk_21_5/forex" if c.verdict == "PASS" and c.always_riskon_ok else "none"
        lines += ["", f"CANDIDATES: {cand}"]
        for note in self.notes:
            lines.append(f"Note: {note}")
        if c.per_symbol:
            lines.append("Per symbol (n, avg R): " + ", ".join(
                f"{s}={n}/{avg:+.3f}" for s, (n, avg) in sorted(c.per_symbol.items())
            ))
        return "\n".join(lines)

    def to_json(self) -> dict:
        c = self.cell
        return {
            "data_source": self.data_source,
            "equity_ticker": self.equity_ticker,
            "notes": self.notes,
            "cell": {
                "group": c.group, "symbols": c.symbols, "verdict": c.verdict, "failed": c.failed,
                "always_riskon_ok": c.always_riskon_ok, "always_riskon_notes": c.always_riskon_notes,
                "real": c.real.__dict__, "placebo": c.placebo.__dict__,
                "always_riskon": c.always_riskon.__dict__, "per_symbol": c.per_symbol,
            },
        }

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"study_eqrisk_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
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
    gains, losses = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    return Stats(
        n=len(rs), wins=sum(r > 0 for r in rs), avg_r=avg, t=t, total_r=sum(rs),
        profit_factor=(gains / losses) if losses > 0 else None,
        n_oos=len(oos), avg_r_oos=avg_oos, t_oos=t_oos,
    )


def always_riskon_diagnostic(real: Stats, always: Stats) -> tuple[bool, list[str]]:
    notes = []
    if always.n < MIN_TRADES:
        notes.append(f"always-risk-on only {always.n} trades (need {MIN_TRADES})")
    if real.avg_r - always.avg_r < MIN_EDGE_OVER_ALWAYS_RISKON:
        notes.append(
            f"beats always-risk-on by only {real.avg_r - always.avg_r:+.3f} R "
            f"(need {MIN_EDGE_OVER_ALWAYS_RISKON})"
        )
    if always.n_oos < MIN_OOS_TRADES:
        notes.append(f"always-risk-on only {always.n_oos} OOS trades")
    elif real.avg_r_oos - always.avg_r_oos < MIN_EDGE_OVER_ALWAYS_RISKON:
        notes.append(
            f"OOS beats always-risk-on by only {real.avg_r_oos - always.avg_r_oos:+.3f} R "
            f"(need {MIN_EDGE_OVER_ALWAYS_RISKON})"
        )
    return (not notes), notes


def _bar_date(candle) -> date:
    t = candle.time
    if isinstance(t, datetime):
        return t.date()
    if isinstance(t, date):
        return t
    raise TypeError(f"candle.time must be date/datetime, got {type(t)}")


def equity_signs(equity: list, lookback: int = LOOKBACK) -> dict[date, int]:
    """Map equity bar date → sign of L-day close return (skip zeros / short history)."""
    out: dict[date, int] = {}
    closes = [c.close for c in equity]
    for i in range(lookback, len(equity)):
        prev = closes[i - lookback]
        cur = closes[i]
        if not prev or prev <= 0 or cur is None:
            continue
        ret = cur / prev - 1.0
        if ret > 0:
            out[_bar_date(equity[i])] = 1
        elif ret < 0:
            out[_bar_date(equity[i])] = -1
    return out


def simulate_symbol(
    symbol: str,
    candles: list,
    signs_by_date: dict[date, int],
    spread: float,
    *,
    mode: str = "rule",
    seed: str = SEED_DEFAULT,
    hold: int = HOLD,
) -> list[Trade]:
    """mode: 'rule' | 'placebo' | 'always_riskon'."""
    n = len(candles)
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr = atr_series(highs, lows, closes)
    split = int(n * (1 - OOS_FRACTION))
    rng = random.Random(f"{seed}:eqrisk:{symbol}:{mode}")
    risk_on = RISK_ON_DIR[symbol]
    overnight = OVERNIGHT_ATR_PER_DAY * hold
    trades: list[Trade] = []
    t = WARMUP
    while t < n - hold - 1:
        a = atr[t]
        if not a or a <= 0:
            t += 1
            continue
        d = _bar_date(candles[t])
        eq_sign = signs_by_date.get(d)
        if eq_sign is None:
            t += 1
            continue
        # Entry open of t+1; exit close of t+H (registered).
        entry_i = t + 1
        exit_i = t + hold
        if exit_i >= n:
            break
        if mode == "placebo":
            direction = rng.choice((-1, 1))
        elif mode == "always_riskon":
            direction = risk_on
        else:
            direction = risk_on if eq_sign > 0 else -risk_on
        entry = opens[entry_i]
        exit_px = closes[exit_i]
        spot_r = direction * (exit_px - entry) / a
        cost = spread / a
        r = spot_r - cost - overnight
        trades.append(Trade(symbol, entry_i, exit_i, direction, r, int(entry_i >= split), eq_sign))
        t = exit_i + 1
    return trades


def run(
    candles_by_symbol: dict[str, list],
    equity: list,
    spreads: Optional[dict[str, float]] = None,
    *,
    seed: str = SEED_DEFAULT,
    data_source: str = "unknown",
    equity_ticker: str = "^GSPC",
) -> Report:
    spreads = spreads or {}
    signs = equity_signs(equity, LOOKBACK)
    notes = [
        "Forex pooled t-stats are not independent (shared USD/JPY factors + one equity driver).",
        f"Signal: sign of {equity_ticker} close return over {LOOKBACK} D1; hold {HOLD}; "
        f"overnight {OVERNIGHT_ATR_PER_DAY:g} ATR/day.",
        "Jump-stops N/A; Boom/Crash drift placebo N/A — gate numbers match Phase 0.",
    ]
    real_all: list[Trade] = []
    plac_all: list[Trade] = []
    aron_all: list[Trade] = []
    symbols_used: list[str] = []
    per_symbol: dict[str, tuple[int, float]] = {}
    for symbol, candles in candles_by_symbol.items():
        if symbol not in RISK_ON_DIR:
            continue
        if len(candles) < WARMUP + HOLD + 50:
            continue
        spread = spreads.get(symbol, DEFAULT_SPREADS.get(symbol, 0.0))
        real = simulate_symbol(symbol, candles, signs, spread, mode="rule", seed=seed)
        plac = simulate_symbol(symbol, candles, signs, spread, mode="placebo", seed=seed)
        aron = simulate_symbol(symbol, candles, signs, spread, mode="always_riskon", seed=seed)
        real_all.extend(real)
        plac_all.extend(plac)
        aron_all.extend(aron)
        symbols_used.append(symbol)
        s = stats_of(real)
        per_symbol[symbol] = (s.n, round(s.avg_r, 3))
    real_s, plac_s, aron_s = stats_of(real_all), stats_of(plac_all), stats_of(aron_all)
    verdict, failed = judge(real_s, plac_s)
    ar_ok: Optional[bool] = None
    ar_notes: list[str] = []
    if verdict == "PASS":
        ar_ok, ar_notes = always_riskon_diagnostic(real_s, aron_s)
    cell = Cell(
        group="forex", symbols=symbols_used, real=real_s, placebo=plac_s, always_riskon=aron_s,
        verdict=verdict, failed=failed, always_riskon_ok=ar_ok, always_riskon_notes=ar_notes,
        per_symbol=per_symbol,
    )
    return Report(
        data_source=data_source, equity_ticker=equity_ticker, cell=cell, notes=notes,
    )


def fetch_yahoo_d1(yahoo_symbol: str, years: int = 20) -> list:
    end = int(time.time())
    start = end - years * 365 * 24 * 3600
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
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
            open=float(o), high=float(h), low=float(l), close=float(c), volume=0,
        ))
    return out


def load_equity_yahoo(years: int = 20) -> tuple[list, str]:
    last_err: Optional[Exception] = None
    for ticker in EQUITY_TICKERS:
        try:
            data = fetch_yahoo_d1(ticker, years=years)
            if len(data) >= LOOKBACK + HOLD + 100:
                return data, ticker
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"Could not load equity D1 from {EQUITY_TICKERS}: {last_err}")


def load_fx_yahoo(symbols: list[str], years: int = 20) -> tuple[dict[str, list], dict[str, float], str]:
    candles, spreads = {}, {}
    for sym in symbols:
        if sym not in YAHOO_FX:
            continue
        data = fetch_yahoo_d1(YAHOO_FX[sym], years=years)
        if len(data) < WARMUP + HOLD + 50:
            continue
        candles[sym] = data
        spreads[sym] = DEFAULT_SPREADS.get(sym, 0.0)
    return candles, spreads, "yahoo"
