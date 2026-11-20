"""Study D machine checks: planted two-way trend PASSes; random walk FAILs; always-long in first gate."""
from __future__ import annotations

import math
import random
from types import SimpleNamespace

from trading.vol_futures_trend import (
    HOLD, LOOKBACK, SKIP, TARGET_VOL, VOL_WINDOW, WARMUP, Stats, judge, run, simulate_symbol,
)


def candle(o, h, l, c):
    return SimpleNamespace(open=o, high=h, low=l, close=c, volume=0)


def _append(out, price, new):
    out.append(candle(price, max(price, new) + abs(new - price) * 0.1 + 0.01,
                      min(price, new) - abs(new - price) * 0.1 - 0.01, new))
    return new


def two_way_trending(n: int = 5000, seed: int = 1) -> list:
    """Long up-legs and down-legs so 12-1 catches both directions; always-long loses on downs.
    Leg length > lookback so the signal spends most bars aligned with the active regime."""
    rng = random.Random(seed)
    price, out = 2000.0, []
    leg = 400
    while len(out) < n:
        for sign in (1, -1):
            for _ in range(leg):
                if len(out) >= n:
                    break
                price = _append(out, price, max(20.0, price + sign * 2.5 + rng.gauss(0, 0.5)))
    return out


def random_walk(n: int = 3500, seed: int = 2) -> list:
    rng = random.Random(seed)
    price, out = 100.0, []
    for _ in range(n):
        new = price * math.exp(rng.gauss(0, 0.01))
        out.append(candle(price, max(price, new) * 1.001, min(price, new) * 0.999, new))
        price = new
    return out


def test_params_match_registration():
    assert LOOKBACK == 252 and SKIP == 21 and HOLD == 21
    assert VOL_WINDOW == 63 and TARGET_VOL == 0.10
    assert WARMUP == LOOKBACK + SKIP + max(14, VOL_WINDOW)


def test_judge_requires_always_long_in_the_first_gate():
    real = Stats(n=200, wins=120, avg_r=0.20, t=4.0, total_r=40.0,
                 n_oos=80, avg_r_oos=0.18, t_oos=2.5)
    placebo = Stats(n=200, avg_r=0.05, n_oos=80, avg_r_oos=0.04)
    always = Stats(n=200, avg_r=0.19, n_oos=80, avg_r_oos=0.17)
    verdict, failed = judge(real, placebo, always)
    assert verdict == "FAIL"
    assert any("always-long" in f for f in failed)


def test_planted_two_way_trend_can_pass():
    candles = {
        "ES": two_way_trending(seed=11),
        "CL": two_way_trending(seed=12),
        "GC": two_way_trending(seed=13),
        "ZN": two_way_trending(seed=14),
    }
    spreads = {s: 0.0 for s in candles}
    report = run(candles, spreads, seed="0", data_source="test")
    assert report.verdict == "PASS", report.failed
    assert report.real.avg_r - report.always_long.avg_r >= 0.05


def test_random_walk_fails():
    candles = {s: random_walk(seed=20 + i) for i, s in enumerate(("ES", "CL", "GC", "ZN"))}
    spreads = {s: 0.0 for s in candles}
    report = run(candles, spreads, seed="0", data_source="test")
    assert report.verdict == "FAIL"


def test_trend_takes_both_directions():
    candles = two_way_trending(seed=3)
    trades = simulate_symbol("ES", candles, spread=0.0, mode="trend", seed="0")
    dirs = {t.direction for t in trades}
    assert dirs == {-1, 1}
    assert all(0 < t.weight <= 2.0 for t in trades)
