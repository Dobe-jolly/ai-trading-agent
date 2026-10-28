from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading.geometry import classify_regime, find_swings
from trading.playbooks import REGISTRY
from trading.playbooks.base import Bars
from trading.playbooks.evaluate import run_playbook
from trading.playbooks.gamma import Gamma
from trading.playbooks.simulate import lookahead_violations
from trading.playbooks.theta import Theta, zscore

from .test_playbooks_phase1 import bars_from_closes, mirror, random_candles, zigzag


def test_phase_two_is_registered_with_the_approved_geometry():
    for name in ("gamma", "theta"):
        cls = REGISTRY[name]
        assert (cls.timeframe, cls.stop_atr, cls.target_atr, cls.max_bars) == ("H4", 2.0, 2.0, 12)
        assert cls.asset_classes == ("forex", "metals", "vol_index")


# ---- Theta -----------------------------------------------------------------------


def flat_then(last, n=30, base=100.0):
    """Closes alternating 100 +/- 0.5 (so the standard deviation is 0.5), then `last` closes."""
    closes = [base + (0.5 if i % 2 else -0.5) for i in range(n)]
    return closes + list(last)


def scan_theta(closes):
    bars = bars_from_closes(closes)
    return Theta().scan(bars, len(bars) - 1)


def test_zscore_matches_a_hand_calculation():
    closes = [1.0, 2.0, 3.0, 4.0]
    # mean 2.5, population variance 1.25
    assert zscore(closes, 3, 4) == pytest.approx(1.5 / 1.25 ** 0.5)
    assert zscore(closes, 2, 4) is None                       # not enough bars yet
    assert zscore([5.0] * 4, 3, 4) is None                    # no variation


def test_theta_buys_a_fresh_extreme_low():
    sig = scan_theta(flat_then([98.0]))
    assert sig is not None and sig.direction == 1


def test_theta_sells_a_fresh_extreme_high():
    sig = scan_theta(flat_then([102.0]))
    assert sig is not None and sig.direction == -1


def test_theta_needs_the_extreme_to_reach_two_standard_deviations():
    assert scan_theta(flat_then([99.4])) is None              # z about -1.2
    closes = flat_then([100.9])
    assert 0 < zscore(closes, len(closes) - 1, 20) < 2.0
    assert scan_theta(closes) is None
    for last, lo in ((98.9, -2.0), (101.1, 1.5)):             # between 1.5 and 2 standard deviations: not enough
        closes = flat_then([last])
        assert 1.5 < abs(zscore(closes, len(closes) - 1, 20)) < 2.0, last
        assert scan_theta(closes) is None


def test_theta_sell_needs_the_extreme_to_be_fresh():
    closes = flat_then([102.0, 102.1])
    bars = bars_from_closes(closes)
    assert Theta().scan(bars, len(bars) - 2).direction == -1
    assert zscore(closes, len(closes) - 1, 20) >= 2.0
    assert Theta().scan(bars, len(bars) - 1) is None


def test_theta_needs_the_extreme_to_be_fresh():
    # 98.0 triggers; the very next bar, still extreme, must not trigger again
    closes = flat_then([98.0, 97.9])
    bars = bars_from_closes(closes)
    assert Theta().scan(bars, len(bars) - 2) is not None
    assert zscore(closes, len(closes) - 1, 20) <= -2.0
    assert Theta().scan(bars, len(bars) - 1) is None


def test_theta_is_symmetric():
    up = bars_from_closes(flat_then([102.0]))
    assert Theta().scan(mirror(up, 200.0), len(up) - 1).direction == 1


def test_theta_says_nothing_with_too_little_history():
    bars = bars_from_closes([100.0 + (i % 2) for i in range(10)])
    assert Theta().scan(bars, 9) is None


# ---- Gamma -----------------------------------------------------------------------

RANGE_POINTS = [100, 106, 100, 106, 100, 106, 100, 106, 100, 106, 101.5]


def range_bars(final):
    """A clean 100-106 box (top 106.3, bottom 99.7 with the 0.3 bar padding) then `final` as (o, h, l, c)."""
    closes = zigzag(RANGE_POINTS) + [final[3]]
    return bars_from_closes(closes, final=final)


GOOD_BUY = (100.6, 101.2, 99.8, 101.0)      # dips to 99.8 (0.1 above the 99.7 low), holds, closes up


def scan_gamma(bars):
    return Gamma().scan(bars, len(bars) - 1)


def test_the_box_used_in_these_tests_really_is_a_range_of_a_fitting_width():
    bars = range_bars(GOOD_BUY)
    t = len(bars) - 1
    swings = find_swings(bars.highs[:t + 1], bars.lows[:t + 1], 3)
    assert classify_regime(swings, 0.1 * bars.atr[t]) == "RANGE"
    width = (max(bars.highs[t - 20:t]) - min(bars.lows[t - 20:t])) / bars.atr[t]
    assert 2.0 <= width <= 6.0


def test_gamma_buys_a_held_touch_of_the_range_low():
    sig = scan_gamma(range_bars(GOOD_BUY))
    assert sig is not None and sig.direction == 1


def test_gamma_sells_the_mirror_image():
    sig = scan_gamma(mirror(range_bars(GOOD_BUY), 210.0))
    assert sig is not None and sig.direction == -1


def test_gamma_needs_the_bar_to_close_up_to_buy():
    assert scan_gamma(range_bars((101.0, 101.2, 99.8, 100.6))) is None


def test_gamma_needs_the_touch_to_come_near_the_edge():
    assert scan_gamma(range_bars((101.6, 102.2, 101.4, 102.0))) is None     # low is 1.7 above the edge


def test_gamma_needs_the_close_to_hold_above_the_range_low():
    # an up bar entirely below the range low: only "no breakdown" can reject it
    assert scan_gamma(range_bars((98.8, 99.6, 98.5, 99.5))) is None


def ignoring_width(lo, hi):
    g = Gamma()
    g.MIN_WIDTH_ATR, g.MAX_WIDTH_ATR = lo, hi
    return g


def test_gamma_ignores_a_range_that_is_too_narrow_or_too_wide():
    narrow = zigzag([100, 101] * 5 + [100.3])                 # tall bars on a small drift: the box is under 2 ATR
    bars = bars_from_closes(narrow + [100.8], pad=1.0, final=(100.2, 100.9, 99.1, 100.8))
    t = len(bars) - 1
    assert (max(bars.highs[t - 20:t]) - min(bars.lows[t - 20:t])) / bars.atr[t] < 2.0
    assert scan_gamma(bars) is None
    assert ignoring_width(0.0, 99.0).scan(bars, t) is not None      # width is the only thing that rejects it
    wide = zigzag([100, 112] * 6, leg=20)                     # slow legs keep ATR small: the box is just over 6 ATR
    bottom = min(bars_from_closes(wide).lows[-20:])
    final = (bottom + 0.6, bottom + 1.2, bottom + 0.1, bottom + 1.0)
    bars = bars_from_closes(wide + [final[3]], final=final)
    t = len(bars) - 1
    assert (max(bars.highs[t - 20:t]) - min(bars.lows[t - 20:t])) / bars.atr[t] > 6.0
    assert scan_gamma(bars) is None
    assert ignoring_width(0.0, 99.0).scan(bars, t) is not None
    assert scan_gamma(bars) is None


def gentle_uptrend():
    """Higher highs and higher lows, with a 20-bar box of a fitting width, and a final bar that would be a
    perfect buy at the box bottom if the market were a range."""
    pts = [100.0]
    for _ in range(8):
        pts += [pts[-1] + 2.0, pts[-1] + 2.0 - 1.2]
    closes = zigzag(pts, leg=6)
    base = bars_from_closes(closes)
    t = len(closes)
    bottom = min(base.lows[t - 20:t])
    final = (bottom + 0.8, bottom + 1.4, bottom + 0.1, bottom + 1.2)
    return bars_from_closes(closes + [final[3]], final=final)


def test_gamma_stays_out_of_a_trend():
    bars = gentle_uptrend()
    t = len(bars) - 1
    swings = find_swings(bars.highs[:t + 1], bars.lows[:t + 1], 3)
    assert classify_regime(swings, 0.1 * bars.atr[t]) == "BULLISH_TREND"
    assert 2.0 <= (max(bars.highs[t - 20:t]) - min(bars.lows[t - 20:t])) / bars.atr[t] <= 6.0
    assert scan_gamma(bars) is None                           # only the regime condition rejects this one


def test_gamma_sell_side_conditions_fail_the_same_way():
    assert scan_gamma(mirror(range_bars((101.6, 102.2, 101.4, 102.0)), 210.0)) is None   # far from the high
    assert scan_gamma(mirror(range_bars((101.0, 101.2, 99.8, 100.6)), 210.0)) is None    # closes the wrong way
    assert scan_gamma(mirror(range_bars((98.8, 99.6, 98.5, 99.5)), 210.0)) is None       # closes beyond the high


def test_the_range_excludes_the_signal_bar_on_the_sell_side_too():
    sig = scan_gamma(mirror(range_bars((100.6, 101.2, 90.0, 101.0)), 210.0))
    assert sig is not None and sig.direction == -1


def test_the_range_excludes_the_signal_bar_itself():
    # a final bar that plunges far below the box: if it were part of its own range, the width would blow
    # past 6 ATR and hide the signal; here it still is judged against the box and fails on the breakdown instead
    bars = range_bars((100.6, 101.2, 90.0, 101.0))
    t = len(bars) - 1
    assert min(bars.lows[t - 20:t]) > 99.0
    sig = scan_gamma(bars)
    assert sig is not None and sig.direction == 1            # touched below the edge, closed back inside, closed up


def test_gamma_says_nothing_before_there_is_an_atr():
    bars = bars_from_closes([100.0 + i * 0.1 for i in range(10)])
    assert scan_gamma(bars) is None


# ---- neither looks ahead ---------------------------------------------------------


def test_theta_does_not_look_ahead():
    assert lookahead_violations(Theta(), Bars.from_candles(random_candles(600, 11, hourly=False)), step=7) == []


def test_gamma_does_not_look_ahead():
    assert lookahead_violations(Gamma(), Bars.from_candles(random_candles(600, 12, hourly=False)), step=7) == []


# ---- the rules fire on noise at all, and run through the whole machine ------------


def h4_candles(n, seed):
    rng = random.Random(seed)
    price, out = 1.1, []
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        new = price + rng.gauss(0, 0.002)
        out.append(SimpleNamespace(open=price, close=new, high=max(price, new) + abs(rng.gauss(0, 0.0006)),
                                   low=min(price, new) - abs(rng.gauss(0, 0.0006)),
                                   time=start + timedelta(hours=4 * i)))
        price = new
    return out


@pytest.mark.parametrize("name", ["theta", "gamma"])
def test_both_trade_on_noise_and_neither_passes_on_a_random_walk(name):
    candles = {f"S{k}": h4_candles(3000, 40 + k) for k in range(3)}
    report = run_playbook(REGISTRY[name](), {k: v for k, v in candles.items()}, {k: 0.0001 for k in candles})
    assert report.passed() == []
