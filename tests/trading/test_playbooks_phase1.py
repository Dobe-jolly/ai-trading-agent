from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading.geometry import classify_regime, find_swings
from trading.playbooks import REGISTRY
from trading.playbooks.alpha import Alpha
from trading.playbooks.base import Bars
from trading.playbooks.beta import Beta
from trading.playbooks.evaluate import run_playbook
from trading.playbooks.simulate import lookahead_violations

# ---- the registry matches what was approved --------------------------------------


def test_phase_one_is_registered_with_the_approved_geometry():
    a, b = REGISTRY["alpha"], REGISTRY["beta"]
    assert (a.timeframe, a.stop_atr, a.target_atr, a.max_bars) == ("H4", 1.5, 3.0, 30)
    assert (b.timeframe, b.stop_atr, b.target_atr, b.max_bars) == ("H1", 1.5, 3.0, 12)
    assert a.asset_classes == b.asset_classes == ("forex", "metals")


# ---- Alpha -----------------------------------------------------------------------


def zigzag(points, leg=6):
    closes = [points[0]]
    for a, b in zip(points, points[1:]):
        closes += [a + (b - a) * k / leg for k in range(1, leg + 1)]
    return closes


def bars_from_closes(closes, pad=0.3, final=None):
    """Opens are the previous close; `final` replaces the last bar with an explicit (open, high, low, close)."""
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + pad for o, c in zip(opens, closes)]
    lows = [min(o, c) - pad for o, c in zip(opens, closes)]
    if final:
        opens[-1], highs[-1], lows[-1], closes[-1] = final
    return Bars(opens, highs, lows, closes)


def uptrend(final, lows=(104, 108)):
    """Higher highs 110, 114, 118 and higher lows, then a fall back toward the last low, then `final`."""
    points = [100, 110, lows[0], 114, lows[1], 118]
    closes = zigzag(points) + zigzag([118, 110.0], leg=5)[1:]
    closes = closes + [final[3]]
    return bars_from_closes(closes, final=final)


def mirror(bars: Bars, pivot=220.0) -> Bars:
    """The same market upside down: prices reflected, so highs become lows."""
    return Bars([pivot - x for x in bars.opens], [pivot - x for x in bars.lows],
                [pivot - x for x in bars.highs], [pivot - x for x in bars.closes])


GOOD = (109.2, 109.8, 108.6, 109.6)         # opens just above the last higher low (107.7), dips to 108.6, closes up


def test_the_uptrend_used_in_these_tests_really_is_one_to_the_engine():
    bars = uptrend(GOOD)
    t = len(bars) - 1
    swings = find_swings(bars.highs[:t + 1], bars.lows[:t + 1], 3)
    assert classify_regime(swings, 0.1 * bars.atr[t]) == "BULLISH_TREND"
    assert [round(s.price, 1) for s in swings if s.kind == "low"][-2:] == [103.7, 107.7]


def test_alpha_buys_a_pullback_to_the_last_higher_low_that_closes_up():
    bars = uptrend(GOOD)
    sig = Alpha().scan(bars, len(bars) - 1)
    assert sig is not None and sig.direction == 1


def test_alpha_needs_the_bar_to_close_up():
    bars = uptrend((109.6, 109.9, 108.6, 109.2))                     # same area, closes down
    assert Alpha().scan(bars, len(bars) - 1) is None


def test_alpha_needs_price_to_come_back_near_the_swing_low():
    bars = uptrend((112.0, 112.6, 111.8, 112.5))                     # up bar, but far above the low
    assert Alpha().scan(bars, len(bars) - 1) is None


def test_alpha_needs_the_close_to_hold_above_the_swing_low():
    # an UP bar (so the close-up rule is satisfied) that is still below the 107.7 swing low: only the
    # "held above it" condition can reject this one
    bars = uptrend((106.0, 107.0, 105.5, 106.8))
    assert Alpha().scan(bars, len(bars) - 1) is None


def test_alpha_stays_out_when_the_lows_are_not_rising():
    bars = uptrend(GOOD, lows=(104, 102))                            # a lower low: not a clean uptrend
    t = len(bars) - 1
    swings = find_swings(bars.highs[:t + 1], bars.lows[:t + 1], 3)
    assert classify_regime(swings, 0.1 * bars.atr[t]) != "BULLISH_TREND"
    assert Alpha().scan(bars, t) is None


def test_alpha_sells_the_mirror_image_in_a_downtrend():
    bars = mirror(uptrend(GOOD))
    sig = Alpha().scan(bars, len(bars) - 1)
    assert sig is not None and sig.direction == -1


def test_the_mirror_conditions_fail_the_same_way():
    assert Alpha().scan(mirror(uptrend((109.6, 109.9, 108.6, 109.2))), len(uptrend(GOOD)) - 1) is None   # closes up in a downtrend
    assert Alpha().scan(mirror(uptrend((112.0, 112.6, 111.8, 112.5))), len(uptrend(GOOD)) - 1) is None


def test_alpha_says_nothing_before_there_is_an_atr():
    bars = bars_from_closes([100.0 + i * 0.1 for i in range(10)])
    assert Alpha().scan(bars, 9) is None


# ---- Beta ------------------------------------------------------------------------

DAY = datetime(2026, 3, 3, tzinfo=timezone.utc)            # a Tuesday


def make_day(closes_by_hour: dict[int, float], asian=(100.0, 101.0), start_hour=0, previous_day=True):
    """A run of H1 bars. Asian hours (0-6) span `asian`; later hours close where `closes_by_hour` says.
    ATR is fixed at 1.0 so range widths read directly as ATRs. Returns the bars and a hour -> index map."""
    rows, index = [], {}
    if previous_day:                                         # yesterday: wild, so any leak into today shows
        for h in range(24):
            when = DAY - timedelta(days=1) + timedelta(hours=h)
            rows.append((when, 150.0, 151.0, 49.0, 50.0 if h % 2 else 150.0))
    for h in range(start_hour, 12):
        when = DAY + timedelta(hours=h)
        if h <= 6:
            rows.append((when, 100.5, asian[1], asian[0], 100.5))
        else:
            c = closes_by_hour.get(h, 100.5)
            rows.append((when, c, c + 0.1, c - 0.1, c))
        index[h] = len(rows) - 1
    times, opens, highs, lows, closes = zip(*rows)
    return Bars(list(opens), list(highs), list(lows), list(closes), list(times), [1.0] * len(rows)), index


def scan(day, hour):
    bars, ix = day
    return Beta().scan(bars, ix[hour])


def test_beta_buys_the_first_close_above_the_asian_range_in_the_london_morning():
    day = make_day({7: 100.5, 8: 101.6})
    assert scan(day, 7) is None
    sig = scan(day, 8)
    assert sig is not None and sig.direction == 1


def test_beta_sells_a_break_below_the_range():
    sig = scan(make_day({8: 99.4}), 8)
    assert sig is not None and sig.direction == -1


def test_only_the_first_breakout_of_the_day_counts():
    day = make_day({8: 101.6, 9: 102.0})
    assert scan(day, 8) is not None
    assert scan(day, 9) is None


def test_a_break_the_other_way_earlier_also_uses_up_the_days_breakout():
    day = make_day({7: 99.4, 8: 101.6})                      # broke down at 07:00, up at 08:00
    assert scan(day, 7).direction == -1
    assert scan(day, 8) is None


def test_beta_ignores_a_range_that_is_too_narrow_or_too_wide():
    assert scan(make_day({8: 101.6}, asian=(100.0, 100.3)), 8) is None      # 0.3 ATR
    assert scan(make_day({8: 103.6}, asian=(100.0, 103.0)), 8) is None      # 3.0 ATR
    assert scan(make_day({8: 100.7}, asian=(100.0, 100.5)), 8) is not None  # 0.5 ATR: the boundary is allowed
    assert scan(make_day({8: 102.6}, asian=(100.0, 102.5)), 8) is not None  # 2.5 ATR: and so is this one


@pytest.mark.parametrize("hour", [11])
def test_beta_does_not_trade_outside_the_london_morning(hour):
    assert scan(make_day({hour: 101.6}), hour) is None


def test_beta_does_not_trade_during_the_asian_session_itself():
    day = make_day({})
    bars, ix = day
    assert Beta().scan(bars, ix[5]) is None


def test_a_day_with_too_few_asian_bars_is_skipped():
    day = make_day({8: 101.6}, start_hour=4)                 # only 04:00-06:59 present: 3 bars
    assert scan(day, 8) is None
    assert scan(make_day({8: 101.6}, start_hour=3), 8) is not None    # 4 bars is enough


def test_yesterdays_bars_never_leak_into_todays_range():
    # yesterday's bars span 49-151 and close far outside any range; today must use only its own
    assert scan(make_day({8: 101.6}, previous_day=True), 8) is not None
    assert scan(make_day({8: 101.6}, previous_day=False), 8) is not None


def test_beta_says_nothing_when_bars_carry_no_times():
    bars = Bars([100.0] * 60, [100.5] * 60, [99.5] * 60, [100.0] * 60, [None] * 60, [1.0] * 60)
    assert Beta().scan(bars, 59) is None


# ---- neither looks ahead ---------------------------------------------------------


def random_candles(n, seed, hourly=True):
    rng = random.Random(seed)
    price, out = 1.1, []
    start = datetime(2025, 1, 6, tzinfo=timezone.utc)
    for i in range(n):
        new = price + rng.gauss(0, 0.001)
        out.append(SimpleNamespace(open=price, close=new, high=max(price, new) + abs(rng.gauss(0, 0.0003)),
                                   low=min(price, new) - abs(rng.gauss(0, 0.0003)),
                                   time=start + timedelta(hours=i if hourly else 4 * i)))
        price = new
    return out


def test_alpha_does_not_look_ahead():
    assert lookahead_violations(Alpha(), Bars.from_candles(random_candles(500, 1, hourly=False)), step=7) == []


def test_beta_does_not_look_ahead():
    assert lookahead_violations(Beta(), Bars.from_candles(random_candles(24 * 40, 2)), step=5) == []


# ---- run through the whole machine on noise: no crash, no false pass -------------


def test_both_run_end_to_end_and_neither_passes_on_a_random_walk():
    candles = {sym: random_candles(24 * 120, seed) for sym, seed in (("EURUSD", 3), ("GBPUSD", 4))}
    for name in ("alpha", "beta"):
        report = run_playbook(REGISTRY[name](), candles, {"EURUSD": 0.00010, "GBPUSD": 0.00012})
        assert report.passed() == []
        assert all(r.real.n > 0 for r in report.rows), f"{name} never traded on 120 days of data"
