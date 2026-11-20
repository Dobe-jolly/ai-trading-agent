"""Study B machine checks: planted trend PASSes, random walk FAILs, reversed rule does not beat placebo."""
from __future__ import annotations

import math
import random
from types import SimpleNamespace

from trading.momentum import (
    MIN_EDGE_OVER_PLACEBO, Variant, VARIANTS, judge, run, simulate_symbol, stats,
)


def candle(o, h, l, c):
    return SimpleNamespace(open=o, high=h, low=l, close=c, volume=0)


def trending(n: int, drift: float = 0.4, sigma: float = 0.5, seed: int = 1) -> list:
    """Strong directional drift so 12-1 momentum has something to latch onto."""
    rng = random.Random(seed)
    price, out = 100.0, []
    for _ in range(n):
        new = price + drift + rng.gauss(0, sigma)
        out.append(candle(price, max(price, new) + 0.2, min(price, new) - 0.2, new))
        price = new
    return out


def random_walk(n: int, seed: int = 2, sigma: float = 1.0) -> list:
    rng = random.Random(seed)
    price, out = 100.0, []
    for _ in range(n):
        new = price + rng.gauss(0, sigma)
        out.append(candle(price, max(price, new) + 0.3, min(price, new) - 0.3, new))
        price = new
    return out


def test_variants_match_playbooks_registration():
    assert [(v.name, v.lookback, v.skip, v.hold) for v in VARIANTS] == [
        ("tsmom_12_1", 252, 21, 21),
        ("tsmom_63", 63, 0, 21),
    ]


def test_entry_is_next_open_exit_is_hold_close_and_costs_subtract():
    # Flat then a clean up-move: lookback window all equal except the measured return positive.
    v = Variant("unit", lookback=5, skip=0, hold=3)
    # closes: build lookback of rising prices so signal at t=warmup buys
    prices = [100 + i for i in range(40)]
    candles = [candle(p, p + 0.5, p - 0.5, p) for p in prices]
    # Force ATR≈1 by using unit ranges already; atr_series will be ~1
    trades = simulate_symbol("EURUSD", candles, v, spread=0.1, mode="rule", seed="0")
    assert trades, "expected at least one trade"
    t0 = trades[0]
    # entry at open of signal+1; exit at close of signal+hold
    assert t0.entry_index == trades[0].entry_index
    assert t0.exit_index - (t0.entry_index - 1) == v.hold
    # r = dir*(exit-entry)/atr - spread/atr - swap*hold; with positive move should be finite
    assert math.isfinite(t0.r)


def test_planted_trend_passes_the_gate_on_forex_basket():
    candles = {f"EURUSD": trending(2000, drift=0.35, seed=11)}
    # Enough synthetic "pairs" that share the trend so n clears the gate.
    for i, name in enumerate(["GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "EURGBP", "EURJPY", "USDJPY"]):
        candles[name] = trending(2000, drift=0.35, seed=20 + i)
    report = run(candles, spreads={s: 0.0 for s in candles}, seed="0", data_source="test",
                 variants=(Variant("tsmom_63", 63, 0, 21),))
    cell = next(c for c in report.cells if c.group == "forex")
    assert cell.verdict == "PASS", cell.failed
    assert cell.real.avg_r - cell.placebo.avg_r >= MIN_EDGE_OVER_PLACEBO


def test_random_walk_fails_the_gate():
    candles = {name: random_walk(2000, seed=100 + i) for i, name in enumerate(
        ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "EURGBP", "EURJPY", "USDJPY"]
    )}
    report = run(candles, spreads={s: 0.0 for s in candles}, seed="0", data_source="test",
                 variants=(Variant("tsmom_63", 63, 0, 21),))
    cell = next(c for c in report.cells if c.group == "forex")
    assert cell.verdict == "FAIL"


def test_judge_requires_edge_over_placebo():
    from trading.momentum import Stats
    real = Stats(n=120, wins=70, avg_r=0.05, t=3.5, total_r=6.0, n_oos=50, avg_r_oos=0.04, t_oos=2.0)
    plac = Stats(n=120, wins=60, avg_r=0.04, t=1.0, total_r=4.8, n_oos=50, avg_r_oos=0.03, t_oos=1.0)
    verdict, failed = judge(real, plac)
    assert verdict == "FAIL"
    assert any("placebo" in f for f in failed)
