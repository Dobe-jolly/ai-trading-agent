"""Study C: FX carry sleeve.

Rules and gate are fixed in PLAYBOOKS.md ("Study C") before any result exists. Fixed-hold D1
simulator with lagged policy-rate differentials and carry accrual. Jump-stops N/A (no stop);
Boom/Crash drift placebo N/A. Gate numbers match Phase 0; always-long diagnostic after a PASS only.
"""
from __future__ import annotations

import calendar
import csv
import io
import json
import math
import random
import statistics
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .playbooks.base import atr_series
from .playbooks.evaluate import (
    MIN_EDGE_OVER_PLACEBO, MIN_OOS_T, MIN_OOS_TRADES, MIN_T, MIN_TRADES, Stats, judge,
)
from .study import asset_class

OOS_FRACTION = 0.4
SEED_DEFAULT = "0"
HOLD = 21
WARMUP = 30  # ATR settle + calendar lag room

# FRED series for each currency (annualised % money-market / policy proxies).
RATE_SERIES: dict[str, str] = {
    "USD": "FEDFUNDS",
    "EUR": "ECBDFR",
    "GBP": "IUDSOIA",
    "JPY": "IRSTCI01JPM156N",
    "AUD": "IRSTCI01AUM156N",
    "CAD": "IRSTCI01CAM156N",
    "CHF": "IRSTCI01CHM156N",
    "NZD": "IRSTCI01NZM156N",
}

# OECD SDMX DF_FINMARK IR3TIB fallback (registered in PLAYBOOKS.md when FRED is unreachable).
OECD_REF_AREA: dict[str, str] = {
    "USD": "USA",
    "EUR": "EA20",
    "GBP": "GBR",
    "JPY": "JPN",
    "AUD": "AUS",
    "CAD": "CAN",
    "CHF": "CHE",
    "NZD": "NZL",
}

# BASEQUOTE → (base, quote)
PAIR_CCY: dict[str, tuple[str, str]] = {
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
    "USDCHF": ("USD", "CHF"),
    "AUDUSD": ("AUD", "USD"),
    "NZDUSD": ("NZD", "USD"),
    "USDCAD": ("USD", "CAD"),
    "EURJPY": ("EUR", "JPY"),
    "EURGBP": ("EUR", "GBP"),
}

FOREX_SYMBOLS = tuple(PAIR_CCY.keys())

YAHOO_MAP: dict[str, str] = {s: f"{s}=X" for s in FOREX_SYMBOLS}

# Price spreads (point × pips from backtest._SPREAD_PIPS).
DEFAULT_SPREADS: dict[str, float] = {
    "EURUSD": 0.00012, "GBPUSD": 0.00015, "AUDUSD": 0.00015, "NZDUSD": 0.00020,
    "USDJPY": 0.015, "USDCHF": 0.00020, "USDCAD": 0.00020,
    "EURJPY": 0.020, "EURGBP": 0.00020,
}

MIN_EDGE_OVER_ALWAYS_LONG = MIN_EDGE_OVER_PLACEBO


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_index: int
    exit_index: int
    direction: int
    r: float
    half: int
    spot_r: float
    carry_r: float


@dataclass
class Cell:
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
    cell: Cell
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        c = self.cell
        lines = [
            "Study C: FX carry_21. Result in ATR units after one spread; carry = entry×Δr×H/365.",
            f"Data source: {self.data_source}",
            f"Gate: >={MIN_TRADES} trades, avg > 0, t >= {MIN_T:g}; OOS >={MIN_OOS_TRADES}, avg > 0, "
            f"t >= {MIN_OOS_T:g}; beat random placebo by >={MIN_EDGE_OVER_PLACEBO:g} R. "
            f"PASS still needs always-long edge >={MIN_EDGE_OVER_ALWAYS_LONG:g} (full + OOS).",
            "Jump-stops N/A (no stop). Boom/Crash drift placebo N/A. Forex pairs share USD/EUR factors.",
            "",
            f"  {'class':<8}{'n':>6}{'win%':>6}{'avg':>8}{'t':>7} | "
            f"{'oos n':>5}{'oos':>8}{'oos t':>7} | {'plac':>8}{'A-long':>8}  verdict",
            f"  {c.group:<8}{c.real.n:>6}{c.real.win_pct:>6.1f}{c.real.avg_r:>+8.3f}{c.real.t:>+7.2f} | "
            f"{c.real.n_oos:>5}{c.real.avg_r_oos:>+8.3f}{c.real.t_oos:>+7.2f} | "
            f"{c.placebo.avg_r:>+8.3f}{c.always_long.avg_r:>+8.3f}  {c.verdict}",
        ]
        for reason in c.failed:
            lines.append(f"      - {reason}")
        if c.always_long_ok is False:
            for note in c.always_long_notes:
                lines.append(f"      - always-long: {note}")
        elif c.always_long_ok is True:
            lines.append("      - always-long diagnostic: cleared")
        cand = "carry_21/forex" if c.verdict == "PASS" and c.always_long_ok else "none"
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
                "always_long_ok": c.always_long_ok, "always_long_notes": c.always_long_notes,
                "real": c.real.__dict__, "placebo": c.placebo.__dict__,
                "always_long": c.always_long.__dict__, "per_symbol": c.per_symbol,
            },
        }

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"study_carry_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
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


def always_long_diagnostic(real: Stats, always_long: Stats) -> tuple[bool, list[str]]:
    notes = []
    if always_long.n < MIN_TRADES:
        notes.append(f"always-long only {always_long.n} trades (need {MIN_TRADES})")
    if real.avg_r - always_long.avg_r < MIN_EDGE_OVER_ALWAYS_LONG:
        notes.append(
            f"beats always-long by only {real.avg_r - always_long.avg_r:+.3f} R "
            f"(need {MIN_EDGE_OVER_ALWAYS_LONG})"
        )
    if always_long.n_oos < MIN_OOS_TRADES:
        notes.append(f"always-long only {always_long.n_oos} OOS trades")
    elif real.avg_r_oos - always_long.avg_r_oos < MIN_EDGE_OVER_ALWAYS_LONG:
        notes.append(
            f"OOS beats always-long by only {real.avg_r_oos - always_long.avg_r_oos:+.3f} R "
            f"(need {MIN_EDGE_OVER_ALWAYS_LONG})"
        )
    return (not notes), notes


# ---- rates -----------------------------------------------------------------------


def fetch_fred_csv(series_id: str) -> list[tuple[date, float]]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    rows: list[tuple[date, float]] = []
    reader = csv.DictReader(io.StringIO(text))
    # Column name is usually the series id
    value_key = None
    for row in reader:
        if value_key is None:
            value_key = next(k for k in row.keys() if k != "observation_date")
        raw = (row.get(value_key) or "").strip()
        if not raw or raw == ".":
            continue
        rows.append((date.fromisoformat(row["observation_date"]), float(raw)))
    return rows


def month_end_rates(series: list[tuple[date, float]]) -> dict[tuple[int, int], float]:
    """Last observation in each (year, month) → rate."""
    out: dict[tuple[int, int], float] = {}
    for d, v in series:
        out[(d.year, d.month)] = v
    return out


def fetch_oecd_ir3tib(ref_area: str) -> list[tuple[date, float]]:
    """Monthly 3m interbank (% p.a.) from OECD SDMX DF_FINMARK."""
    url = (
        "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/"
        f"{ref_area}.M.IR3TIB.PA.....?startPeriod=1999&dimensionAtObservation=AllDimensions"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/vnd.sdmx.data+json;charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = json.load(resp)
    struct = payload["data"]["structures"][0]
    dims = struct["dimensions"]["observation"]
    time_dim = next((d for d in dims if d.get("id") in ("TIME_PERIOD", "TIME")), dims[-1])
    times = [v["id"] if isinstance(v, dict) else str(v) for v in time_dim["values"]]
    rows: list[tuple[date, float]] = []
    for key, vals in payload["data"]["dataSets"][0]["observations"].items():
        period = times[int(key.split(":")[-1])]
        y, m = int(period[:4]), int(period[5:7])
        rows.append((date(y, m, calendar.monthrange(y, m)[1]), float(vals[0])))
    return rows


def load_currency_month_rates() -> tuple[dict[str, dict[tuple[int, int], float]], str]:
    """Prefer FRED; fall back to OECD IR3TIB. Returns (rates_by_ccy, source_tag)."""
    try:
        rates = {ccy: month_end_rates(fetch_fred_csv(sid)) for ccy, sid in RATE_SERIES.items()}
        if all(len(v) >= 24 for v in rates.values()):
            return rates, "fred"
    except Exception:  # noqa: BLE001 — network / parse; OECD is the registered fallback
        pass
    rates = {
        ccy: month_end_rates(fetch_oecd_ir3tib(ref))
        for ccy, ref in OECD_REF_AREA.items()
    }
    return rates, "oecd_ir3tib"


def prior_month_key(d: date) -> tuple[int, int]:
    if d.month == 1:
        return d.year - 1, 12
    return d.year, d.month - 1


def lagged_diff(
    symbol: str,
    when: date,
    rates: dict[str, dict[tuple[int, int], float]],
) -> Optional[float]:
    base, quote = PAIR_CCY[symbol]
    key = prior_month_key(when)
    rb = rates.get(base, {}).get(key)
    rq = rates.get(quote, {}).get(key)
    if rb is None or rq is None:
        return None
    return rb - rq


# ---- simulation ------------------------------------------------------------------


def _bar_date(candle) -> date:
    t = candle.time
    if isinstance(t, datetime):
        return t.date()
    if isinstance(t, date):
        return t
    raise TypeError(f"candle.time must be date/datetime, got {type(t)}")


def simulate_symbol(
    symbol: str,
    candles: list,
    rates: dict[str, dict[tuple[int, int], float]],
    spread: float,
    *,
    mode: str = "rule",
    seed: str = SEED_DEFAULT,
    hold: int = HOLD,
) -> list[Trade]:
    """mode: 'rule' | 'placebo' | 'always_long'."""
    n = len(candles)
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr = atr_series(highs, lows, closes)
    split = int(n * (1 - OOS_FRACTION))
    rng = random.Random(f"{seed}:carry:{symbol}:{mode}")
    trades: list[Trade] = []
    t = WARMUP
    while t < n - hold - 1:
        a = atr[t]
        diff = lagged_diff(symbol, _bar_date(candles[t]), rates)
        if diff is None or not a or a <= 0:
            t += 1
            continue
        if diff > 0:
            direction = 1
        elif diff < 0:
            direction = -1
        else:
            t += 1
            continue
        if mode == "placebo":
            direction = rng.choice((-1, 1))
        elif mode == "always_long":
            direction = 1
        entry_i = t + 1
        exit_i = t + hold
        if exit_i >= n:
            break
        entry = opens[entry_i]
        exit_px = closes[exit_i]
        # Same lagged differential as the signal (held fixed over the trade).
        carry_price = entry * (diff / 100.0) * hold / 365.0
        spot_r = direction * (exit_px - entry) / a
        carry_r = direction * carry_price / a
        cost = spread / a
        r = spot_r + carry_r - cost
        trades.append(Trade(
            symbol, entry_i, exit_i, direction, r, int(entry_i >= split), spot_r, carry_r,
        ))
        t = exit_i + 1
    return trades


def run(
    candles_by_symbol: dict[str, list],
    rates: dict[str, dict[tuple[int, int], float]],
    spreads: Optional[dict[str, float]] = None,
    *,
    seed: str = SEED_DEFAULT,
    data_source: str = "unknown",
) -> Report:
    spreads = spreads or {}
    notes = [
        "Forex pooled t-stats are not independent (shared USD/EUR factors).",
        "Carry uses lagged month M−1 policy differentials held fixed over each trade.",
        "Jump-stops N/A; Boom/Crash drift placebo N/A — gate numbers match Phase 0.",
    ]
    real_all: list[Trade] = []
    plac_all: list[Trade] = []
    along_all: list[Trade] = []
    symbols_used: list[str] = []
    per_symbol: dict[str, tuple[int, float]] = {}
    for symbol, candles in candles_by_symbol.items():
        if asset_class(symbol) != "forex" or symbol not in PAIR_CCY:
            continue
        if len(candles) < WARMUP + HOLD + 50:
            continue
        spread = spreads.get(symbol, DEFAULT_SPREADS.get(symbol, 0.0))
        real = simulate_symbol(symbol, candles, rates, spread, mode="rule", seed=seed)
        plac = simulate_symbol(symbol, candles, rates, spread, mode="placebo", seed=seed)
        along = simulate_symbol(symbol, candles, rates, spread, mode="always_long", seed=seed)
        real_all.extend(real)
        plac_all.extend(plac)
        along_all.extend(along)
        symbols_used.append(symbol)
        s = stats_of(real)
        per_symbol[symbol] = (s.n, round(s.avg_r, 3))
    real_s, plac_s, along_s = stats_of(real_all), stats_of(plac_all), stats_of(along_all)
    verdict, failed = judge(real_s, plac_s)
    al_ok: Optional[bool] = None
    al_notes: list[str] = []
    if verdict == "PASS":
        al_ok, al_notes = always_long_diagnostic(real_s, along_s)
    cell = Cell(
        group="forex", symbols=symbols_used, real=real_s, placebo=plac_s, always_long=along_s,
        verdict=verdict, failed=failed, always_long_ok=al_ok, always_long_notes=al_notes,
        per_symbol=per_symbol,
    )
    return Report(data_source=data_source, cell=cell, notes=notes)


# ---- data loading ----------------------------------------------------------------


def fetch_yahoo_d1(symbol: str, years: int = 20) -> list:
    import time
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
            open=float(o), high=float(h), low=float(l), close=float(c), volume=0,
        ))
    return out


def load_candles_yahoo(symbols: list[str], years: int = 20) -> tuple[dict[str, list], dict[str, float], str]:
    candles, spreads = {}, {}
    for sym in symbols:
        if sym not in YAHOO_MAP:
            continue
        data = fetch_yahoo_d1(sym, years=years)
        if len(data) < WARMUP + HOLD + 50:
            continue
        candles[sym] = data
        spreads[sym] = DEFAULT_SPREADS.get(sym, 0.0)
    return candles, spreads, "yahoo"
