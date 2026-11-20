"""Gate realism: jump-stops and Boom/Crash drift placebo are ON by default."""
from __future__ import annotations

import inspect
import random
from types import SimpleNamespace

from trading.playbooks.evaluate import Stats, judge, run_playbook
from trading.playbooks.simulate import DriftPlacebo, drift_direction, simulate
from trading.playbooks.base import Bars, Playbook, Signal
from trading import __main__ as trading_main


def test_simulate_and_run_playbook_default_to_jump_stops_and_drift_on():
    assert inspect.signature(simulate).parameters["jump_stops"].default is True
    sig = inspect.signature(run_playbook)
    assert sig.parameters["jump_stops"].default is True
    assert sig.parameters["drift_placebo"].default is True


def test_cli_exposes_diagnostic_opt_outs():
    src = inspect.getsource(trading_main.main)
    assert "--no-jump-stops" in src
    assert "--no-drift-placebo" in src
    # Old opt-in flag must be gone so the default path is the realistic one.
    assert '--jump-stops", action="store_true"' not in src


def test_drift_direction_from_symbol_name():
    assert drift_direction("Boom 1000 Index") == -1
    assert drift_direction("Crash 500 Index") == 1
    assert drift_direction("EURUSD") is None
    assert drift_direction("Volatility 75 Index") is None


def test_judge_fails_when_real_does_not_beat_drift_placebo():
    real = Stats(n=500, wins=300, avg_r=0.20, t=5.0, total_r=100, profit_factor=1.5,
                 n_oos=200, avg_r_oos=0.18, t_oos=3.0)
    placebo = Stats(n=500, avg_r=0.05)
    drift = Stats(n=500, avg_r=0.19, n_oos=200, avg_r_oos=0.17)
    verdict, failed = judge(real, placebo, drift)
    assert verdict == "FAIL"
    assert any("drift placebo" in f for f in failed)


def test_judge_fails_oos_drift_even_if_full_sample_clears():
    real = Stats(n=500, wins=300, avg_r=0.25, t=5.0, total_r=125, profit_factor=1.5,
                 n_oos=200, avg_r_oos=0.12, t_oos=3.0)
    placebo = Stats(n=500, avg_r=0.05)
    drift = Stats(n=500, avg_r=0.10, n_oos=200, avg_r_oos=0.11)  # OOS margin 0.01 < 0.05
    verdict, failed = judge(real, placebo, drift)
    assert verdict == "FAIL"
    assert any("out-of-sample beats the drift placebo" in f for f in failed)


def test_judge_passes_when_real_beats_both_placebos():
    real = Stats(n=500, wins=300, avg_r=0.25, t=5.0, total_r=125, profit_factor=1.5,
                 n_oos=200, avg_r_oos=0.22, t_oos=3.0)
    placebo = Stats(n=500, avg_r=0.05)
    drift = Stats(n=500, avg_r=0.10, n_oos=200, avg_r_oos=0.08)
    verdict, failed = judge(real, placebo, drift)
    assert verdict == "PASS" and failed == []


class AlwaysSell(Playbook):
    """Trades with Boom drift every free bar — must face the drift placebo under defaults."""
    name = "always_sell"
    asset_classes = ("boom_crash",)
    warmup = 20
    stop_atr, target_atr, max_bars = 2.0, 2.0, 5

    def scan(self, bars, t):
        return Signal(-1)


def _boom_candles(n=800, seed=3):
    """Down-drifting path with occasional up spikes (Boom-like)."""
    rng = random.Random(seed)
    price, out = 1000.0, []
    for i in range(n):
        if i > 0 and i % 40 == 0:
            new = price + abs(rng.gauss(0, 8))
        else:
            new = price - abs(rng.gauss(0.15, 0.4))
        hi = max(price, new) + abs(rng.gauss(0, 0.2))
        lo = min(price, new) - abs(rng.gauss(0, 0.2))
        out.append(SimpleNamespace(open=price, high=hi, low=lo, close=new, time=None))
        price = new
    return out


def test_run_playbook_defaults_include_jump_stops_and_drift_on_boom():
    candles = {"Boom 1000 Index": _boom_candles()}
    report = run_playbook(AlwaysSell(), candles, {"Boom 1000 Index": 0.0})
    assert report.jump_stops is True and report.drift_placebo is True
    row = report.rows[0]
    assert row.group == "boom_crash"
    assert row.drift is not None
    # Blindly selling with the drift is not an edge vs the drift placebo.
    assert abs(row.real.avg_r - row.drift.avg_r) < 0.05 or row.verdict == "FAIL"
    assert row.verdict == "FAIL"


def test_diagnostic_flags_can_turn_the_extras_off():
    candles = {"Boom 1000 Index": _boom_candles()}
    report = run_playbook(
        AlwaysSell(), candles, {"Boom 1000 Index": 0.0},
        jump_stops=False, drift_placebo=False,
    )
    assert report.jump_stops is False and report.drift_placebo is False
    assert report.rows[0].drift is None


def test_forex_cells_have_no_drift_placebo_row():
    class Buy(Playbook):
        name = "buy"
        asset_classes = ("forex",)
        warmup = 20
        stop_atr, target_atr, max_bars = 1.5, 1.5, 5

        def scan(self, bars, t):
            return Signal(1)

    rng = random.Random(0)
    price, out = 1.1, []
    for _ in range(600):
        new = price + rng.gauss(0, 0.001)
        out.append(SimpleNamespace(
            open=price, high=max(price, new) + 0.0002, low=min(price, new) - 0.0002, close=new, time=None,
        ))
        price = new
    report = run_playbook(Buy(), {"EURUSD": out}, {"EURUSD": 0.0})
    assert report.rows[0].drift is None


def test_drift_placebo_always_uses_the_named_direction():
    pb = AlwaysSell()
    bars = Bars.from_candles(_boom_candles(120))
    fake = DriftPlacebo(pb, rate=1.0, seed="s", direction=-1)
    for t in range(pb.warmup, 40):
        sig = fake.scan(bars, t)
        assert sig is not None and sig.direction == -1
