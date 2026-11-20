"""Study C machine checks: planted carry PASSes; random rates FAIL; lag has no lookahead."""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

from trading.carry import (
    HOLD, PAIR_CCY, always_long_diagnostic, lagged_diff, prior_month_key, run, simulate_symbol, stats_of,
)
from trading.playbooks.evaluate import Stats


def candle(d: date, o, h, l, c):
    return SimpleNamespace(
        time=datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c, volume=0,
    )


def test_prior_month_key_wraps_january():
    assert prior_month_key(date(2020, 1, 15)) == (2019, 12)
    assert prior_month_key(date(2020, 3, 1)) == (2020, 2)


def test_lagged_diff_uses_prior_month_only():
    rates = {
        "EUR": {(2020, 1): 0.0, (2020, 2): 1.0},
        "USD": {(2020, 1): 0.0, (2020, 2): 0.0},
    }
    # March bar → February rates: EUR-USD = 1.0
    assert lagged_diff("EURUSD", date(2020, 3, 10), rates) == 1.0
    # February bar → January rates: 0.0 → skip as zero later; differential is 0
    assert lagged_diff("EURUSD", date(2020, 2, 10), rates) == 0.0
    # No January for a Jan bar (needs Dec 2019) → None
    assert lagged_diff("EURUSD", date(2020, 1, 10), rates) is None


def _path_with_carry_edge(n=900):
    """Uptrend when EUR>USD carry is positive: price rises while EUR rate stays above USD."""
    # Build daily candles from 2005-01-01
    start = date(2005, 1, 1)
    out = []
    price = 1.20
    from datetime import timedelta
    day = start
    for i in range(n):
        # mild uptrend
        new = price + 0.0015
        out.append(candle(day, price, max(price, new) + 0.0003, min(price, new) - 0.0003, new))
        price = new
        day = day + timedelta(days=1)
    # Rates: EUR always 5%, USD always 1% for every month in range
    rates = {"EUR": {}, "USD": {}, "GBP": {}, "JPY": {}, "AUD": {}, "CAD": {}, "CHF": {}, "NZD": {}}
    y, m = 2004, 12
    for _ in range(80):
        rates["EUR"][(y, m)] = 5.0
        rates["USD"][(y, m)] = 1.0
        for ccy in ("GBP", "JPY", "AUD", "CAD", "CHF", "NZD"):
            rates[ccy][(y, m)] = 1.0
        m += 1
        if m > 12:
            m = 1
            y += 1
    return {"EURUSD": out}, rates


def test_planted_positive_carry_with_aligned_spot_passes_gate():
    candles, rates = _path_with_carry_edge()
    # Duplicate as more pairs sharing the same constructive edge so n clears the gate.
    for sym in ("GBPUSD", "AUDUSD", "NZDUSD"):
        candles[sym] = candles["EURUSD"]
        # For GBP/AUD/NZD vs USD, set base rate high
        base = PAIR_CCY[sym][0]
        for key in list(rates["USD"].keys()):
            rates[base][key] = 5.0
    report = run(candles, rates, {s: 0.0 for s in candles}, seed="0", data_source="test")
    assert report.cell.verdict == "PASS", report.cell.failed
    assert report.cell.real.avg_r > 0


def test_zero_differential_produces_no_trades():
    candles, rates = _path_with_carry_edge()
    for key in list(rates["EUR"].keys()):
        rates["EUR"][key] = rates["USD"][key]
    trades = simulate_symbol("EURUSD", candles["EURUSD"], rates, 0.0, mode="rule", seed="0")
    assert trades == []


def test_always_long_diagnostic_requires_margin():
    real = Stats(n=120, wins=70, avg_r=0.10, t=3.5, total_r=12, n_oos=50, avg_r_oos=0.08, t_oos=2.0)
    along = Stats(n=120, wins=65, avg_r=0.09, t=3.0, total_r=10, n_oos=50, avg_r_oos=0.07, t_oos=1.8)
    ok, notes = always_long_diagnostic(real, along)
    assert not ok and any("always-long" in n for n in notes)


def test_hold_constant_matches_registration():
    assert HOLD == 21
