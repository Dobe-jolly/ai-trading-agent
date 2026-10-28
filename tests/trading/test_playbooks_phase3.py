from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from trading.playbooks import REGISTRY
from trading.playbooks.base import Bars
from trading.playbooks.delta import Delta
from trading.playbooks.epsilon import Epsilon
from trading.playbooks.evaluate import run_playbook
from trading.playbooks.simulate import lookahead_violations
from trading.playbooks.theta import Theta

from .test_playbooks_phase1 import bars_from_closes, random_candles
from .test_playbooks_phase2 import flat_then, h4_candles


def test_phase_three_is_registered_with_the_approved_geometry():
    d, e = REGISTRY["delta"], REGISTRY["epsilon"]
    assert (d.timeframe, d.stop_atr, d.target_atr, d.max_bars, d.asset_classes) == ("M5", 2.0, 2.0, 12, ("boom_crash",))
    assert (e.timeframe, e.stop_atr, e.target_atr, e.max_bars, e.asset_classes) == ("M15", 2.0, 2.0, 12, ("step",))


# ---- Delta -----------------------------------------------------------------------


def quiet_then(body, atr_at_signal=1.0, n=30):
    """Small bars with the ATR fixed at 1.0, then a final bar with the given body (open 100)."""
    opens = [100.0] * (n + 1)
    closes = [100.2] * n + [100.0 + body]
    highs = [max(o, c) + 0.3 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.3 for o, c in zip(opens, closes)]
    atr = [1.0] * n + [atr_at_signal]
    return Bars(opens, highs, lows, closes, [None] * (n + 1), atr)


def scan_delta(bars):
    return Delta().scan(bars, len(bars) - 1)


def test_delta_sells_after_an_up_spike():
    sig = scan_delta(quiet_then(+4.0))
    assert sig is not None and sig.direction == -1


def test_delta_buys_after_a_down_spike():
    sig = scan_delta(quiet_then(-4.0))
    assert sig is not None and sig.direction == 1


def test_delta_needs_a_body_of_three_atr_and_three_is_enough():
    assert scan_delta(quiet_then(+2.9)) is None
    assert scan_delta(quiet_then(-2.9)) is None
    assert scan_delta(quiet_then(+3.0)).direction == -1
    assert scan_delta(quiet_then(-3.0)).direction == 1


def test_delta_judges_the_spike_against_the_previous_bars_atr():
    # the spike inflated its own ATR to 2.0: judged against that, a 3.5 body would be too small
    assert scan_delta(quiet_then(+3.5, atr_at_signal=2.0)).direction == -1


def test_delta_measures_the_body_not_the_wick():
    bars = quiet_then(+0.5)
    bars.highs[-1] = 108.0                                    # a huge wick, small body
    assert scan_delta(bars) is None


def test_delta_says_nothing_without_an_atr():
    bars = quiet_then(+4.0)
    bars.atr[-2] = None
    assert scan_delta(bars) is None
    assert Delta().scan(bars, 0) is None


def spiky_candles(n, seed):
    """A drifting random walk with a random up spike about every 40 bars, like a Boom index."""
    rng = random.Random(seed)
    price, out = 1000.0, []
    for i in range(n):
        move = -0.05 + rng.gauss(0, 0.4)
        if rng.random() < 1 / 40:
            move += rng.uniform(4, 10)
        new = price + move
        out.append(SimpleNamespace(open=price, close=new, high=max(price, new) + abs(rng.gauss(0, 0.1)),
                                   low=min(price, new) - abs(rng.gauss(0, 0.1)), time=None))
        price = new
    return out


def test_delta_does_not_look_ahead():
    bars = Bars.from_candles(spiky_candles(1500, 21))
    assert sum(1 for t in range(60, len(bars)) if Delta().scan(bars, t)) > 10          # the check is not vacuous
    assert lookahead_violations(Delta(), bars, step=3) == []


# ---- Epsilon ---------------------------------------------------------------------


def test_epsilon_is_theta_on_step_index_and_nothing_else():
    assert issubclass(Epsilon, Theta)
    assert (Epsilon.LENGTH, Epsilon.Z) == (Theta.LENGTH, Theta.Z)
    for last, direction in (([98.0], 1), ([102.0], -1), ([99.4], None)):
        bars = bars_from_closes(flat_then(last))
        sig = Epsilon().scan(bars, len(bars) - 1)
        assert (sig.direction if sig else None) == direction


def test_epsilon_does_not_look_ahead():
    assert lookahead_violations(Epsilon(), Bars.from_candles(random_candles(600, 22, hourly=False)), step=7) == []


# ---- through the whole machine on noise ------------------------------------------


def spiky_with_times(n, seed):
    from datetime import datetime, timedelta, timezone
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    out = spiky_candles(n, seed)
    for i, c in enumerate(out):
        c.time = start + timedelta(minutes=5 * i)
    return out


@pytest.mark.parametrize("name", ["delta", "epsilon"])
def test_neither_passes_on_a_random_walk(name):
    make = spiky_with_times if name == "delta" else h4_candles
    candles = {f"S{k}": make(3000, 60 + k) for k in range(3)}
    report = run_playbook(REGISTRY[name](), candles, {k: 0.0001 for k in candles})
    assert report.passed() == []


# ---- jump stops ------------------------------------------------------------------


from trading.playbooks.base import Playbook, Signal
from trading.playbooks.simulate import simulate


class ShortAt60(Playbook):
    name = "short_at_60"
    warmup, stop_atr, target_atr, max_bars = 60, 2.0, 2.0, 12

    def scan(self, bars, t):
        return Signal(-1) if t == 60 else None


def after_entry(high, close, n=80):
    """Flat at 100 with ATR 1.0; the entry bar (61) opens at 100 and then does `high` / `close`."""
    opens, highs, lows, closes = [100.0] * n, [100.3] * n, [99.7] * n, [100.0] * n
    highs[61], closes[61] = high, close
    return Bars(opens, highs, lows, closes, [None] * n, [1.0] * n)


def test_a_jump_through_the_stop_fills_at_the_close_only_when_asked():
    bars = after_entry(high=110.0, close=109.0)                  # short from 100, stop at 102
    plain = simulate("X", bars, ShortAt60()).trades[0]
    jumped = simulate("X", bars, ShortAt60(), jump_stops=True).trades[0]
    assert plain.r == pytest.approx(-1.0) and plain.reason == "stop"
    assert jumped.r == pytest.approx(-4.5) and jumped.reason == "stop"      # (109 - 100) / 2 ATR of risk


def test_touching_the_stop_but_closing_inside_is_still_minus_one_r():
    bars = after_entry(high=102.5, close=100.5)
    assert simulate("X", bars, ShortAt60(), jump_stops=True).trades[0].r == pytest.approx(-1.0)


def test_jump_stops_never_help_a_trade():
    for high, close in ((110, 109), (102.5, 100.5), (103, 102.5)):
        bars = after_entry(high, close)
        assert simulate("X", bars, ShortAt60(), jump_stops=True).trades[0].r <= simulate("X", bars, ShortAt60()).trades[0].r
