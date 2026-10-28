from __future__ import annotations

import json
import random
from types import SimpleNamespace
from typing import Optional

import pytest

from trading.indicators import atr as reference_atr
from trading.playbooks import REGISTRY, register
from trading.playbooks.base import Bars, Playbook, Signal, atr_series
from trading.playbooks.evaluate import (
    MIN_EDGE_OVER_PLACEBO, MIN_OOS_T, MIN_OOS_TRADES, MIN_T, MIN_TRADES, Stats, judge, run_playbook, stats,
)
from trading.playbooks.simulate import Placebo, Trade, lookahead_violations, simulate


# ---- helpers ---------------------------------------------------------------------


def flat_bars(opens, highs, lows, closes, atr=1.0) -> Bars:
    """Bars with a fixed ATR, so a stop of N ATR is exactly N price units."""
    return Bars(list(opens), list(highs), list(lows), list(closes), [None] * len(opens), [atr] * len(opens))


class OneShot(Playbook):
    """Signals once, at bar `at`, with a fixed geometry of stop 1, target 2, five bars."""
    name = "oneshot"
    asset_classes = ("forex",)
    warmup = 2
    stop_atr, target_atr, max_bars = 1.0, 2.0, 5

    def __init__(self, direction: int = 1, at: int = 3) -> None:
        self._direction, self._at = direction, at

    def scan(self, bars: Bars, t: int) -> Optional[Signal]:
        return Signal(self._direction) if t == self._at else None


def scenario(path_after_entry, direction=1, entry_open=100.0, spread=0.0, extra_bars=0):
    """Signal at t=3, entry at the open of bar 4 at `entry_open`, then the (open, high, low, close) bars."""
    pre = [(100.0, 100.5, 99.5, 100.0)] * 4
    post = list(path_after_entry) + [(entry_open, entry_open + 0.4, entry_open - 0.4, entry_open)] * extra_bars
    rows = pre + post
    rows[4] = (entry_open,) + tuple(rows[4][1:])
    bars = flat_bars(*zip(*rows))
    return simulate("EURUSD", bars, OneShot(direction), spread).trades


def inside(entry=100.0):
    return (entry, entry + 0.4, entry - 0.4, entry + 0.2)


# ---- ATR -------------------------------------------------------------------------


def test_the_rolling_atr_matches_the_reference_implementation_at_every_bar():
    rng = random.Random(4)
    c, h, l = [100.0], [100.5], [99.5]
    for _ in range(80):
        c.append(c[-1] + rng.gauss(0, 1))
        h.append(c[-1] + abs(rng.gauss(0, 0.5)))
        l.append(c[-1] - abs(rng.gauss(0, 0.5)))
    series = atr_series(h, l, c)
    for n in (20, 40, 81):
        # the reference rounds to 6 decimals, so compare to that precision
        assert series[n - 1] == pytest.approx(reference_atr(h[:n], l[:n], c[:n]), abs=1e-6)
    assert series[13] is None and series[14] is not None


# ---- simulator: fills ------------------------------------------------------------


def test_a_buy_that_reaches_the_target_makes_the_reward_in_r():
    (t,) = scenario([inside(), (100.0, 102.5, 99.6, 102.0)])
    assert (t.reason, round(t.r, 6), t.direction) == ("target", 2.0, 1)


def test_a_buy_that_reaches_the_stop_loses_one_r():
    (t,) = scenario([inside(), (100.0, 100.4, 98.9, 99.0)])
    assert (t.reason, round(t.r, 6)) == ("stop", -1.0)


def test_when_stop_and_target_share_a_bar_the_stop_is_assumed_first():
    (t,) = scenario([inside(), (100.0, 102.5, 98.5, 100.0)])
    assert (t.reason, round(t.r, 6)) == ("stop", -1.0)


def test_a_gap_through_the_stop_fills_at_the_open_which_is_worse_than_the_stop():
    (t,) = scenario([inside(), (98.0, 98.5, 97.5, 98.0)])
    assert (t.reason, round(t.r, 6)) == ("stop", -2.0)


def test_a_gap_over_the_target_fills_at_the_open_which_is_better_than_the_target():
    (t,) = scenario([inside(), (103.0, 103.5, 102.8, 103.2)])
    assert (t.reason, round(t.r, 6)) == ("target", 3.0)


def test_a_trade_that_goes_nowhere_is_closed_at_the_market_after_the_time_limit():
    (t,) = scenario([inside()] * 4 + [(100.0, 100.5, 99.5, 101.0)], extra_bars=3)
    assert t.reason == "time" and round(t.r, 6) == 1.0 and t.exit_index - t.entry_index == 4


def test_a_sell_is_the_mirror_image():
    (stop,) = scenario([inside(), (100.0, 101.2, 99.6, 101.0)], direction=-1)
    (target,) = scenario([inside(), (100.0, 100.3, 97.9, 98.0)], direction=-1)
    assert (stop.reason, round(stop.r, 6)) == ("stop", -1.0)
    assert (target.reason, round(target.r, 6)) == ("target", 2.0)


def test_the_entry_is_the_open_of_the_bar_after_the_signal_not_the_signal_bars_close():
    # signal bar closes at 100, the next bar opens at 105: the trade is struck at 105, so its target is 107
    (t,) = scenario([inside(105.0), (105.0, 107.1, 104.8, 107.0)], entry_open=105.0)
    assert (t.reason, round(t.r, 6)) == ("target", 2.0)


def test_one_spread_is_charged_per_trade_in_r():
    (stop,) = scenario([inside(), (100.0, 100.4, 98.9, 99.0)], spread=0.1)
    (target,) = scenario([inside(), (100.0, 102.5, 99.6, 102.0)], spread=0.1)
    assert round(stop.r, 6) == -1.1 and round(target.r, 6) == 1.9


def test_a_trade_still_open_when_the_data_ends_is_closed_at_the_last_close():
    (t,) = scenario([inside(), inside()])
    assert t.reason == "end"


# ---- simulator: bookkeeping ------------------------------------------------------


class EveryBar(Playbook):
    name = "everybar"
    asset_classes = ("forex",)
    warmup = 2
    stop_atr, target_atr, max_bars = 1.0, 1.0, 4

    def scan(self, bars, t):
        return Signal(1)


def wiggly_bars(n=200, seed=1) -> Bars:
    rng = random.Random(seed)
    price, o, h, l, c = 100.0, [], [], [], []
    for _ in range(n):
        new = price + rng.gauss(0, 1)
        o.append(price); c.append(new)
        h.append(max(price, new) + abs(rng.gauss(0, .3))); l.append(min(price, new) - abs(rng.gauss(0, .3)))
        price = new
    return Bars(o, h, l, c)


def test_there_is_never_more_than_one_trade_open_at_a_time():
    trades = simulate("EURUSD", wiggly_bars(), EveryBar()).trades
    assert len(trades) > 20
    for earlier, later in zip(trades, trades[1:]):
        assert later.entry_index > earlier.exit_index - 1          # the next entry is after the previous exit


def test_trades_in_the_last_forty_percent_are_marked_out_of_sample():
    bars = wiggly_bars(300)
    trades = simulate("EURUSD", bars, EveryBar()).trades
    split = int(300 * 0.6)
    assert all(t.half == int(t.entry_index >= split) for t in trades)
    assert {t.half for t in trades} == {0, 1}


def test_the_signal_count_and_free_bars_are_reported():
    r = simulate("EURUSD", wiggly_bars(), EveryBar())
    assert r.signals == len(r.trades) and 0 < r.free_bars <= 200


# ---- lookahead check -------------------------------------------------------------


class Honest(Playbook):
    name = "honest"
    asset_classes = ("forex",)
    warmup = 5

    def scan(self, bars, t):
        return Signal(1 if bars.closes[t] > bars.closes[t - 1] else -1)


class Cheat(Playbook):
    name = "cheat"
    asset_classes = ("forex",)
    warmup = 5

    def scan(self, bars, t):
        nxt = bars.closes[t + 1] if t + 1 < len(bars) else bars.closes[t]       # peeks one bar ahead
        return Signal(1 if nxt > bars.closes[t] else -1)


def test_an_honest_rule_has_no_lookahead_violations():
    assert lookahead_violations(Honest(), wiggly_bars(300), step=3) == []


def test_a_rule_that_peeks_at_the_next_bar_is_caught():
    assert len(lookahead_violations(Cheat(), wiggly_bars(300), step=3)) > 10


# ---- statistics and the gate -----------------------------------------------------


def trade(r, half=0):
    return Trade("EURUSD", 0, 1, 1, r, "x", half)


def test_stats_on_a_known_set_of_results():
    s = stats([trade(2.0), trade(-1.0), trade(2.0, 1), trade(-1.0, 1), trade(2.0, 1)])
    assert (s.n, s.wins, s.n_oos) == (5, 3, 3)
    assert s.avg_r == pytest.approx(0.8) and s.total_r == pytest.approx(4.0)
    assert s.profit_factor == pytest.approx(6.0 / 2.0)
    assert s.avg_r_oos == pytest.approx(1.0)


def test_profit_factor_is_undefined_without_a_loss():
    assert stats([trade(1.0), trade(2.0)]).profit_factor is None


def good() -> Stats:
    return Stats(n=500, wins=300, avg_r=0.2, t=5.0, total_r=100, profit_factor=1.5,
                 n_oos=200, avg_r_oos=0.15, t_oos=3.0)


def flat() -> Stats:
    return Stats(n=500, avg_r=-0.02)


def test_a_result_that_clears_every_condition_passes():
    verdict, failed = judge(good(), flat())
    assert verdict == "PASS" and failed == []


@pytest.mark.parametrize("change, fragment", [
    (dict(n=MIN_TRADES - 1), "trades (need"),
    (dict(avg_r=0.0), "not above zero"),
    (dict(t=MIN_T - 0.01), "is below"),
    (dict(n_oos=MIN_OOS_TRADES - 1), "out-of-sample trades"),
    (dict(avg_r_oos=-0.01), "out-of-sample average R"),
    (dict(t_oos=MIN_OOS_T - 0.01), "out-of-sample t"),
])
def test_each_condition_can_fail_the_verdict_on_its_own(change, fragment):
    s = good()
    for k, v in change.items():
        setattr(s, k, v)
    verdict, failed = judge(s, flat())
    assert verdict == "FAIL" and any(fragment in f for f in failed)


def test_beating_zero_is_not_enough_it_must_beat_the_placebo():
    placebo = Stats(n=500, avg_r=0.2 - (MIN_EDGE_OVER_PLACEBO - 0.01))      # the placebo earns nearly as much
    verdict, failed = judge(good(), placebo)
    assert verdict == "FAIL" and any("placebo" in f for f in failed)


def test_the_gate_reports_every_reason_not_just_the_first():
    _, failed = judge(Stats(n=10, avg_r=-0.1, t=-1.0, n_oos=3, avg_r_oos=-0.1, t_oos=-1.0), flat())
    assert len(failed) >= 6


# ---- the whole machine: does it find real edges and reject noise? -----------------


def candles_from_returns(returns, start=100.0, seed=0):
    rng = random.Random(seed)
    price, out = start, []
    for r in returns:
        new = price + r
        out.append(SimpleNamespace(open=price, close=new, time=None,
                                   high=max(price, new) + abs(rng.gauss(0, .3)),
                                   low=min(price, new) - abs(rng.gauss(0, .3))))
        price = new
    return out


def ar1(n, phi, seed):
    rng, r, out = random.Random(seed), 0.0, []
    for _ in range(n):
        r = phi * r + rng.gauss(0, 1)
        out.append(r)
    return out


class Momentum(Playbook):
    """Buy after an up bar, sell after a down bar, hold three bars. Wins only if returns trend."""
    name = "momentum_toy"
    asset_classes = ("forex",)
    warmup = 30
    stop_atr, target_atr, max_bars = 3.0, 3.0, 3

    def scan(self, bars, t):
        return Signal(1 if bars.closes[t] > bars.closes[t - 1] else -1)


def market(phi, seeds=(1, 2)):
    return {sym: candles_from_returns(ar1(6000, phi, s), seed=s) for sym, s in zip(("EURUSD", "GBPUSD"), seeds)}


def forex_row(report):
    (row,) = [r for r in report.rows if r.group == "forex"]
    return row


def test_it_finds_an_edge_that_is_really_there():
    row = forex_row(run_playbook(Momentum(), market(phi=0.5)))
    assert row.verdict == "PASS", row.failed
    assert row.real.avg_r > 0.1 and row.real.t > 10 and abs(row.placebo.avg_r) < 0.05


def test_it_rejects_a_random_walk():
    row = forex_row(run_playbook(Momentum(), market(phi=0.0)))
    assert row.verdict == "FAIL"


def test_it_sees_a_rule_that_is_wrong_way_round():
    row = forex_row(run_playbook(Momentum(), market(phi=-0.5)))
    assert row.verdict == "FAIL" and row.real.avg_r < 0


def test_costs_can_eat_a_real_edge():
    spreads = {"EURUSD": 1.5, "GBPUSD": 1.5}                    # about 0.45 R per trade against ~0.25 R of edge
    row = forex_row(run_playbook(Momentum(), market(phi=0.5), spreads))
    assert row.verdict == "FAIL" and any("average R" in f for f in row.failed)


def test_the_placebo_mirrors_the_geometry_and_earns_about_nothing_minus_costs():
    row = forex_row(run_playbook(Momentum(), market(phi=0.0), {"EURUSD": 0.5, "GBPUSD": 0.5}))
    assert row.placebo.n > 200 and row.placebo.avg_r < 0.0


def test_the_same_seed_gives_the_same_report():
    a = run_playbook(Momentum(), market(0.2), seed="x").to_json()
    b = run_playbook(Momentum(), market(0.2), seed="x").to_json()
    assert a == b


def test_symbols_outside_the_playbooks_asset_classes_are_ignored():
    candles = {**market(0.5), "Volatility 75 Index": candles_from_returns(ar1(2000, 0.5, 9))}
    report = run_playbook(Momentum(), candles)
    assert [r.group for r in report.rows] == ["forex"]


def test_the_report_reads_and_saves(tmp_path):
    report = run_playbook(Momentum(), market(0.5))
    text = report.text()
    assert "momentum_toy" in text and "PASSED: forex" in text and "placebo" in text
    path = report.save(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["rows"][0]["group"] == "forex"


# ---- registry --------------------------------------------------------------------


def test_registering_needs_a_name_and_rejects_duplicates():
    class Nameless(Playbook):
        pass

    with pytest.raises(ValueError):
        register(Nameless)

    class Dup(Playbook):
        name = "dup_test_playbook"

    register(Dup)
    try:
        with pytest.raises(ValueError):
            register(Dup)
    finally:
        REGISTRY.pop("dup_test_playbook", None)
