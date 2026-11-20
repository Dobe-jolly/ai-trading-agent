"""Machine checks for Study G10-F (funding/swap carry). Rules are fixed in PLAYBOOKS.md."""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading import carry_funding as cf


def _candle(t: datetime, o: float, h: float, l: float, c: float):
    return SimpleNamespace(time=t, open=o, high=h, low=l, close=c, volume=0)


def _flat_then_drift(n: int, start: float, drift: float, noise: float, seed: int) -> list:
    """D1-like path with controllable drift; ATR stays positive via noise."""
    rng = random.Random(seed)
    t0 = datetime(2008, 1, 1, tzinfo=timezone.utc)
    price, out = start, []
    for i in range(n):
        move = drift + rng.gauss(0, noise)
        new = price + move
        hi = max(price, new) + abs(rng.gauss(0, noise))
        lo = min(price, new) - abs(rng.gauss(0, noise))
        out.append(_candle(t0 + timedelta(days=i), price, hi, lo, new))
        price = new
    return out


def _panel_for_audusd(high_aud: float = 5.0, low_usd: float = 0.5) -> dict:
    """Constant AUD>>USD differential across many months so the rule always buys AUDUSD."""
    months = {}
    for y in range(2007, 2027):
        for m in range(1, 13):
            months[f"{y:04d}-{m:02d}"] = None
    aud = {k: high_aud for k in months}
    usd = {k: low_usd for k in months}
    return {
        "AUD": aud, "USD": usd, "EUR": usd, "JPY": usd, "GBP": usd,
        "CHF": usd, "CAD": usd, "NZD": aud,
    }


def test_swap_long_day_formula():
    # 1.0 spot, +3.65 pp differential → 0.0001 price units per day
    assert cf.swap_long_day(1.0, 3.65) == pytest.approx(0.0001)


def test_lagged_rate_no_same_month_peek():
    panel = {"2020-01": 1.0, "2020-02": 2.0, "2020-03": 3.0}
    assert cf.lagged_rate(panel, date(2020, 3, 15)) == 2.0
    assert cf.lagged_rate(panel, date(2020, 1, 5)) is None


def test_planted_funding_edge_can_pass_vs_placebo():
    """Adverse mild price drift but large positive funding → rule can clear the gate."""
    # Price drifts slightly against AUD long; funding at 5pp over 21d on ~0.005 ATR dominates.
    # Non-overlapping HOLD=21 needs ~2500 bars for ≥100 trades.
    candles = {"AUDUSD": _flat_then_drift(2800, start=0.75, drift=-0.00002, noise=0.0004, seed=1)}
    panel = _panel_for_audusd(high_aud=8.0, low_usd=0.25)
    report = cf.run(candles, panel, spreads={"AUDUSD": 0.00015}, seed="0")
    cell = report.cells[0]
    assert cell.real.n >= cf.MIN_TRADES
    assert cell.real.avg_r > 0
    assert cell.real.avg_r - cell.placebo.avg_r >= cf.MIN_EDGE
    assert cell.verdict == "PASS"


def test_random_walk_and_flat_rates_fail():
    candles = {"EURUSD": _flat_then_drift(900, start=1.1, drift=0.0, noise=0.002, seed=7)}
    # Flat rates → deadband → almost no trades, or near-zero funding.
    flat = {f"{y:04d}-{m:02d}": 1.0 for y in range(2007, 2027) for m in range(1, 13)}
    panel = {c: dict(flat) for c in ("EUR", "USD", "JPY", "GBP", "CHF", "CAD", "AUD", "NZD")}
    report = cf.run(candles, panel, spreads={"EURUSD": 0.00012}, seed="0")
    assert report.cells[0].verdict == "FAIL"


def test_anti_carry_mirrors_rule_direction():
    candles = {"AUDUSD": _flat_then_drift(500, start=0.8, drift=0.0, noise=0.0005, seed=3)}
    panel = _panel_for_audusd()
    real = cf.simulate_symbol("AUDUSD", candles["AUDUSD"], panel, 0.00015, mode="rule", seed="0")
    anti = cf.simulate_symbol("AUDUSD", candles["AUDUSD"], panel, 0.00015, mode="anti", seed="0")
    assert real and anti
    assert len(real) == len(anti)
    for a, b in zip(real, anti):
        assert a.entry_index == b.entry_index
        assert a.direction == -b.direction


def test_judge_requires_placebo_edge():
    real = cf.Stats(n=200, wins=110, avg_r=0.04, t=3.5, total_r=8.0, n_oos=80, avg_r_oos=0.05, t_oos=2.0)
    placebo = cf.Stats(n=200, wins=100, avg_r=0.03, t=2.0, total_r=6.0, n_oos=80, avg_r_oos=0.02, t_oos=1.0)
    verdict, failed = cf.judge(real, placebo)
    assert verdict == "FAIL"
    assert any("placebo" in f for f in failed)
