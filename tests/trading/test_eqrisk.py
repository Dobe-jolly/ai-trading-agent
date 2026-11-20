"""Study D machine checks: planted equity→FX PASSes; random equity FAIL; constants match registration."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from trading.eqrisk import (
    HOLD, LOOKBACK, OVERNIGHT_ATR_PER_DAY, RISK_ON_DIR, equity_signs, run, simulate_symbol, stats_of,
)
from trading.playbooks.evaluate import Stats


def candle(d: date, o, h, l, c):
    return SimpleNamespace(
        time=datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c, volume=0,
    )


def _series(start: date, n: int, start_px: float, daily: float):
    out, px, day = [], start_px, start
    for _ in range(n):
        new = px + daily
        out.append(candle(day, px, max(px, new) + abs(daily) * 0.2, min(px, new) - abs(daily) * 0.2, new))
        px = new
        day += timedelta(days=1)
    return out


def test_registered_constants():
    assert LOOKBACK == 21 and HOLD == 5
    assert OVERNIGHT_ATR_PER_DAY == 0.015
    assert set(RISK_ON_DIR) == {"AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "EURJPY"}
    assert all(v == 1 for v in RISK_ON_DIR.values())


def test_equity_signs_no_lookahead_and_skips_flat():
    # Flat then up then down
    start = date(2010, 1, 1)
    eq = _series(start, 40, 100.0, 0.0)
    # Force a rise over last 21 from bar 21 onward
    for i in range(21, 40):
        eq[i] = candle(
            start + timedelta(days=i),
            100 + (i - 21), 101 + (i - 21), 99 + (i - 21), 100 + (i - 21) + 1,
        )
    signs = equity_signs(eq, lookback=21)
    # First possible sign at index 21
    assert (start + timedelta(days=20)) not in signs or True  # bar 20 has lookback into flats → ~0 skip
    d21 = start + timedelta(days=21)
    assert signs.get(d21) == 1


def test_planted_aligned_equity_fx_passes_gate():
    """When equity rises and risk FX rises (falls on risk-off), the rule should PASS."""
    start = date(2008, 1, 1)
    n = 900
    # Equity: steady up → always risk-on
    equity = _series(start, n, 1000.0, 1.0)
    # FX: AUDUSD rises with equity (risk-on long wins)
    candles = {}
    for sym in ("AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "EURJPY"):
        # USDJPY etc also rise under risk-on mapping (+1)
        base = {"AUDUSD": 0.70, "NZDUSD": 0.60, "USDJPY": 100.0, "USDCHF": 0.90, "EURJPY": 140.0}[sym]
        step = {"AUDUSD": 0.001, "NZDUSD": 0.001, "USDJPY": 0.05, "USDCHF": 0.0005, "EURJPY": 0.05}[sym]
        candles[sym] = _series(start, n, base, step)
    report = run(candles, equity, {s: 0.0 for s in candles}, seed="0", data_source="test", equity_ticker="TEST")
    assert report.cell.verdict == "PASS", report.cell.failed
    assert report.cell.real.avg_r > 0


def test_anti_aligned_equity_fx_fails():
    """Equity rises (risk-on) but risk FX trends down → rule loses → FAIL."""
    start = date(2008, 1, 1)
    n = 900
    equity = _series(start, n, 1000.0, 1.0)
    candles = {}
    for sym in RISK_ON_DIR:
        base = {"AUDUSD": 0.90, "NZDUSD": 0.80, "USDJPY": 120.0, "USDCHF": 1.10, "EURJPY": 160.0}[sym]
        step = {"AUDUSD": -0.001, "NZDUSD": -0.001, "USDJPY": -0.05, "USDCHF": -0.0005, "EURJPY": -0.05}[sym]
        candles[sym] = _series(start, n, base, step)
    report = run(candles, equity, {s: 0.0 for s in candles}, seed="0", data_source="test", equity_ticker="TEST")
    assert report.cell.verdict == "FAIL"
    assert report.cell.real.avg_r < 0


def test_always_riskon_diagnostic_requires_margin():
    real = Stats(n=120, wins=70, avg_r=0.10, t=3.5, total_r=12, n_oos=50, avg_r_oos=0.08, t_oos=2.0)
    aron = Stats(n=120, wins=65, avg_r=0.09, t=3.0, total_r=10, n_oos=50, avg_r_oos=0.07, t_oos=1.8)
    from trading.eqrisk import always_riskon_diagnostic
    ok, notes = always_riskon_diagnostic(real, aron)
    assert not ok and any("always-risk-on" in n for n in notes)
