"""Study C machine checks: planted trend PASSes; random walk FAILs; always-long is in the first gate."""
from __future__ import annotations

import random
from types import SimpleNamespace

from trading.metals_trend import HOLD, SMA_N, WARMUP, Stats, judge, run, simulate_symbol


def candle(o, h, l, c):
    return SimpleNamespace(open=o, high=h, low=l, close=c, volume=0)


def _append(out, price, new):
    out.append(candle(price, max(price, new) + 0.05, min(price, new) - 0.05, new))
    return new


def trending_with_drawdowns(n: int = 6000, seed: int = 1) -> list:
    """Strong up-blocks, flat to let holds expire, then a soft grind below the SMA
    (filter flat; always-long keeps buying the hole). No cliff gaps — those dominate ATR PnL."""
    rng = random.Random(seed)
    price, out = 500.0, []
    while len(out) < n:
        for _ in range(220):
            if len(out) >= n:
                break
            price = _append(out, price, price + 1.5 + rng.gauss(0, 0.15))
        for _ in range(45):
            if len(out) >= n:
                break
            price = _append(out, price, price + rng.gauss(0, 0.08))
        for _ in range(140):
            if len(out) >= n:
                break
            price = _append(out, price, price - 0.8 + rng.gauss(0, 0.15))
    return out


def random_walk(n: int = 2500, seed: int = 2) -> list:
    rng = random.Random(seed)
    price, out = 100.0, []
    for _ in range(n):
        new = price + rng.gauss(0, 1.0)
        out.append(candle(price, max(price, new) + 0.3, min(price, new) - 0.3, new))
        price = new
    return out


def test_warmup_and_hold_match_registration():
    assert SMA_N == 200 and HOLD == 21 and WARMUP == 214


def test_judge_requires_always_long_in_the_first_gate():
    real = Stats(n=200, wins=120, avg_r=0.20, t=4.0, total_r=40.0,
                 n_oos=80, avg_r_oos=0.18, t_oos=2.5)
    placebo = Stats(n=200, avg_r=0.05, n_oos=80, avg_r_oos=0.04)
    always = Stats(n=200, avg_r=0.19, n_oos=80, avg_r_oos=0.17)  # within 0.05 → fail
    verdict, failed = judge(real, placebo, always)
    assert verdict == "FAIL"
    assert any("always-long" in f for f in failed)


def test_planted_trend_with_drawdowns_can_pass():
    candles = {"XAUUSD": trending_with_drawdowns(seed=11), "XAGUSD": trending_with_drawdowns(seed=12)}
    report = run(candles, {"XAUUSD": 0.0, "XAGUSD": 0.0}, seed="0", data_source="test")
    assert report.verdict == "PASS", report.failed
    assert report.real.avg_r - report.always_long.avg_r >= 0.05


def test_random_walk_fails():
    candles = {"XAUUSD": random_walk(seed=21), "XAGUSD": random_walk(seed=22)}
    report = run(candles, {"XAUUSD": 0.0, "XAGUSD": 0.0}, seed="0", data_source="test")
    assert report.verdict == "FAIL"


def test_trend_mode_only_longs_above_sma():
    # Flat then ramp: first trades should appear only after price is above the SMA.
    prices = [100.0] * 250 + [100.0 + i * 0.5 for i in range(300)]
    candles = [candle(p, p + 0.2, p - 0.2, p) for p in prices]
    trades, signals, free = simulate_symbol("XAUUSD", candles, spread=0.0, mode="trend", seed="0")
    assert signals == len(trades) > 0
    assert free > signals
