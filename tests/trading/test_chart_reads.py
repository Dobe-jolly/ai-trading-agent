from __future__ import annotations

import json
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from trading import chart_reads as cr
from trading.chart_reads import (
    ReadStore, base_rates, clean_read, report, run_trade, score_bias, score_entry, score_level, score_read,
    score_sweep,
)
from trading.config import Settings
from trading.decision import SUBMIT_TRADE_TOOL, TradeDecision
from trading.loop import TradeResult
from trading.runner import TradingRunner

from .test_decision_logging import QuoteBroker
from .test_runner_breakers import ExplodingAgent, FakePaper

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float


def bar(i, o, h, l, c):
    return Bar(T0 + timedelta(hours=i), o, h, l, c)


def flat(n, price=100.0, rng=1.0, start=0):
    return [bar(start + i, price, price + rng / 2, price - rng / 2, price) for i in range(n)]


# ---- clean_read -----------------------------------------------------------------------


def test_non_dict_and_empty_reads_are_dropped():
    assert clean_read(None) is None and clean_read("x") is None and clean_read([1]) is None


def test_an_honest_unclear_read_is_kept():
    assert clean_read({"bias": "unclear"}) == {"bias": "unclear"}
    assert clean_read({"bias": "sideways-ish"})["bias"] == "unclear"


def test_bad_levels_are_dropped_good_ones_kept_and_zone_edges_ordered():
    r = clean_read({"bias": "up", "levels": [
        {"type": "liquidity_high", "price": 1.1},
        {"type": "banana", "price": 1.2},
        {"type": "fvg", "price": 1.3},                       # missing nothing: a line-like gap is allowed
        {"type": "fvg", "price": 1.5, "price_to": 1.4},       # edges given high-first
        {"type": "major_level", "price": -3},
        {"type": "major_level", "price": "abc"},
        "junk",
    ]})
    assert [(l["type"], l["price"], l.get("price_to")) for l in r["levels"]] == [
        ("liquidity_high", 1.1, None), ("fvg", 1.3, None), ("fvg", 1.4, 1.5)]


def test_at_most_eight_levels():
    r = clean_read({"bias": "up", "levels": [{"type": "major_level", "price": 1 + i} for i in range(20)]})
    assert len(r["levels"]) == 8


def test_price_to_is_ignored_for_plain_levels():
    r = clean_read({"bias": "up", "levels": [{"type": "liquidity_low", "price": 5, "price_to": 6}]})
    assert "price_to" not in r["levels"][0]


def test_entry_idea_geometry_is_checked():
    good_buy = {"direction": "buy", "price": 100, "invalidation": 99, "target": 102}
    good_sell = {"direction": "sell", "price": 100, "invalidation": 101, "target": 98}
    assert clean_read({"bias": "up", "entry_idea": good_buy})["entry_idea"]["target"] == 102
    assert "entry_idea" in clean_read({"bias": "down", "entry_idea": good_sell})
    assert "entry_idea" not in clean_read({"bias": "up", "entry_idea": {**good_buy, "invalidation": 101}})
    assert "entry_idea" not in clean_read({"bias": "up", "entry_idea": {**good_buy, "target": 99.5}})
    assert "entry_idea" not in clean_read({"bias": "down", "entry_idea": {**good_sell, "target": 101.5}})
    assert "entry_idea" not in clean_read({"bias": "up", "entry_idea": {"direction": "buy", "price": 100}})


def test_sweep_is_validated_and_bars_ago_capped():
    r = clean_read({"bias": "down", "sweep": {"level_price": 10, "direction": "up", "bars_ago": 999}})
    assert r["sweep"] == {"level_price": 10.0, "direction": "up", "bars_ago": 24}
    assert "sweep" not in clean_read({"bias": "down", "sweep": {"level_price": 10, "direction": "sideways"}})
    assert clean_read({"bias": "down", "sweep": {"level_price": 10, "direction": "down", "bars_ago": "x"}})["sweep"]["bars_ago"] == 0


# ---- the decision carries the read, and a bad read never blocks it ---------------------


def test_schema_offers_chart_read_but_does_not_require_it():
    props = SUBMIT_TRADE_TOOL["input_schema"]["properties"]
    assert "chart_read" in props and "chart_read" not in SUBMIT_TRADE_TOOL["input_schema"]["required"]


def wait_input(**extra):
    return {"action": "wait", "symbol": "EURUSD", "direction": "", "risk_pct": 1, "sl_pips": 0, "tp_pips": 0,
            "reason": "r", "confidence": 0.1, "setup": "wait", **extra}


def test_decision_carries_the_cleaned_read():
    d = TradeDecision.from_tool_input(wait_input(chart_read={"bias": "up", "levels": [{"type": "fvg", "price": 1.0}]}))
    assert d.chart_read["bias"] == "up" and len(d.chart_read["levels"]) == 1


def test_a_garbage_read_never_blocks_a_decision():
    for junk in ("garbage", 5, [], {"bias": 3, "levels": "no"}, {"levels": [None]}):
        TradeDecision.from_tool_input(wait_input(chart_read=junk))
    assert TradeDecision.from_tool_input(wait_input()).chart_read is None


# ---- storage --------------------------------------------------------------------------


def test_store_round_trip_and_maturity(tmp_path):
    s = ReadStore(tmp_path / "a.sqlite")
    old = s.record("r1", "live", "EURUSD", {"bias": "up"}, "wait", created_at=T0 - timedelta(days=5))
    s.record("r1", "live", "XAUUSD", {"bias": "down"}, "open", created_at=T0)
    due = s.unscored(timedelta(days=3), now=T0)
    assert [d["id"] for d in due] == [old]
    s.save_score(old, {"ok": True, "levels": []})
    assert s.unscored(timedelta(days=3), now=T0) == []
    rows = s.scored()
    assert rows[0]["read"] == {"bias": "up"} and rows[0]["score"]["ok"] is True and s.count() == 2


# ---- levels ---------------------------------------------------------------------------


def levelled(path):
    """Bars after the read, one per entry in path as (high, low, close)."""
    return [bar(i, c, h, l, c) for i, (h, l, c) in enumerate(path)]


def test_a_support_level_that_is_touched_and_holds():
    bars = levelled([(101, 100.2, 100.5), (100.6, 99.9, 100.4), (101, 100.3, 100.8)] + [(101, 100.5, 100.8)] * 30)
    s = score_level({"type": "major_level", "price": 100.0}, 101.0, bars)
    assert s["touched"] and s["held"] is True


def test_a_support_level_that_is_touched_then_closed_through_is_broken():
    bars = levelled([(101, 100.2, 100.5), (100.6, 99.5, 99.6)] + [(100, 99, 99.5)] * 30)
    s = score_level({"type": "major_level", "price": 100.0}, 101.0, bars)
    assert s["touched"] and s["held"] is False


def test_a_level_never_reached_is_not_touched_and_has_no_hold():
    bars = levelled([(101.5, 100.5, 101)] * 30)
    s = score_level({"type": "liquidity_low", "price": 99.0}, 101.0, bars)
    assert s["touched"] is False and s["held"] is None


def test_touch_must_happen_within_24_bars():
    path = [(101.5, 100.5, 101)] * 24 + [(101, 99.8, 100.5)] * 5
    assert score_level({"type": "major_level", "price": 100.0}, 101.0, levelled(path))["touched"] is False
    path = [(101.5, 100.5, 101)] * 23 + [(101, 99.8, 100.5)] * 5
    assert score_level({"type": "major_level", "price": 100.0}, 101.0, levelled(path))["touched"] is True


def test_resistance_above_price_uses_the_mirror_logic():
    bars = levelled([(100.5, 99.5, 100), (101.2, 100.5, 100.8)] + [(100.9, 100, 100.5)] * 30)
    s = score_level({"type": "liquidity_high", "price": 101.0}, 100.0, bars)
    assert s["touched"] and s["held"] is True
    bars = levelled([(100.5, 99.5, 100), (101.8, 100.5, 101.6)] + [(102, 101, 101.5)] * 30)
    assert score_level({"type": "liquidity_high", "price": 101.0}, 100.0, bars)["held"] is False


def test_a_level_price_is_already_inside_is_not_scored():
    assert score_level({"type": "fvg", "price": 99.0, "price_to": 101.0}, 100.0, levelled([(101, 99, 100)] * 30)) is None


def test_zone_is_touched_by_its_near_edge_and_broken_beyond_the_far_edge():
    zone = {"type": "fvg", "price": 98.0, "price_to": 99.0}           # below price 100
    touched_edge = levelled([(100.5, 99.2, 99.5)] + [(100.5, 99.5, 100)] * 30)
    assert score_level(zone, 100.0, touched_edge)["touched"] is False
    touched_edge = levelled([(100.5, 99.0, 99.5)] + [(100.5, 99.5, 100)] * 30)
    s = score_level(zone, 100.0, touched_edge)
    assert s["touched"] and s["held"] is True
    through = levelled([(100.5, 99.0, 97.9)] + [(100, 97, 97.5)] * 30)
    assert score_level(zone, 100.0, through)["held"] is False


def test_placebo_is_the_mirror_level_at_the_same_distance():
    """Support 1.0 below price; the mirror sits 1.0 above price. Price rises to 101: the real level is
    not touched, the placebo is."""
    bars = levelled([(101.2, 100.5, 101)] * 30)
    s = score_level({"type": "major_level", "price": 99.0}, 100.0, bars)
    assert s["touched"] is False and s["placebo_touch"] == 1.0 and s["placebo_touched_n"] == 1


def test_placebo_hold_is_scored_too():
    bars = levelled([(101.2, 100.5, 101)] * 30)
    s = score_level({"type": "major_level", "price": 99.0}, 100.0, bars)
    assert s["placebo_held_n"] == 1                                   # touched 101 from below, never closed above it


# ---- sweep ----------------------------------------------------------------------------


def test_a_real_sweep_of_highs_is_verified():
    c = flat(40)
    c[30] = bar(30, 100, 102, 99.5, 99.8)           # wick through 101, closes back below
    assert score_sweep({"level_price": 101.0, "direction": "up", "bars_ago": 5}, c, start=35) == {"verified": True}


def test_closing_back_inside_on_the_same_bar_or_three_bars_later_counts():
    c = flat(40)
    c[30] = bar(30, 100, 102, 100, 100.5)                 # wick through 101, closed back inside on the same bar
    assert score_sweep({"level_price": 101.0, "direction": "up", "bars_ago": 6}, c, start=36)["verified"] is True
    c = flat(40)
    for i in (30, 31, 32):
        c[i] = bar(i, 101.5, 102, 101.2, 101.5)           # three closes above the level
    c[33] = bar(33, 101.5, 101.8, 100.0, 100.5)           # closes back below three bars after the first break
    assert score_sweep({"level_price": 101.0, "direction": "up", "bars_ago": 6}, c, start=36)["verified"] is True


def test_a_claim_older_than_its_stated_age_is_not_found():
    c = flat(60)
    c[10] = bar(10, 100, 102, 100, 100.5)                 # a real sweep, but 30 bars before the read
    assert score_sweep({"level_price": 101.0, "direction": "up", "bars_ago": 3}, c, start=40)["verified"] is False


def test_a_claimed_sweep_where_price_never_traded_through_is_false():
    assert score_sweep({"level_price": 105.0, "direction": "up", "bars_ago": 3}, flat(40), start=35)["verified"] is False


def test_sweep_of_lows_mirrors_and_ignores_bars_after_the_read():
    c = flat(40)
    c[37] = bar(37, 100, 100.2, 98, 100)            # after `start`: must not count
    assert score_sweep({"level_price": 99.0, "direction": "down", "bars_ago": 2}, c, start=35)["verified"] is False
    c = flat(40)
    c[33] = bar(33, 100, 100.2, 98, 99.5)
    assert score_sweep({"level_price": 99.0, "direction": "down", "bars_ago": 2}, c, start=35)["verified"] is True


def test_a_sweep_that_closed_beyond_and_stayed_there_is_a_break_not_a_sweep():
    c = flat(40)
    for i in range(30, 36):
        c[i] = bar(i, 102, 103, 101.5, 102)
    assert score_sweep({"level_price": 101.0, "direction": "up", "bars_ago": 5}, c, start=36)["verified"] is False


# ---- bias -----------------------------------------------------------------------------


BASE = {"up": 0.5, "range": 0.4, "n": 100}


def test_bias_up_is_a_hit_when_price_is_higher_12_bars_on():
    bars = [bar(i, 100, 101, 99, 100 + i * 0.2) for i in range(20)]
    s = score_bias("up", 100.0, 1.0, bars, BASE)
    assert s["hit"] and s["expected"] == 0.5 and s["move_atr"] == pytest.approx(2.2)


def test_bias_down_expected_rate_is_the_complement():
    bars = [bar(i, 100, 101, 99, 99) for i in range(20)]
    s = score_bias("down", 100.0, 1.0, bars, {**BASE, "up": 0.7})
    assert s["hit"] and s["expected"] == pytest.approx(0.3)


def test_bias_uses_the_12th_bar_exactly():
    bars = [bar(i, 100, 101, 99, 101 if i == 11 else 99) for i in range(20)]
    assert score_bias("up", 100.0, 1.0, bars, BASE)["hit"] is True
    bars = [bar(i, 100, 101, 99, 99 if i == 11 else 101) for i in range(20)]
    assert score_bias("up", 100.0, 1.0, bars, BASE)["hit"] is False


def test_range_bias_hits_inside_one_atr_and_unclear_is_not_scored():
    bars = [bar(i, 100, 101, 99, 100.4) for i in range(20)]
    assert score_bias("range", 100.0, 1.0, bars, BASE)["hit"] is True
    bars = [bar(i, 100, 101, 99, 101.5) for i in range(20)]
    assert score_bias("range", 100.0, 1.0, bars, BASE)["hit"] is False
    assert score_bias("unclear", 100.0, 1.0, bars, BASE) is None


def test_base_rates_count_ups_and_ranges_from_history():
    c = [bar(i, 100 + i, 100.5 + i, 99.5 + i, 100 + i) for i in range(80)]    # rising 1 per bar, ATR ~1
    atr = cr.atr_series([x.high for x in c], [x.low for x in c], [x.close for x in c])
    b = base_rates(c, atr)
    assert b["up"] == 1.0 and b["range"] == 0.0 and b["n"] > 0


# ---- trade simulation -----------------------------------------------------------------


def test_stop_wins_a_tie_inside_a_bar():
    bars = [bar(0, 100, 103, 98.5, 100)]
    assert run_trade(bars, 0, 5, 1, 100.0, 99.0, 102.0, 0.0, target_from=0) == -1.0


def test_target_hit_pays_the_reward_in_r_less_the_spread():
    bars = [bar(0, 100, 100.5, 99.5, 100), bar(1, 100, 102.5, 99.5, 102)]
    r = run_trade(bars, 0, 5, 1, 100.0, 99.0, 102.0, 0.1, target_from=0)
    assert r == pytest.approx(2.0 - 0.1)


def test_sell_direction_mirrors():
    bars = [bar(0, 100, 100.5, 99.5, 100), bar(1, 100, 100.2, 97.5, 98)]
    assert run_trade(bars, 0, 5, -1, 100.0, 101.0, 98.0, 0.0, target_from=0) == pytest.approx(2.0)
    bars = [bar(0, 100, 101.2, 99.5, 100)]
    assert run_trade(bars, 0, 5, -1, 100.0, 101.0, 98.0, 0.0, target_from=0) == -1.0


def test_target_on_the_entry_bar_is_ignored_when_told_to():
    bars = [bar(0, 100, 103, 99.5, 102), bar(1, 102, 101.9, 101, 101.5)]
    r = run_trade(bars, 0, 1, 1, 100.0, 99.0, 102.0, 0.0, target_from=1)
    assert r == pytest.approx(1.5)                                   # timed out at the last close, not a target win


def test_timeout_marks_to_the_last_close():
    bars = [bar(i, 100, 100.4, 99.6, 100.5) for i in range(10)]
    assert run_trade(bars, 0, 4, 1, 100.0, 99.0, 103.0, 0.0, target_from=0) == pytest.approx(0.5)


# ---- entry ideas ----------------------------------------------------------------------


def quiet_history(n=400):
    rng = random.Random(7)
    out, p = [], 100.0
    for i in range(n):
        o = p
        p = o + rng.uniform(-0.3, 0.3)
        out.append(bar(i, o, max(o, p) + 0.2, min(o, p) - 0.2, p))
    return out


def test_an_entry_the_price_never_reaches_is_not_a_trade():
    idea = {"direction": "buy", "price": 90.0, "invalidation": 89.0, "target": 95.0}
    assert score_entry(idea, flat(80), 0.0, quiet_history(), seed=1) == {"reached": False}


def test_reached_entry_then_target_is_a_win_with_a_placebo():
    bars = flat(80)
    bars[3] = bar(3, 100, 100.4, 99.6, 100)
    bars[4] = bar(4, 100, 102.5, 99.9, 102.2)
    idea = {"direction": "buy", "price": 100.0, "invalidation": 99.0, "target": 102.0}
    s = score_entry(idea, bars, 0.0, quiet_history(), seed=1)
    assert s["reached"] and s["r"] == 2.0 and s["placebo_r"] is not None


def test_reached_entry_then_stop_is_minus_one_and_the_stop_bar_is_pessimistic():
    bars = flat(80)
    bars[3] = bar(3, 100, 100.4, 98.5, 99.5)         # reaches 100 and trades through the stop in the same bar
    idea = {"direction": "buy", "price": 100.0, "invalidation": 99.0, "target": 102.0}
    assert score_entry(idea, bars, 0.0, quiet_history(), seed=1)["r"] == -1.0


def test_entry_must_be_reached_within_24_bars():
    bars = flat(80, price=105.0)
    bars[24] = bar(24, 105, 105.5, 99.9, 100)
    idea = {"direction": "buy", "price": 100.0, "invalidation": 99.0, "target": 102.0}
    assert score_entry(idea, bars, 0.0, quiet_history(), seed=1) == {"reached": False}
    bars[24] = flat(1, 105.0)[0]
    bars[23] = bar(23, 105, 105.5, 99.9, 100)
    assert score_entry(idea, bars, 0.0, quiet_history(), seed=1)["reached"] is True


def test_placebo_is_deterministic_for_a_seed():
    bars = flat(80)
    bars[2] = bar(2, 100, 100.4, 99.6, 100)
    idea = {"direction": "buy", "price": 100.0, "invalidation": 99.0, "target": 102.0}
    h = quiet_history()
    assert score_entry(idea, bars, 0.0, h, seed=3) == score_entry(idea, bars, 0.0, h, seed=3)


def test_a_short_history_gives_no_placebo():
    bars = flat(80)
    bars[2] = bar(2, 100, 100.4, 99.6, 100)
    idea = {"direction": "buy", "price": 100.0, "invalidation": 99.0, "target": 102.0}
    assert score_entry(idea, bars, 0.0, quiet_history(60), seed=3)["placebo_r"] is None


# ---- scoring a whole read -------------------------------------------------------------


def history_with_future(extra_bars):
    past = [bar(i - 200, 101, 101.5, 100.5, 101) for i in range(200)]
    return past + [replace(b, time=T0 + timedelta(hours=i)) for i, b in enumerate(extra_bars)]


def test_a_read_without_enough_later_candles_is_not_scored_yet():
    c = history_with_future(flat(40))
    out = score_read({"bias": "up"}, c, T0, 0.0, BASE)
    assert out["ok"] is False and "later" in out["reason"]


def test_a_read_older_than_the_candles_cannot_be_scored():
    out = score_read({"bias": "up"}, history_with_future(flat(80)), T0 - timedelta(days=400), 0.0, BASE)
    assert out["ok"] is False


def test_only_bars_from_the_read_time_onward_are_the_future():
    """A touch of the level that happened BEFORE the read must not count."""
    c = history_with_future(flat(80, price=101.0))
    c[199] = replace(c[199], low=99.9)               # the bar before the read dips to 100
    out = score_read({"bias": "unclear", "levels": [{"type": "major_level", "price": 100.0}]}, c, T0, 0.0, BASE)
    assert out["ok"] and out["levels"][0]["touched"] is False


def test_end_to_end_read_scores_every_part():
    fut = flat(80, price=101.0)
    fut[5] = bar(5, 101, 101.2, 99.8, 100.2)          # touches 100 and holds
    fut[6] = bar(6, 100.2, 102.5, 100, 102.3)
    for i in range(7, 80):
        fut[i] = bar(i, 102.3, 102.8, 101.8, 102.3)   # rises: bias up hits
    read = {"bias": "up",
            "levels": [{"type": "major_level", "price": 100.0}],
            "sweep": {"level_price": 100.2, "direction": "down", "bars_ago": 1},
            "entry_idea": {"direction": "buy", "price": 100.0, "invalidation": 99.0, "target": 102.0}}
    c = history_with_future(fut)
    c[198] = replace(c[198], low=99.0, close=100.5)   # a real sweep of 100.2 lows before the read
    out = score_read(read, c, T0, 0.0, BASE, seed=2)
    assert out["ok"]
    assert out["levels"][0]["touched"] and out["levels"][0]["held"]
    assert out["sweep"]["verified"] is True
    assert out["bias"]["hit"] is True
    assert out["entry"]["reached"] and out["entry"]["r"] == 2.0


# ---- report ---------------------------------------------------------------------------


def make_rows(n, touched, placebo, entry_r=None, placebo_r=0.0):
    rows = []
    for i in range(n):
        score = {"ok": True,
                 "levels": [{"type": "liquidity_low", "touched": touched(i), "held": True,
                             "placebo_touch": placebo(i), "placebo_touched_n": 1, "placebo_held_n": 0}],
                 "sweep": {"verified": True}, "bias": {"bias": "up", "hit": touched(i), "expected": 0.5, "move_atr": 1},
                 "entry": ({"reached": True, "r": entry_r(i), "placebo_r": placebo_r} if entry_r else None)}
        rows.append({"symbol": "EURUSD", "read": {}, "score": score, "created_at": f"2026-11-{1 + i // 10:02d}"})
    return rows


def test_report_with_nothing_scored_says_so():
    assert "0 of 0" in report([])
    assert "0 of 1" in report([{"symbol": "X", "read": {}, "score": {"ok": False}}])


def test_a_level_type_that_beats_its_placebo_is_called_skill():
    rows = make_rows(60, touched=lambda i: i % 10 != 0, placebo=lambda i: float(i % 2))
    text = report(rows)
    assert "liquidity_low" in text and "SKILL" in text


def test_a_level_type_no_better_than_placebo_is_not_skill():
    rows = make_rows(60, touched=lambda i: i % 2 == 0, placebo=lambda i: float(i % 2 == 0))
    assert "SKILL" not in report(rows)


def test_too_few_reads_never_count_as_skill():
    rows = make_rows(10, touched=lambda i: True, placebo=lambda i: 0.0)
    assert "SKILL" not in report(rows)


def test_skill_must_also_hold_in_the_last_40_percent():
    rows = make_rows(60, touched=lambda i: i < 36, placebo=lambda i: 0.0 if i < 36 else 1.0)
    # first 60% all hits over a placebo of 0; the last 40% all misses against a placebo of 1
    assert "SKILL" not in report(rows)


def test_entry_ideas_beating_the_placebo_are_reported_with_the_difference():
    rows = make_rows(60, touched=lambda i: True, placebo=lambda i: 0.0, entry_r=lambda i: 1.0 if i % 3 else -1.0, placebo_r=-0.2)
    text = report(rows)
    assert "Entry ideas: 60" in text and "diff" in text and "placebo -0.20" in text


# ---- the scan stores the read ---------------------------------------------------------


class ReadingAgent(ExplodingAgent):
    def __init__(self, read):
        super().__init__()
        self._read = read

    def decide(self, symbol, account, positions):
        self.calls.append(symbol)
        d = TradeDecision.fallback("nothing to do")
        d = replace(d, chart_read=self._read(symbol))
        return TradeResult(decision=d)


@pytest.fixture
def runner(tmp_path):
    s = replace(
        Settings.from_env(tmp_path / "none.env"),
        mode="paper", state_db=tmp_path / "agent.sqlite", paper_db=tmp_path / "paper.sqlite",
        lockout_file=tmp_path / "LOCKOUT.paper", kill_switch_file=tmp_path / "STOP",
        daily_loss_limit_pct=60.0, blackout_utc="", max_latency_ms=1000.0, market_stale_min=0.0,
    )
    r = TradingRunner(s)
    r._broker = QuoteBroker({"EURUSD": (1.1170, 1.1172), "XAUUSD": (2000.0, 2000.5)})
    r._paper = FakePaper()
    return r


def test_each_symbols_read_is_stored_even_when_the_decision_is_wait(runner):
    runner._agent = ReadingAgent(lambda s: {"bias": "up" if s == "EURUSD" else "down"})
    runner.run(["EURUSD", "XAUUSD"])
    rows = {r["symbol"]: r for r in runner._reads.scored() + [dict(x, read=json.loads(x["read_json"])) for x in runner._reads.unscored(timedelta(seconds=-10))]}
    assert {k: v["read"]["bias"] for k, v in rows.items()} == {"EURUSD": "up", "XAUUSD": "down"}
    assert all(v["action"] == "wait" for v in rows.values())


def test_no_read_means_nothing_is_stored(runner):
    runner._agent = ReadingAgent(lambda s: None)
    runner.run(["EURUSD"])
    assert runner._reads.count() == 0


def test_a_storage_failure_does_not_stop_the_scan(runner):
    runner._agent = ReadingAgent(lambda s: {"bias": "up"})

    def boom(*a, **k):
        raise RuntimeError("disk full")

    runner._reads.record = boom
    summary = runner.run(["EURUSD", "XAUUSD"])
    assert summary.decided == 2 and sorted(runner._agent.calls) == ["EURUSD", "XAUUSD"]


# ---- boundaries (found by mutation checks) ---------------------------------------------


def test_the_bar_that_opens_exactly_at_the_read_time_is_the_future():
    fut = flat(80, price=101.0)
    fut[0] = bar(0, 101, 101.2, 99.9, 100.5)          # opens at T0, touches 100
    out = score_read({"bias": "unclear", "levels": [{"type": "major_level", "price": 100.0}]},
                     history_with_future(fut), T0, 0.0, BASE)
    assert out["levels"][0]["touched"] is True


def test_hold_is_judged_over_the_touch_bar_and_the_next_two_only():
    base = [(101, 100.2, 100.5)] * 3
    through = (100.5, 99.0, 99.2)
    held = levelled([(100.6, 99.9, 100.4), (101, 100.3, 100.6), (101, 100.3, 100.6), through] + [(100, 99, 99.5)] * 30)
    assert score_level({"type": "major_level", "price": 100.0}, 101.0, held)["held"] is True      # break on the 4th bar
    broke = levelled([(100.6, 99.9, 100.4), (101, 100.3, 100.6), through] + [(100, 99, 99.5)] * 30)
    assert score_level({"type": "major_level", "price": 100.0}, 101.0, broke)["held"] is False    # break on the 3rd bar


def test_price_exactly_on_the_edge_of_a_zone_counts_as_inside_it():
    assert score_level({"type": "fvg", "price": 99.0, "price_to": 100.0}, 100.0, levelled([(101, 99, 100)] * 30)) is None


def test_a_bar_that_only_touches_the_stop_exactly_stops_the_trade():
    bars = [bar(0, 100, 100.5, 99.0, 100)]
    assert run_trade(bars, 0, 3, 1, 100.0, 99.0, 102.0, 0.0, target_from=0) == -1.0
    bars = [bar(0, 100, 101.0, 99.5, 100)]
    assert run_trade(bars, 0, 3, -1, 100.0, 101.0, 98.0, 0.0, target_from=0) == -1.0


def test_the_target_is_not_taken_on_the_bar_that_reached_the_entry():
    bars = flat(80, price=101.0)                       # sits above the entry, never touching it
    bars[3] = bar(3, 100, 102.5, 99.6, 100.4)          # reaches 100 and also trades through 102 (order unknown)
    idea = {"direction": "buy", "price": 100.0, "invalidation": 99.0, "target": 102.0}
    s = score_entry(idea, bars, 0.0, quiet_history(), seed=1)
    assert s["r"] == pytest.approx(1.0)                # timed out at 101, not booked as a clean 2R win


def test_an_entry_exactly_at_the_bar_extreme_is_reached():
    bars = flat(80, price=105.0)
    bars[4] = bar(4, 105, 105.5, 100.0, 104)           # low is exactly the entry
    idea = {"direction": "buy", "price": 100.0, "invalidation": 99.0, "target": 102.0}
    assert score_entry(idea, bars, 0.0, quiet_history(), seed=1)["reached"] is True


def test_bias_thresholds_are_strict():
    bars = [bar(i, 100, 101, 99, 100) for i in range(20)]                  # no move at all
    assert score_bias("up", 100.0, 1.0, bars, BASE)["hit"] is False
    assert score_bias("down", 100.0, 1.0, bars, BASE)["hit"] is False
    bars = [bar(i, 100, 101, 99, 101.0) for i in range(20)]                 # exactly +1 ATR
    assert score_bias("range", 100.0, 1.0, bars, BASE)["hit"] is False


def test_exactly_enough_later_candles_is_enough_one_fewer_is_not():
    n = cr.MIN_AFTER
    assert score_read({"bias": "up"}, history_with_future(flat(n)), T0, 0.0, BASE)["ok"] is True
    assert score_read({"bias": "up"}, history_with_future(flat(n - 1)), T0, 0.0, BASE)["ok"] is False


def test_the_first_future_bar_cannot_confirm_a_sweep():
    c = flat(40)
    c[35] = bar(35, 100, 102, 100, 100.5)               # index == start: already the future
    assert score_sweep({"level_price": 101.0, "direction": "up", "bars_ago": 2}, c, start=35)["verified"] is False


def test_entry_idea_with_the_entry_equal_to_the_stop_is_dropped():
    assert "entry_idea" not in clean_read({"bias": "up", "entry_idea": {"direction": "buy", "price": 100, "invalidation": 100, "target": 102}})


def test_a_weakly_positive_last_40_percent_is_not_enough():
    def touched(i):
        k = i - 36
        return True if i < 36 or k < 8 or k >= 12 else False

    def placebo(i):
        k = i - 36
        return 0.0 if i < 36 or k < 8 else 1.0           # last 40%: 8 wins over placebo, 4 losses, 12 ties

    def level_line(text):
        return next(l for l in text.splitlines() if l.lstrip().startswith("liquidity_low"))

    rows = make_rows(60, touched=touched, placebo=placebo)
    assert "SKILL" not in level_line(report(rows))
    strong = make_rows(60, touched=lambda i: True, placebo=lambda i: 0.0 if i % 5 else 1.0)
    assert "SKILL" in level_line(report(strong))
