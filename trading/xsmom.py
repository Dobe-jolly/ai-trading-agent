"""Study E: cross-sectional FX momentum (Menkhoff-style winners-minus-losers).

Rules fixed in PLAYBOOKS.md ("Study E") before any result. Rank G10 currencies by past L-day
USD spot return; long top-2 / short bottom-2; hold H. Jump-stops N/A; Boom/Crash drift N/A.
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
HOLD = 21
TOP_K = 2
OVERNIGHT_ATR_PER_DAY = 0.015
MIN_ELIGIBLE = 6

# currency → (Yahoo pair, quote style). ccyusd: pair rises when currency strengthens vs USD.
# usdccy: invert close to get currency-per-USD for formation returns.
CURRENCY_PAIR: dict[str, tuple[str, str]] = {
    "EUR": ("EURUSD", "ccyusd"),
    "GBP": ("GBPUSD", "ccyusd"),
    "AUD": ("AUDUSD", "ccyusd"),
    "NZD": ("NZDUSD", "ccyusd"),
    "JPY": ("USDJPY", "usdccy"),
    "CHF": ("USDCHF", "usdccy"),
    "CAD": ("USDCAD", "usdccy"),
}

PAIRS = tuple(sorted({p for p, _ in CURRENCY_PAIR.values()}))
YAHOO_FX = {s: f"{s}=X" for s in PAIRS}

DEFAULT_SPREADS: dict[str, float] = {
    "EURUSD": 0.00012, "GBPUSD": 0.00015, "AUDUSD": 0.00015, "NZDUSD": 0.00020,
    "USDJPY": 0.015, "USDCHF": 0.00020, "USDCAD": 0.00020,
}

# Fixed risk-on diagnostic basket (registered): long AUD+NZD, short JPY+CHF.
RISKON_LONG = ("AUD", "NZD")
RISKON_SHORT = ("JPY", "CHF")

MIN_EDGE_OVER_RISKON = MIN_EDGE_OVER_PLACEBO


@dataclass(frozen=True)
class Trade:
    symbol: str
    currency: str
    entry_index: int
    exit_index: int
    direction: int
    r: float
    half: int


@dataclass
class Cell:
    group: str
    symbols: list[str]
    real: Stats
    placebo: Stats
    riskon: Stats
    verdict: str
    failed: list[str]
    riskon_ok: Optional[bool] = None
    riskon_notes: list[str] = field(default_factory=list)
    per_symbol: dict[str, tuple[int, float]] = field(default_factory=dict)


@dataclass
class Report:
    data_source: str
    cell: Cell
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        c = self.cell
        lines = [
            f"Study E: xsmom_{LOOKBACK}_{HOLD}. Cross-sectional FX momentum; "
            f"R after spread + {OVERNIGHT_ATR_PER_DAY:g}×H overnight.",
            f"Data source: {self.data_source}",
            f"Gate: >={MIN_TRADES} trades, avg > 0, t >= {MIN_T:g}; OOS >={MIN_OOS_TRADES}, avg > 0, "
            f"t >= {MIN_OOS_T:g}; beat random placebo by >={MIN_EDGE_OVER_PLACEBO:g} R. "
            f"PASS still needs risk-on-basket edge >={MIN_EDGE_OVER_RISKON:g} (full + OOS).",
            "Jump-stops N/A. Boom/Crash drift placebo N/A. Shared USD factors; legs dependent.",
            "",
            f"  {'class':<8}{'n':>6}{'win%':>6}{'avg':>8}{'t':>7} | "
            f"{'oos n':>5}{'oos':>8}{'oos t':>7} | {'plac':>8}{'risk':>8}  verdict",
            f"  {c.group:<8}{c.real.n:>6}{c.real.win_pct:>6.1f}{c.real.avg_r:>+8.3f}{c.real.t:>+7.2f} | "
            f"{c.real.n_oos:>5}{c.real.avg_r_oos:>+8.3f}{c.real.t_oos:>+7.2f} | "
            f"{c.placebo.avg_r:>+8.3f}{c.riskon.avg_r:>+8.3f}  {c.verdict}",
        ]
        for reason in c.failed:
            lines.append(f"      - {reason}")
        if c.riskon_ok is False:
            for note in c.riskon_notes:
                lines.append(f"      - risk-on basket: {note}")
        elif c.riskon_ok is True:
            lines.append("      - risk-on basket diagnostic: cleared")
        cand = "xsmom_21_21/forex" if c.verdict == "PASS" and c.riskon_ok else "none"
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
            "notes": self.notes,
            "cell": {
                "group": c.group, "symbols": c.symbols, "verdict": c.verdict, "failed": c.failed,
                "riskon_ok": c.riskon_ok, "riskon_notes": c.riskon_notes,
                "real": c.real.__dict__, "placebo": c.placebo.__dict__,
                "riskon": c.riskon.__dict__, "per_symbol": c.per_symbol,
            },
        }

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"study_xsmom_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
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


def riskon_diagnostic(real: Stats, riskon: Stats) -> tuple[bool, list[str]]:
    notes = []
    if riskon.n < MIN_TRADES:
        notes.append(f"risk-on basket only {riskon.n} trades (need {MIN_TRADES})")
    if real.avg_r - riskon.avg_r < MIN_EDGE_OVER_RISKON:
        notes.append(
            f"beats risk-on basket by only {real.avg_r - riskon.avg_r:+.3f} R "
            f"(need {MIN_EDGE_OVER_RISKON})"
        )
    if riskon.n_oos < MIN_OOS_TRADES:
        notes.append(f"risk-on basket only {riskon.n_oos} OOS trades")
    elif real.avg_r_oos - riskon.avg_r_oos < MIN_EDGE_OVER_RISKON:
        notes.append(
            f"OOS beats risk-on basket by only {real.avg_r_oos - riskon.avg_r_oos:+.3f} R "
            f"(need {MIN_EDGE_OVER_RISKON})"
        )
    return (not notes), notes


def _bar_date(candle) -> date:
    t = candle.time
    if isinstance(t, datetime):
        return t.date()
    if isinstance(t, date):
        return t
    raise TypeError(f"candle.time must be date/datetime, got {type(t)}")


def pair_direction_for_currency(ccy: str, side: int) -> tuple[str, int]:
    """side +1 = long the currency, -1 = short. Returns (pair, pair_trade_direction)."""
    pair, style = CURRENCY_PAIR[ccy]
    if style == "ccyusd":
        return pair, side  # long EUR → buy EURUSD
    return pair, -side  # long JPY → sell USDJPY


def currency_px(close: float, style: str) -> float:
    if style == "ccyusd":
        return close
    if close == 0:
        return float("nan")
    return 1.0 / close


def build_common_calendar(candles_by_pair: dict[str, list]) -> list[date]:
    sets = []
    for pair in PAIRS:
        if pair not in candles_by_pair:
            return []
        sets.append({_bar_date(c) for c in candles_by_pair[pair]})
    common = set.intersection(*sets)
    return sorted(common)


def index_maps(candles_by_pair: dict[str, list]) -> dict[str, dict[date, int]]:
    return {pair: {_bar_date(c): i for i, c in enumerate(candles)} for pair, candles in candles_by_pair.items()}


def formation_return(
    ccy: str,
    signal_date: date,
    lookback_date: date,
    candles_by_pair: dict[str, list],
    idx: dict[str, dict[date, int]],
) -> Optional[float]:
    pair, style = CURRENCY_PAIR[ccy]
    i0 = idx[pair].get(lookback_date)
    i1 = idx[pair].get(signal_date)
    if i0 is None or i1 is None:
        return None
    c0 = candles_by_pair[pair][i0].close
    c1 = candles_by_pair[pair][i1].close
    p0, p1 = currency_px(c0, style), currency_px(c1, style)
    if not p0 or p0 <= 0 or math.isnan(p0) or math.isnan(p1):
        return None
    return p1 / p0 - 1.0


def rank_basket(
    signal_date: date,
    lookback_date: date,
    candles_by_pair: dict[str, list],
    idx: dict[str, dict[date, int]],
    *,
    mode: str,
    rng: random.Random,
) -> Optional[tuple[list[str], list[str]]]:
    """Returns (long_ccys, short_ccys) or None if ineligible."""
    scored: list[tuple[float, str]] = []
    for ccy in CURRENCY_PAIR:
        ret = formation_return(ccy, signal_date, lookback_date, candles_by_pair, idx)
        if ret is None:
            continue
        scored.append((ret, ccy))
    if len(scored) < MIN_ELIGIBLE:
        return None
    if mode == "placebo":
        ccys = [c for _, c in scored]
        rng.shuffle(ccys)
        long_ccys, short_ccys = ccys[:TOP_K], ccys[-TOP_K:]
    elif mode == "riskon":
        long_ccys, short_ccys = list(RISKON_LONG), list(RISKON_SHORT)
        # Only if all four are eligible
        eligible = {c for _, c in scored}
        if not all(c in eligible for c in long_ccys + short_ccys):
            return None
    else:
        # Higher return first; ties → alphabetical currency (stable, registered)
        scored.sort(key=lambda x: (-x[0], x[1]))
        long_ccys = [c for _, c in scored[:TOP_K]]
        short_ccys = [c for _, c in scored[-TOP_K:]]
    if set(long_ccys) & set(short_ccys):
        return None
    return long_ccys, short_ccys


def _leg_trade(
    ccy: str,
    side: int,
    signal_date: date,
    exit_date: date,
    candles_by_pair: dict[str, list],
    idx: dict[str, dict[date, int]],
    spreads: dict[str, float],
) -> Optional[Trade]:
    pair, direction = pair_direction_for_currency(ccy, side)
    # Entry = open of next common/pair bar after signal_date
    dates_pair = sorted(idx[pair].keys())
    # find signal index on pair
    if signal_date not in idx[pair] or exit_date not in idx[pair]:
        return None
    sig_i = idx[pair][signal_date]
    entry_i = sig_i + 1
    exit_i = idx[pair][exit_date]
    candles = candles_by_pair[pair]
    if entry_i >= len(candles) or exit_i <= entry_i or exit_i >= len(candles):
        # exit_date should be HOLD steps ahead on common calendar; pair must have that date
        return None
    # Recompute exit_i as pair index of exit_date — already done
    # ATR at signal bar
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr = atr_series(highs, lows, closes)
    a = atr[sig_i]
    if not a or a <= 0:
        return None
    entry = candles[entry_i].open
    exit_px = candles[exit_i].close
    spread = spreads.get(pair, DEFAULT_SPREADS.get(pair, 0.0))
    overnight = OVERNIGHT_ATR_PER_DAY * HOLD
    r = direction * (exit_px - entry) / a - spread / a - overnight
    n = len(candles)
    split = int(n * (1 - OOS_FRACTION))
    return Trade(pair, ccy, entry_i, exit_i, direction, r, int(entry_i >= split))


def simulate(
    candles_by_pair: dict[str, list],
    spreads: dict[str, float],
    *,
    mode: str = "rule",
    seed: str = SEED_DEFAULT,
) -> list[Trade]:
    calendar = build_common_calendar(candles_by_pair)
    if len(calendar) < LOOKBACK + HOLD + 50:
        return []
    idx = index_maps(candles_by_pair)
    rng = random.Random(f"{seed}:xsmom:{mode}")
    trades: list[Trade] = []
    # Walk common calendar by index
    i = LOOKBACK
    while i < len(calendar) - HOLD - 1:
        signal_date = calendar[i]
        lookback_date = calendar[i - LOOKBACK]
        exit_date = calendar[i + HOLD]
        basket = rank_basket(
            signal_date, lookback_date, candles_by_pair, idx, mode=mode, rng=rng,
        )
        if basket is None:
            i += 1
            continue
        longs, shorts = basket
        cohort: list[Trade] = []
        ok = True
        for ccy in longs:
            leg = _leg_trade(ccy, +1, signal_date, exit_date, candles_by_pair, idx, spreads)
            if leg is None:
                ok = False
                break
            cohort.append(leg)
        if ok:
            for ccy in shorts:
                leg = _leg_trade(ccy, -1, signal_date, exit_date, candles_by_pair, idx, spreads)
                if leg is None:
                    ok = False
                    break
                cohort.append(leg)
        if ok and len(cohort) == 2 * TOP_K:
            trades.extend(cohort)
            # Non-overlapping: next signal after hold on common calendar
            i = i + HOLD + 1
        else:
            i += 1
    return trades


def run(
    candles_by_pair: dict[str, list],
    spreads: Optional[dict[str, float]] = None,
    *,
    seed: str = SEED_DEFAULT,
    data_source: str = "unknown",
) -> Report:
    spreads = spreads or {}
    notes = [
        "Cross-sectional FX momentum (Menkhoff et al. style) on G10 vs USD — not time-series TSMOM.",
        f"Formation L={LOOKBACK}, hold H={HOLD}, top/bottom {TOP_K}; overnight {OVERNIGHT_ATR_PER_DAY:g} ATR/day.",
        "Jump-stops N/A; Boom/Crash drift placebo N/A — gate numbers match Phase 0.",
        "Pooled t overstates independence (shared USD factor; 4 legs per rebalance).",
    ]
    real = simulate(candles_by_pair, spreads, mode="rule", seed=seed)
    plac = simulate(candles_by_pair, spreads, mode="placebo", seed=seed)
    risk = simulate(candles_by_pair, spreads, mode="riskon", seed=seed)
    real_s, plac_s, risk_s = stats_of(real), stats_of(plac), stats_of(risk)
    verdict, failed = judge(real_s, plac_s)
    rk_ok: Optional[bool] = None
    rk_notes: list[str] = []
    if verdict == "PASS":
        rk_ok, rk_notes = riskon_diagnostic(real_s, risk_s)
    per_symbol: dict[str, tuple[int, float]] = {}
    by_sym: dict[str, list[float]] = {}
    for t in real:
        by_sym.setdefault(t.symbol, []).append(t.r)
    for sym, rs in by_sym.items():
        per_symbol[sym] = (len(rs), round(statistics.fmean(rs), 3))
    cell = Cell(
        group="forex", symbols=sorted(candles_by_pair.keys()), real=real_s, placebo=plac_s,
        riskon=risk_s, verdict=verdict, failed=failed, riskon_ok=rk_ok, riskon_notes=rk_notes,
        per_symbol=per_symbol,
    )
    return Report(data_source=data_source, cell=cell, notes=notes)


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


def load_fx_yahoo(years: int = 20) -> tuple[dict[str, list], dict[str, float], str]:
    candles, spreads = {}, {}
    for pair in PAIRS:
        data = fetch_yahoo_d1(YAHOO_FX[pair], years=years)
        if len(data) < LOOKBACK + HOLD + 50:
            continue
        candles[pair] = data
        spreads[pair] = DEFAULT_SPREADS.get(pair, 0.0)
    return candles, spreads, "yahoo"
