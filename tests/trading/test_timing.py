from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading import timing
from trading.timing import (Baseline, Event, SymbolData, collect, evaluate, make_row, slot_of)


def at(hour):
    return datetime(2026, 3, 3, hour, 30, tzinfo=timezone.utc)


# ---- time slots ------------------------------------------------------------------


@pytest.mark.parametrize("hour,slot", [(0, "asian"), (6, "asian"), (7, "london"), (11, "london"),
                                        (12, "new_york"), (20, "new_york"), (21, "late"), (23, "late")])
def test_slot_boundaries(hour, slot):
    assert slot_of(at(hour)) == slot


def test_a_bar_without_a_time_has_no_slot():
    assert slot_of(None) is None


# ---- evaluation on hand-built events ---------------------------------------------


def flat_baseline(symbols, mean=0.0, p_up=0.5):
    out = {}
    for sym in symbols:
        b = Baseline()
        for half in (0, 1):
            for slot, _, _ in timing.SLOTS:
                for h in timing.HORIZONS:
                    for k in range(100):                                  # 50 up moves and 50 down moves
                        b.add(half, slot, h, mean + (1.0 if k < 100 * p_up else -1.0) * 0.0)
                    b.total[(half, slot, h)] = mean * 100
                    b.ups[(half, slot, h)] = int(100 * p_up)
        out[sym] = b
    return out


def events(n, slot="london", move=0.5, direction=1, symbol="EURUSD", cost=0.05, jitter=0.4, seed=1):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        m = move + rng.uniform(-jitter, jitter)
        out.append(Event(symbol, i * 20, int(i >= n * 0.6), slot, direction, {4: m * direction, 12: m * direction}, cost))
    return out


def row(evs, baselines=None, kind="real"):
    return make_row(kind, "forex", "london", 4, evs, baselines or flat_baseline(["EURUSD"]))


def test_a_planted_reversal_in_one_slot_is_a_candidate_and_only_there():
    good = row(events(300, slot="london", move=0.5))
    assert good.verdict == "CANDIDATE" and good.n == 300
    assert row(events(300, move=0.0)).verdict == "none"


def test_a_slots_own_drift_is_not_mistaken_for_skill():
    # events earn +0.5 ATR, but that slot's average move is also +0.5: the excess is zero
    biased = flat_baseline(["EURUSD"], mean=0.5)
    r = row(events(300, move=0.5), biased)
    assert abs(r.mean) < 0.05 and r.verdict == "none"


def test_the_excess_is_signed_by_the_events_direction():
    r = row(events(300, move=0.5, direction=-1))                  # a swept high implies down; price went down
    assert r.mean > 0.4 and r.verdict == "CANDIDATE"


@pytest.mark.parametrize("change,expect", [
    (dict(n=99), "none"),                                         # too few events
    (dict(move=0.02), "none"),                                    # not significant
])
def test_each_gate_condition_can_reject(change, expect):
    base = dict(n=300, move=0.5)
    base.update(change)
    assert row(events(**base)).verdict == expect


def test_the_effect_must_beat_one_spread():
    assert row(events(300, move=0.5, cost=0.2)).verdict == "CANDIDATE"
    assert row(events(300, move=0.5, cost=0.6)).verdict == "none"


def test_a_real_effect_that_vanishes_out_of_sample_is_rejected():
    evs = events(300, move=0.5)
    for e in evs:
        if e.half == 1:
            e.fwd = {h: 0.0 for h in e.fwd}
    r = row(evs)
    assert r.n_oos >= 40 and r.mean_oos < r.mean and r.verdict == "none"


def test_too_few_out_of_sample_events_rejects():
    evs = events(300, move=0.5)
    for e in evs:
        e.half = 0 if e.index > 100 else 1                        # only a handful left in the second half
    assert row(evs).verdict == "none"


def test_a_strongly_negative_slot_is_only_a_hint():
    r = row(events(300, move=-0.5))
    assert r.verdict == "hint: continues"


def test_a_control_or_placebo_cell_that_clears_the_gate_is_flagged_as_a_broken_study():
    assert "CLEARS" in row(events(300, move=0.5), kind="control").verdict
    assert "CLEARS" in row(events(300, move=0.5), kind="placebo").verdict


# ---- evaluate() routes events to the right cell -----------------------------------


def test_events_land_only_in_their_own_slot_and_class():
    sym = "EURUSD"
    data = [SymbolData(sym, events(300, slot="london", move=0.5), [], flat_baseline([sym])[sym]),
            SymbolData("XAUUSD", events(300, slot="asian", move=0.5, symbol="XAUUSD"), [], flat_baseline(["XAUUSD"])["XAUUSD"])]
    rows = {(r.group, r.slot, r.horizon): r for r in evaluate(data) if r.kind == "real"}
    assert rows[("forex", "london", 4)].verdict == "CANDIDATE"
    assert rows[("forex", "asian", 4)].n == 0
    assert rows[("metals", "asian", 4)].verdict == "CANDIDATE"
    assert rows[("metals", "london", 4)].n == 0
    assert not any(k[0] == "vol_index" for k in rows)             # no control symbols supplied


# ---- collecting from candles ------------------------------------------------------


def hourly_candles(n, seed, start=datetime(2025, 1, 6, tzinfo=timezone.utc)):
    rng = random.Random(seed)
    price, out = 1.1, []
    for i in range(n):
        new = price + rng.gauss(0, 0.001)
        out.append(SimpleNamespace(open=price, close=new, high=max(price, new) + abs(rng.gauss(0, 0.0004)),
                                   low=min(price, new) - abs(rng.gauss(0, 0.0004)), time=start + timedelta(hours=i)))
        price = new
    return out


def test_collect_finds_sweeps_and_records_them_in_the_right_slot_with_no_lookahead():
    candles = hourly_candles(1500, 7)
    full = collect("EURUSD", candles, spread=0.0001)
    assert len(full.events) > 5, "the check below would be vacuous with no events"
    assert all(e.slot == slot_of(candles[e.index].time) for e in full.events)
    cut_at = 1000
    cut = collect("EURUSD", candles[:cut_at + 20], spread=0.0001)
    early = [(e.index, e.direction, e.slot) for e in full.events if e.index <= cut_at]
    assert early == [(e.index, e.direction, e.slot) for e in cut.events if e.index <= cut_at]


def test_events_are_declustered():
    data = collect("EURUSD", hourly_candles(1500, 8))
    idx = [e.index for e in data.events]
    assert all(b - a >= timing.DECLUSTER for a, b in zip(idx, idx[1:]))


def test_the_baseline_covers_every_slot_and_the_outcome_starts_at_the_next_open():
    candles = hourly_candles(800, 9)
    data = collect("EURUSD", candles)
    assert all(data.baseline.count[(h, s, 4)] > 0 for h in (0, 1) for s, _, _ in timing.SLOTS)
    e = data.events[0] if data.events else None
    if e:
        assert e.fwd[4] != 0.0


def test_the_study_runs_end_to_end_and_the_placebo_and_noise_do_not_pass():
    candles = {sym: hourly_candles(2500, 20 + k) for k, sym in enumerate(("EURUSD", "GBPUSD", "XAUUSD", "Volatility 10 Index"))}
    study = timing.run(candles, {k: 0.0001 for k in candles})
    assert study.tests == 3 * len(timing.SLOTS) * len(timing.HORIZONS) and "STUDY BROKEN" not in study.text()
    assert not [r for r in study.rows if r.verdict == "CANDIDATE"]


# ---- detection rules, pinned with a scripted engine --------------------------------


def sweep(direction="bullish", source="equal", bars_ago=0):
    return {"direction": direction, "source": source, "bars_ago": bars_ago}


def test_only_a_fresh_equal_level_sweep_on_bar_t_is_an_event(monkeypatch):
    candles = hourly_candles(700, 3)
    t0 = 400
    sweeps = {t0: sweep("bullish"), t0 + 30: sweep("bearish"),
              t0 + 60: sweep("bullish", bars_ago=1),          # an old sweep: not today's candle
              t0 + 90: sweep("bullish", source="single"),     # a lone swing, not a liquidity pool
              t0 + 120: sweep("bearish", bars_ago=2)}
    monkeypatch.setattr(timing, "analyze", lambda h, l, c, *a, **k: {"atr": 1.0, "recent_sweep": sweeps.get(_current_t(h, candles))})
    found = collect("EURUSD", candles)
    assert [(e.index, e.direction) for e in found.events] == [(t0, 1), (t0 + 30, -1)]


def _current_t(highs, candles):
    """Index of the last bar in the window handed to analyze(): match by its high."""
    last_high = highs[-1]
    for i in range(len(candles) - 1, -1, -1):
        if candles[i].high == last_high:
            return i
    raise AssertionError


def test_the_outcome_runs_from_the_next_open_to_the_close_h_bars_later(monkeypatch):
    candles = hourly_candles(700, 4)
    t0 = 400
    monkeypatch.setattr(timing, "analyze", lambda h, l, c, *a, **k: {"atr": 1.0, "recent_sweep": sweep() if _current_t(h, candles) == t0 else None})
    e = collect("EURUSD", candles, spread=0.0002).events[0]
    from trading.playbooks.base import atr_series
    atr = atr_series([c.high for c in candles], [c.low for c in candles], [c.close for c in candles])[t0]
    assert e.fwd[4] == pytest.approx((candles[t0 + 4].close - candles[t0 + 1].open) / atr)
    assert e.fwd[12] == pytest.approx((candles[t0 + 12].close - candles[t0 + 1].open) / atr)
    assert e.cost_atr == pytest.approx(0.0002 / atr)


def test_a_middling_t_statistic_is_not_enough():
    r = row(events(300, move=0.03, cost=0.0))
    assert 1.0 < r.t < 3.0 and r.verdict == "none"


def test_events_closer_than_twelve_bars_are_dropped(monkeypatch):
    candles = hourly_candles(700, 5)
    hits = {400: sweep(), 405: sweep("bearish"), 411: sweep(), 412: sweep("bearish"), 440: sweep()}
    monkeypatch.setattr(timing, "analyze", lambda h, l, c, *a, **k: {"atr": 1.0, "recent_sweep": hits.get(_current_t(h, candles))})
    assert [e.index for e in collect("EURUSD", candles).events] == [400, 412, 440]
