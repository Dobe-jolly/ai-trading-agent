from __future__ import annotations

import json

import pytest

from trading.geometry import (
    analyze, classify_regime, find_breaks, find_gaps, find_levels, find_swings,
)

PAD = 0.05


def path(points: list[float], leg: int = 5) -> list[float]:
    """Closes moving in straight lines between `points`, `leg` bars per leg."""
    closes = [points[0]]
    for a, b in zip(points, points[1:]):
        closes += [a + (b - a) * k / leg for k in range(1, leg + 1)]
    return closes


def candles(points: list[float], leg: int = 5):
    closes = path(points, leg)
    return [c + PAD for c in closes], [c - PAD for c in closes], closes


# ---- swings ----------------------------------------------------------------------


def test_swings_land_on_the_turning_points():
    highs, lows, _ = candles([10, 20, 12, 22, 14, 24, 16])
    swings = find_swings(highs, lows, n=3)
    assert [s.index for s in swings if s.kind == "high"] == [5, 15, 25]
    assert [s.index for s in swings if s.kind == "low"] == [10, 20]


def test_last_n_bars_never_hold_a_swing():
    # The peak is two bars from the end; with n=3 it is not confirmed yet, so it is not reported.
    highs = [1, 2, 3, 4, 5, 4, 3, 5, 6, 5]
    lows = [h - 0.5 for h in highs]
    assert all(s.index <= len(highs) - 1 - 3 for s in find_swings(highs, lows, n=3))


def test_flat_top_gives_one_swing_not_two():
    highs = [1, 2, 3, 5, 5, 3, 2, 1]
    lows = [h - 0.5 for h in highs]
    assert len([s for s in find_swings(highs, lows, n=2) if s.kind == "high"]) == 1


def test_swing_n_must_be_positive():
    with pytest.raises(ValueError):
        find_swings([1, 2], [1, 2], n=0)


# ---- regime ----------------------------------------------------------------------


@pytest.mark.parametrize("points, expected", [
    ([10, 20, 12, 22, 14, 24, 16], "BULLISH_TREND"),
    ([24, 14, 22, 12, 20, 10, 18], "BEARISH_TREND"),
    ([10, 20, 12, 20, 12, 20, 12], "RANGE"),
])
def test_regime(points, expected):
    highs, lows, _ = candles(points)
    assert classify_regime(find_swings(highs, lows, 3)) == expected


def test_regime_is_undefined_without_enough_swings():
    highs, lows, _ = candles([10, 20, 12])
    assert classify_regime(find_swings(highs, lows, 3)) == "UNDEFINED"


def test_small_moves_inside_the_tolerance_count_as_range():
    highs, lows, _ = candles([10, 20, 12, 20.2, 12.2, 20.4, 12.4])
    swings = find_swings(highs, lows, 3)
    assert classify_regime(swings, tol=0.0) == "BULLISH_TREND"
    assert classify_regime(swings, tol=0.5) == "RANGE"


# ---- breaks of structure ---------------------------------------------------------


def test_continuation_breaks_are_bos():
    highs, lows, closes = candles([10, 20, 12, 22, 14, 24])
    breaks = find_breaks(closes, find_swings(highs, lows, 3), n=3)
    assert [(b.kind, b.direction) for b in breaks][:2] == [("BOS", "up"), ("BOS", "up")]


def test_break_against_the_trend_is_a_choch():
    highs, lows, closes = candles([10, 20, 12, 22, 8, 18])
    breaks = find_breaks(closes, find_swings(highs, lows, 3), n=3)
    kinds = [(b.kind, b.direction) for b in breaks]
    assert kinds[0] == ("BOS", "up")
    assert ("CHOCH", "down") in kinds


def test_a_break_is_judged_only_against_swings_already_confirmed():
    highs, lows, closes = candles([10, 20, 12, 22, 8, 18])
    n = 3
    for b in find_breaks(closes, find_swings(highs, lows, n), n):
        assert b.index >= b.swing_index + n


def test_each_swing_breaks_once():
    highs, lows, closes = candles([10, 20, 12, 22, 14, 24])
    breaks = find_breaks(closes, find_swings(highs, lows, 3), n=3)
    assert len({(b.direction, b.swing_index) for b in breaks}) == len(breaks)


# ---- liquidity: equal levels and sweeps ------------------------------------------

EQUAL_HIGHS = [10, 20, 12, 20, 12, 17]          # two tops at the same price


def with_last_candle(points, high, low, close):
    highs, lows, closes = candles(points)
    return highs + [high], lows + [low], closes + [close]


def level_for(highs, lows, closes, kind="high"):
    swings = find_swings(highs, lows, 3)
    levels = [lv for lv in find_levels(highs, lows, closes, swings, tol=0.3) if lv.source == "equal" and lv.kind == kind]
    assert len(levels) == 1
    return levels[0]


def test_two_tops_at_the_same_price_form_an_equal_high():
    lv = level_for(*candles(EQUAL_HIGHS))
    assert (lv.touches, lv.status, round(lv.price, 2)) == (2, "intact", 20.05)


def test_tops_far_apart_are_not_equal():
    highs, lows, closes = candles([10, 20, 12, 23, 12, 17])
    swings = find_swings(highs, lows, 3)
    levels = find_levels(highs, lows, closes, swings, tol=0.3)
    assert [lv for lv in levels if lv.source == "equal" and lv.kind == "high"] == []


def test_wick_through_and_close_back_inside_is_a_sweep():
    lv = level_for(*with_last_candle(EQUAL_HIGHS, high=21.0, low=16.0, close=17.5))
    assert lv.status == "swept" and lv.extreme == 21.0


def test_close_beyond_the_level_is_a_breakout_not_a_sweep():
    lv = level_for(*with_last_candle(EQUAL_HIGHS, high=22.0, low=17.0, close=21.5))
    assert lv.status == "broken" and lv.extreme is None


def test_equal_low_swept_and_reclaimed_is_bullish():
    points = [20, 10, 18, 10, 18, 13]
    highs, lows, closes = candles(points)
    highs, lows, closes = highs + [14.0], lows + [9.0], closes + [13.5]
    lv = level_for(highs, lows, closes, kind="low")
    assert lv.status == "swept" and lv.extreme == 9.0


def test_the_first_poke_decides_a_later_sweep_does_not_count():
    # First candle through the level closes above it (breakout). A later wick-and-reject
    # does not turn it back into a sweep.
    highs, lows, closes = candles(EQUAL_HIGHS)
    highs += [22.0, 21.0]
    lows += [17.0, 16.0]
    closes += [21.5, 17.5]
    assert level_for(highs, lows, closes).status == "broken"


# ---- fair value gaps -------------------------------------------------------------


def gap_at(gaps, index):
    return next(g for g in gaps if g.index == index)


HIGHS = [10, 10.5, 13, 13.2, 13.4]
LOWS = [9, 9.5, 12.5, 12.6, 12.7]      # later lows stay above the gap, so it starts untouched


def test_bullish_gap_and_its_edges():
    g = gap_at(find_gaps(HIGHS, LOWS), 2)
    assert (g.direction, g.bottom, g.top, g.fill) == ("bullish", 10, 12.5, 0.0)


def test_partial_fill_is_measured_and_full_fill_removes_the_gap():
    partial = gap_at(find_gaps(HIGHS + [12.0], LOWS + [11.0]), 2)
    assert partial.fill == pytest.approx(0.6)
    assert not [g for g in find_gaps(HIGHS + [12.0], LOWS + [9.9]) if g.index == 2]


def test_bearish_gap_is_the_mirror():
    highs = [14, 13.5, 11, 11.5, 11.2]
    lows = [13, 12.5, 10, 10.5, 10.2]
    g = gap_at(find_gaps(highs, lows), 2)
    assert (g.direction, g.bottom, g.top) == ("bearish", 11, 13)


def test_gaps_smaller_than_the_minimum_are_ignored():
    assert not [g for g in find_gaps(HIGHS, LOWS, min_size=3.0) if g.index == 2]


# ---- analyze ---------------------------------------------------------------------


def test_analyze_reports_an_intact_equal_high_and_regime():
    out = analyze(*candles(EQUAL_HIGHS + [14, 16]))
    assert out["nearest_equal_high"] == 20.05
    # price is 16.0 and the level 20.05, so it is a few ATRs away, and that distance is reported
    assert out["nearest_equal_high_atr"] == pytest.approx((20.05 - out["price"]) / out["atr"], abs=0.06)
    assert out["nearest_equal_low_atr"] > 0       # the equal lows at 11.95 are also still intact
    assert out["recent_sweep"] is None
    assert out["regime"] in {"RANGE", "BULLISH_TREND", "BEARISH_TREND"}


def test_analyze_reports_a_recent_sweep_with_its_stop_anchor():
    out = analyze(*with_last_candle(EQUAL_HIGHS, high=21.0, low=16.0, close=17.5))
    sweep = out["recent_sweep"]
    assert sweep["signal"] == "HIGH_SWEPT_AND_REJECTED" and sweep["direction"] == "bearish"
    assert sweep["level"] == 20.05 and sweep["wick_extreme"] == 21.0 and sweep["bars_ago"] == 0
    assert out["nearest_equal_high"] is None      # that liquidity has been taken


def test_old_sweeps_drop_out_of_recent_sweep():
    highs, lows, closes = with_last_candle(EQUAL_HIGHS, high=21.0, low=16.0, close=17.5)
    for _ in range(8):
        highs, lows, closes = highs + [17.6], lows + [17.4], closes + [17.5]
    assert analyze(highs, lows, closes)["recent_sweep"] is None


def test_analyze_flags_price_sitting_in_an_unfilled_gap():
    highs, lows, closes = candles([10, 20, 12, 20, 12, 17])
    # impulse up that leaves a gap, then a pullback candle that dips back into it
    highs += [17.5, 21.0, 21.5, 21.2, 19.0]
    lows += [17.1, 19.0, 20.5, 20.8, 18.0]
    closes += [17.4, 20.8, 21.2, 21.0, 18.6]
    out = analyze(highs, lows, closes)
    assert out["fvg_zone_active"] is True
    # The latest candle (18.0-19.0) overlaps both gaps the impulse left; the newest is reported.
    assert out["fvg_boundaries"] == [17.5, 20.5]
    # Three gaps are active: the two bullish ones above, plus a bearish one the pullback candle
    # itself created (which is why it is not counted as "price sitting in a gap").
    assert [g["direction"] for g in out["active_fvgs"]].count("bullish") == 2
    assert len(out["active_fvgs"]) == 3


def test_analyze_is_json_safe_deterministic_and_does_not_touch_its_input():
    highs, lows, closes = candles(EQUAL_HIGHS + [14, 16])
    snapshot = (list(highs), list(lows), list(closes))
    first = analyze(highs, lows, closes)
    json.dumps(first)
    assert analyze(highs, lows, closes) == first
    assert (highs, lows, closes) == snapshot


def test_analyze_needs_enough_candles():
    assert "error" in analyze([1.0] * 10, [0.5] * 10, [0.8] * 10)
    assert "error" in analyze([1.0] * 40, [0.5] * 39, [0.8] * 40)
