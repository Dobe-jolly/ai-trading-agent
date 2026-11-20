"""Study G10-F: funding / swap carry on a slow G10 FX basket.

Rules and gate are fixed in PLAYBOOKS.md ("Study G10-F") before any result exists. Fixed-hold
simulator: signal from lagged money-market differentials, enter next open, hold 21 D1 bars, net R =
price/ATR + synthetic funding/ATR − spread/ATR. Placebo = random direction. Always-long and
anti-carry diagnostics run only after a PASS.
"""
from __future__ import annotations

import csv
import io
import json
import math
import random
import statistics
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from .playbooks.base import atr_series
from .study import asset_class

OOS_FRACTION = 0.4
SEED_DEFAULT = "0"
HOLD = 21
DEADBAND = 0.25          # percentage points
MIN_TRADES = 100
MIN_T = 3.0
MIN_OOS_TRADES = 40
MIN_OOS_T = 1.65
MIN_EDGE = 0.05

CACHE_DIR = Path(__file__).resolve().parent / "data" / "funding"

# BASE/QUOTE pairs in the registered basket.
PAIRS: tuple[str, ...] = (
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD",
    "EURJPY", "GBPJPY", "EURGBP", "AUDJPY", "CADJPY",
)

YAHOO_MAP: dict[str, str] = {p: f"{p}=X" for p in PAIRS}

# Pip sizes for Yahoo path (matches backtest._SPREAD_PIPS × point). JPY quotes use 0.01.
_SPREAD_PIPS: dict[str, float] = {
    "EURUSD": 1.2, "GBPUSD": 1.5, "AUDUSD": 1.5, "NZDUSD": 2.0,
    "USDJPY": 1.5, "USDCHF": 2.0, "USDCAD": 2.0,
    "EURJPY": 2.0, "GBPJPY": 2.0, "EURGBP": 2.0, "AUDJPY": 2.0, "CADJPY": 2.0,
}


def spread_price(symbol: str) -> float:
    pips = _SPREAD_PIPS.get(symbol, 2.0)
    point = 0.01 if symbol.endswith("JPY") else 0.0001
    return pips * point

# Currency → which month-keyed rate series name we record on the report.
SERIES_LABEL: dict[str, str] = {
    "USD": "NYFed_SOFR|BIS_US_policy_pre201804",
    "EUR": "SNB_ESTR|SNB_EURIBOR_pre201910",
    "JPY": "SNB_TONA",
    "GBP": "SNB_SONIA",
    "CHF": "SNB_SARON",
    "CAD": "BoC_V39079",
    "AUD": "RBA_F1.1_interbank_overnight",
    "NZD": "BIS_NZ_policy_OCR",
}


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_index: int
    exit_index: int
    direction: int
    r: float
    half: int
    score: float


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
    group: str
    symbols: list[str]
    real: Stats
    placebo: Stats
    always_long: Stats
    anti: Stats
    verdict: str
    failed: list[str]
    always_long_ok: Optional[bool] = None
    anti_ok: Optional[bool] = None
    diagnostic_notes: list[str] = field(default_factory=list)
    per_symbol: dict[str, tuple[int, float]] = field(default_factory=dict)


@dataclass
class Report:
    data_source: str
    funding_source: str
    cells: list[Cell]
    notes: list[str] = field(default_factory=list)
    series_used: dict[str, str] = field(default_factory=dict)

    def text(self) -> str:
        lines = [
            "Study G10-F: funding/swap carry on G10 FX. Net R = price/ATR + funding/ATR − spread/ATR.",
            f"Prices: {self.data_source}. Funding: {self.funding_source}.",
            f"Series: {self.series_used}",
            f"Gate: >={MIN_TRADES} trades, avg>0, t>={MIN_T:g}; OOS >={MIN_OOS_TRADES}, avg>0, t>={MIN_OOS_T:g}; "
            f"beat random placebo by >={MIN_EDGE:g} ATR. PASS still needs always-long and anti-carry margins.",
            "G10 pairs share dollar/rate factors — pooled t overstates independence.",
            "",
            f"  {'class':<8}{'n':>6}{'win%':>6}{'avg':>8}{'t':>7} | {'oos n':>5}{'oos':>8}{'oos t':>7} | "
            f"{'plac':>8}{'A-long':>8}{'anti':>8}  verdict",
        ]
        for c in self.cells:
            lines.append(
                f"  {c.group:<8}{c.real.n:>6}{c.real.win_pct:>6.1f}{c.real.avg_r:>+8.3f}{c.real.t:>+7.2f} | "
                f"{c.real.n_oos:>5}{c.real.avg_r_oos:>+8.3f}{c.real.t_oos:>+7.2f} | "
                f"{c.placebo.avg_r:>+8.3f}{c.always_long.avg_r:>+8.3f}{c.anti.avg_r:>+8.3f}  {c.verdict}"
            )
            for reason in c.failed:
                lines.append(f"      - {reason}")
            for note in c.diagnostic_notes:
                lines.append(f"      - {note}")
        cands = [c for c in self.cells if c.verdict == "PASS" and c.always_long_ok and c.anti_ok]
        lines += ["", f"CANDIDATES: {', '.join(c.group for c in cands) or 'none'}"]
        for note in self.notes:
            lines.append(f"Note: {note}")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "data_source": self.data_source, "funding_source": self.funding_source,
            "series_used": self.series_used, "notes": self.notes,
            "cells": [
                {
                    "group": c.group, "symbols": c.symbols, "verdict": c.verdict, "failed": c.failed,
                    "always_long_ok": c.always_long_ok, "anti_ok": c.anti_ok,
                    "diagnostic_notes": c.diagnostic_notes, "per_symbol": c.per_symbol,
                    "real": c.real.__dict__, "placebo": c.placebo.__dict__,
                    "always_long": c.always_long.__dict__, "anti": c.anti.__dict__,
                }
                for c in self.cells
            ],
        }

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"study_g10_carry_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
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
    if real.avg_r - placebo.avg_r < MIN_EDGE:
        failed.append(
            f"beats the placebo by only {real.avg_r - placebo.avg_r:+.3f} ATR "
            f"(real {real.avg_r:+.3f}, placebo {placebo.avg_r:+.3f}; need {MIN_EDGE})"
        )
    return ("FAIL" if failed else "PASS"), failed


def _margin_ok(real: Stats, other: Stats, label: str) -> tuple[bool, list[str]]:
    notes = []
    if other.n < MIN_TRADES:
        notes.append(f"{label}: only {other.n} trades")
    if real.avg_r - other.avg_r < MIN_EDGE:
        notes.append(f"{label}: beats by only {real.avg_r - other.avg_r:+.3f} ATR (need {MIN_EDGE})")
    if other.n_oos < MIN_OOS_TRADES:
        notes.append(f"{label}: only {other.n_oos} OOS trades")
    elif real.avg_r_oos - other.avg_r_oos < MIN_EDGE:
        notes.append(
            f"{label}: OOS beats by only {real.avg_r_oos - other.avg_r_oos:+.3f} ATR (need {MIN_EDGE})"
        )
    return (not notes), notes


def pair_currencies(symbol: str) -> tuple[str, str]:
    return symbol[:3], symbol[3:6]


# ---- HTTP helpers ----------------------------------------------------------------


def _http_get(url: str, retries: int = 4, timeout: int = 90) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (g10-carry-study)"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if data.lstrip().startswith(b"<") and b"GenericData" not in data[:200] and b"<?xml" not in data[:50]:
                # HTML error page
                if b"<html" in data[:200].lower():
                    raise RuntimeError(f"HTML error page from {url}")
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def _cache_write(name: str, data: bytes) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    path.write_bytes(data)
    return path


def _cache_read(name: str) -> Optional[bytes]:
    path = CACHE_DIR / name
    return path.read_bytes() if path.exists() else None


# ---- rate panel: month key "YYYY-MM" -> percent ------------------------------------------------


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _parse_ymd(s: str) -> date:
    s = s.strip().strip('"')
    if "/" in s:
        # MM/DD/YYYY (NY Fed)
        m, d, y = s.split("/")
        return date(int(y), int(m), int(d))
    if len(s) == 7 and s[4] == "-":
        y, m = s.split("-")
        return date(int(y), int(m), 1)
    return date.fromisoformat(s[:10])


def _month_average(daily: list[tuple[date, float]]) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for d, v in daily:
        buckets.setdefault(_month_key(d), []).append(v)
    return {k: statistics.fmean(vs) for k, vs in buckets.items()}


def load_snb_zimoma() -> dict[str, dict[str, float]]:
    """SNB money-market cube → {code: {YYYY-MM: rate}}."""
    raw = _cache_read("snb_zimoma.csv")
    if raw is None:
        raw = _http_get("https://data.snb.ch/api/cube/zimoma/data/csv/en")
        _cache_write("snb_zimoma.csv", raw)
    out: dict[str, dict[str, float]] = {}
    for line in raw.decode("utf-8-sig", errors="replace").splitlines():
        parts = [p.strip().strip('"') for p in line.split(";")]
        if len(parts) < 3 or not parts[0][:1].isdigit():
            continue
        d, code, val = parts[0], parts[1], parts[2]
        if not val:
            continue
        try:
            rate = float(val)
        except ValueError:
            continue
        mk = d if len(d) == 7 else _month_key(_parse_ymd(d))
        out.setdefault(code, {})[mk] = rate
    return out


def load_sofr_months() -> dict[str, float]:
    raw = _cache_read("nyfed_sofr.csv")
    if raw is None:
        raw = _http_get(
            "https://markets.newyorkfed.org/read?productCode=50&eventCodes=520"
            "&limit=25000&startPosition=0&sort=postDt:1&format=csv"
        )
        _cache_write("nyfed_sofr.csv", raw)
    daily = []
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))
    for row in reader:
        rate_type = (row.get("Rate Type") or "SOFR").strip()
        if rate_type and rate_type != "SOFR":
            continue
        try:
            daily.append((_parse_ymd(row["Effective Date"]), float(row["Rate (%)"])))
        except (KeyError, ValueError):
            continue
    return _month_average(daily)


def load_boc_months() -> dict[str, float]:
    raw = _cache_read("boc_v39079.csv")
    if raw is None:
        raw = _http_get("https://www.bankofcanada.ca/valet/observations/V39079/csv")
        _cache_write("boc_v39079.csv", raw)
    text = raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.strip().strip('"').upper() == "OBSERVATIONS":
            # Next non-empty line is the "date","V39079" header.
            start = i + 2
            break
        if line.lower().startswith('"date"') or line.lower().startswith("date,"):
            start = i + 1
            break
    daily = []
    for line in lines[start:]:
        if not line.strip():
            continue
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 2 or not parts[0][:1].isdigit():
            continue
        try:
            daily.append((_parse_ymd(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return _month_average(daily)


def load_rba_overnight_months() -> dict[str, float]:
    """RBA F1.1 Interbank Overnight Cash Rate — already monthly averages (month-end dates)."""
    raw = _cache_read("rba_f1_1.csv")
    if raw is None:
        raw = _http_get("https://www.rba.gov.au/statistics/tables/csv/f1.1-data.csv")
        _cache_write("rba_f1_1.csv", raw)
    text = raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    header_i = next(i for i, l in enumerate(lines) if "Interbank Overnight Cash Rate" in l)
    header = next(csv.reader([lines[header_i]]))
    try:
        col = header.index("Interbank Overnight Cash Rate")
    except ValueError:
        col = 2
    months: dict[str, float] = {}
    for line in lines[header_i + 1:]:
        row = next(csv.reader([line]))
        if not row or not row[0] or not row[0][0].isdigit():
            continue
        try:
            ds = row[0].strip()
            if "/" in ds:
                d = datetime.strptime(ds, "%d/%m/%Y").date()
            else:
                try:
                    d = date.fromisoformat(ds[:10])
                except ValueError:
                    d = datetime.strptime(ds, "%d-%b-%Y").date()
            val = row[col].strip() if col < len(row) else ""
            if not val:
                continue
            months[_month_key(d)] = float(val)
        except (ValueError, IndexError):
            continue
    return months


def load_bis_policy_months(areas: tuple[str, ...] = ("US", "NZ", "XM")) -> dict[str, dict[str, float]]:
    """BIS WS_CBPOL monthly policy rates. areas: US, NZ, XM (euro area), etc."""
    key = "bis_cbpol.csv"
    raw = _cache_read(key)
    if raw is None:
        areas_q = "+".join(areas)
        raw = _http_get(
            f"https://stats.bis.org/api/v1/data/WS_CBPOL/M.{areas_q}"
            f"?format=csv&startPeriod=2000"
        )
        _cache_write(key, raw)
    out: dict[str, dict[str, float]] = {}
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace")))
    for row in reader:
        area = row.get("REF_AREA") or row.get("ref_area")
        period = row.get("TIME_PERIOD") or row.get("time_period")
        val = row.get("OBS_VALUE") or row.get("obs_value")
        if not area or not period or val in (None, ""):
            continue
        try:
            out.setdefault(area, {})[period[:7]] = float(val)
        except ValueError:
            continue
    return out


def build_rate_panel() -> tuple[dict[str, dict[str, float]], dict[str, str]]:
    """Return ({CCY: {YYYY-MM: rate%}}, labels)."""
    snb = load_snb_zimoma()
    sofr = load_sofr_months()
    boc = load_boc_months()
    rba = load_rba_overnight_months()
    bis = load_bis_policy_months(("US", "NZ", "XM", "JP", "GB", "CH", "CA", "AU"))

    usd = dict(sofr)
    for mk, v in bis.get("US", {}).items():
        if mk < "2018-04":
            usd.setdefault(mk, v)

    eur = dict(snb.get("ESTR", {}))
    for mk, v in snb.get("EURIBOR", {}).items():
        if mk < "2019-10":
            eur.setdefault(mk, v)
    # last-resort euro area policy
    for mk, v in bis.get("XM", {}).items():
        eur.setdefault(mk, v)

    panel = {
        "USD": usd,
        "EUR": eur,
        "JPY": dict(snb.get("TONA", {})),
        "GBP": dict(snb.get("SONIA", {})),
        "CHF": dict(snb.get("SARON", {})),
        "CAD": dict(boc) if boc else dict(bis.get("CA", {})),
        "AUD": dict(rba) if rba else dict(bis.get("AU", {})),
        "NZD": dict(bis.get("NZ", {})),
    }
    labels = dict(SERIES_LABEL)
    if not boc:
        labels["CAD"] = "BIS_CA_policy"
    if not rba:
        labels["AUD"] = "BIS_AU_policy"
    return panel, labels


def lagged_rate(panel_ccy: dict[str, float], when: date) -> Optional[float]:
    """Last month-average strictly before the calendar month of `when`."""
    cutoff = f"{when.year:04d}-{when.month:02d}"
    keys = [k for k in panel_ccy if k < cutoff]
    if not keys:
        return None
    return panel_ccy[max(keys)]


def carry_score(panel: dict[str, dict[str, float]], symbol: str, when: date) -> Optional[float]:
    base, quote = pair_currencies(symbol)
    rb = lagged_rate(panel.get(base, {}), when)
    rq = lagged_rate(panel.get(quote, {}), when)
    if rb is None or rq is None:
        return None
    return rb - rq


def swap_long_day(spot: float, score_pct: float) -> float:
    """Price units per day for a long, from registered formula."""
    return spot * (score_pct / 100.0) / 365.0


# ---- simulation ------------------------------------------------------------------


def _candle_date(c) -> date:
    t = getattr(c, "time", None)
    if isinstance(t, datetime):
        return t.date()
    if isinstance(t, date):
        return t
    return date(2000, 1, 1)


def simulate_symbol(
    symbol: str,
    candles: list,
    panel: dict[str, dict[str, float]],
    spread: float,
    *,
    mode: str = "rule",
    seed: str = SEED_DEFAULT,
) -> list[Trade]:
    """mode: rule | placebo | always_long | anti."""
    n = len(candles)
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr = atr_series(highs, lows, closes)
    split = int(n * (1 - OOS_FRACTION))
    rng = random.Random(f"{seed}:g10:{symbol}:{mode}")
    trades: list[Trade] = []
    t = 60
    while t < n - HOLD - 1:
        a = atr[t]
        when = _candle_date(candles[t])
        score = carry_score(panel, symbol, when)
        if score is None or not a or a <= 0:
            t += 1
            continue
        if mode == "rule":
            if score > DEADBAND:
                direction = 1
            elif score < -DEADBAND:
                direction = -1
            else:
                t += 1
                continue
        elif mode == "anti":
            if score > DEADBAND:
                direction = -1
            elif score < -DEADBAND:
                direction = 1
            else:
                t += 1
                continue
        elif mode == "placebo":
            if abs(score) < DEADBAND:
                t += 1
                continue
            direction = rng.choice((-1, 1))
        elif mode == "always_long":
            if abs(score) < DEADBAND:
                t += 1
                continue
            direction = 1
        else:
            raise ValueError(mode)

        entry_i = t + 1
        exit_i = t + HOLD
        if exit_i >= n:
            break
        entry = opens[entry_i]
        exit_px = closes[exit_i]
        # Funding uses signal-time score and entry spot (rates fixed for the hold).
        fund = direction * swap_long_day(entry, score) * HOLD / a
        price_r = direction * (exit_px - entry) / a
        cost = spread / a
        r = price_r + fund - cost
        trades.append(Trade(symbol, entry_i, exit_i, direction, r, int(entry_i >= split), score))
        t = exit_i + 1
    return trades


def run(
    candles_by_symbol: dict[str, list],
    panel: dict[str, dict[str, float]],
    spreads: Optional[dict[str, float]] = None,
    *,
    seed: str = SEED_DEFAULT,
    data_source: str = "yahoo",
    funding_source: str = "public_mm_proxy",
    series_used: Optional[dict[str, str]] = None,
) -> Report:
    spreads = spreads or {}
    notes = [
        "Synthetic swaps use overnight/MM differentials with no broker markup (optimistic vs live MT5).",
        "NZD uses BIS policy OCR where overnight was unreachable; stated in PLAYBOOKS.md before the run.",
        "G10 pairs are correlated — discount pooled t-stats.",
    ]
    real_all: list[Trade] = []
    plac_all: list[Trade] = []
    along_all: list[Trade] = []
    anti_all: list[Trade] = []
    per_symbol: dict[str, tuple[int, float]] = {}
    symbols_used: list[str] = []
    for symbol, candles in candles_by_symbol.items():
        if asset_class(symbol) != "forex":
            continue
        if len(candles) < 200:
            continue
        spread = spreads.get(symbol, spread_price(symbol))
        real = simulate_symbol(symbol, candles, panel, spread, mode="rule", seed=seed)
        plac = simulate_symbol(symbol, candles, panel, spread, mode="placebo", seed=seed)
        along = simulate_symbol(symbol, candles, panel, spread, mode="always_long", seed=seed)
        anti = simulate_symbol(symbol, candles, panel, spread, mode="anti", seed=seed)
        if not real and not plac:
            continue
        symbols_used.append(symbol)
        real_all.extend(real)
        plac_all.extend(plac)
        along_all.extend(along)
        anti_all.extend(anti)
        s = stats_of(real)
        per_symbol[symbol] = (s.n, round(s.avg_r, 3))

    real_s, plac_s = stats_of(real_all), stats_of(plac_all)
    along_s, anti_s = stats_of(along_all), stats_of(anti_all)
    verdict, failed = judge(real_s, plac_s)
    al_ok = anti_ok = None
    diag: list[str] = []
    if verdict == "PASS":
        al_ok, al_notes = _margin_ok(real_s, along_s, "always-long")
        anti_ok, anti_notes = _margin_ok(real_s, anti_s, "anti-carry")
        diag = al_notes + anti_notes
        if al_ok:
            diag.append("always-long diagnostic: cleared")
        if anti_ok:
            diag.append("anti-carry diagnostic: cleared")

    cell = Cell(
        group="forex", symbols=symbols_used, real=real_s, placebo=plac_s,
        always_long=along_s, anti=anti_s, verdict=verdict, failed=failed,
        always_long_ok=al_ok, anti_ok=anti_ok, diagnostic_notes=diag, per_symbol=per_symbol,
    )
    return Report(
        data_source=data_source, funding_source=funding_source, cells=[cell], notes=notes,
        series_used=series_used or dict(SERIES_LABEL),
    )


def fetch_yahoo_d1(symbol: str, years: int = 20) -> list:
    from types import SimpleNamespace
    yahoo = YAHOO_MAP[symbol]
    end = int(time.time())
    start = end - years * 365 * 24 * 3600
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo}"
        f"?interval=1d&period1={start}&period2={end}"
    )
    payload = json.loads(_http_get(url).decode())
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


def load_yahoo_basket(years: int = 20) -> tuple[dict[str, list], dict[str, float], str]:
    candles, spreads = {}, {}
    for sym in PAIRS:
        try:
            data = fetch_yahoo_d1(sym, years=years)
        except Exception as exc:  # noqa: BLE001
            continue
        if len(data) < 400:
            continue
        candles[sym] = data
        spreads[sym] = spread_price(sym)
        time.sleep(0.2)
    return candles, spreads, "yahoo"
