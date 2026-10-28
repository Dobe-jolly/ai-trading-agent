from __future__ import annotations

import json
import random
from types import SimpleNamespace

import pytest

from trading import study
from trading.study import Obs, asset_class, collect, evaluate, run

H = (4, 12, 24)


def candle(o, h, l, c):
    return SimpleNamespace(open=o, high=h, low=l, close=c)


def random_walk(n: int, seed: int) -> list:
    rng = random.Random(seed)
    price, out = 100.0, []
    for _ in range(n):
        new = price + rng.gauss(0, 1)
        out.append(candle(price, max(price, new) + abs(rng.gauss(0, 0.3)),
                          min(price, new) - abs(rng.gauss(0, 0.3)), new))
        price = new
    return out


def test_asset_classes():
    assert [asset_class(s) for s in
            ("EURUSD", "XAUUSD", "XAGUSD", "Volatility 75 (1s) Index", "Step Index", "Boom 500 Index", "Crash 300 Index")] \
        == ["forex", "metals", "metals", "vol_index", "step", "boom_crash", "boom_crash"]


# ---- collect: no lookahead, correct outcome --------------------------------------


@pytest.fixture
def stub_analyze(monkeypatch):
    seen = []

    def fake(highs, lows, closes, *a, **k):
        seen.append((len(closes), closes[-1], max(highs)))
        return {"atr": 2.0, "regime": "RANGE", "recent_sweep": None, "fvg_zone_active": False,
                "fvg_direction": None, "last_break": None}

    monkeypatch.setattr(study, "analyze", fake)
    return seen


def test_analysis_window_ends_at_t_and_never_sees_the_outcome(stub_analyze):
    candles = [candle(i, i + 0.5, i - 0.5, i + 0.1) for i in range(120)]
    obs = collect("EURUSD", candles, window=30, step=5, horizons=H)
    assert len(obs) == len(stub_analyze) > 5
    for o, (length, last_close, highest) in zip(obs, stub_analyze):
        assert length == 30
        assert last_close == candles[o.index].close          # the newest bar shown is bar t
        assert highest <= candles[o.index].high              # nothing later was visible


def test_outcome_is_next_open_to_close_n_bars_on_in_atrs(stub_analyze):
    candles = [candle(i, i + 0.5, i - 0.5, i + 0.1) for i in range(120)]
    for o in collect("EURUSD", candles, window=30, step=5, horizons=H):
        for h in H:
            expected = (candles[o.index + h].close - candles[o.index + 1].open) / 2.0
            assert o.fwd[h] == pytest.approx(expected)


def test_walk_stops_early_enough_to_measure_the_longest_horizon(stub_analyze):
    candles = [candle(i, i + 0.5, i - 0.5, i + 0.1) for i in range(120)]
    obs = collect("EURUSD", candles, window=30, step=1, horizons=H)
    assert max(o.index for o in obs) == len(candles) - 1 - max(H)


def test_history_is_split_sixty_forty(stub_analyze):
    candles = [candle(i, i + 0.5, i - 0.5, i + 0.1) for i in range(200)]
    obs = collect("EURUSD", candles, window=30, step=1, horizons=H)
    assert {o.half for o in obs if o.index < 120} == {0} and {o.half for o in obs if o.index >= 120} == {1}


# ---- evaluate: planted effects ---------------------------------------------------


def synthetic(effect: float, *, oos_effect=None, direction=1, name="sweep:bullish", n=500, seed=3, drift=0.0):
    """Every 5th observation carries the signal, and its outcome is shifted by `effect`."""
    rng = random.Random(seed)
    oos_effect = effect if oos_effect is None else oos_effect
    out = []
    for i in range(n):
        half = int(i >= n * 0.6)
        on = i % 5 == 0
        shift = (oos_effect if half else effect) * direction if on else 0.0
        out.append(Obs("EURUSD", i * 100, half, {name: direction} if on else {},
                       {h: rng.gauss(0, 1) + drift + shift for h in H}))
    return out


def row(rows, name="sweep:bullish", h=12):
    return next(r for r in rows if r.signal == name and r.horizon == h)


def test_a_real_effect_is_a_candidate():
    r = row(evaluate(synthetic(0.8), H))
    assert r.mean == pytest.approx(0.8, abs=0.3) and r.t > 3
    assert r.n == 100 and r.n_oos == 40 and r.verdict == "CANDIDATE"


def test_no_effect_is_not_a_candidate():
    assert all(r.verdict != "CANDIDATE" for r in evaluate(synthetic(0.0), H))


def test_an_effect_that_vanishes_out_of_sample_is_not_a_candidate():
    r = row(evaluate(synthetic(1.0, oos_effect=0.0), H))
    assert r.t > 3 and r.verdict == "none"


def test_an_effect_that_flips_sign_out_of_sample_is_not_a_candidate():
    assert row(evaluate(synthetic(1.0, oos_effect=-1.0), H)).verdict == "none"


def test_a_signal_that_works_in_reverse_shows_negative_excess():
    r = row(evaluate(synthetic(-0.8), H))
    assert r.mean < 0 and r.t < -3 and r.verdict == "CANDIDATE"      # negative: fading it works


def test_a_bearish_signal_is_scored_in_its_own_direction():
    r = row(evaluate(synthetic(0.8, direction=-1, name="sweep:bearish"), H), "sweep:bearish")
    assert r.mean == pytest.approx(0.8, abs=0.3) and r.t > 3


def test_market_drift_is_not_mistaken_for_skill():
    obs = synthetic(0.0, drift=0.6)
    r = row(evaluate(obs, H))
    assert abs(r.mean) < 0.3 and r.verdict == "none"
    raw = sum(o.fwd[12] for o in obs if o.signals) / sum(1 for o in obs if o.signals)
    assert raw > 0.4        # the raw move was large; only the baseline removed it


def test_a_signal_with_no_direction_is_scored_on_absolute_move():
    rng = random.Random(5)
    obs = [Obs("EURUSD", i * 100, int(i >= 300), {"regime:range": 0} if i % 5 == 0 else {},
               {h: rng.choice((-1, 1)) * (2.0 if i % 5 == 0 else 1.0) for h in H}) for i in range(500)]
    r = row(evaluate(obs, H), "regime:range")
    # events move 2.0, everything else 1.0; the baseline is the all-bars average, 1.2
    assert r.mean == pytest.approx(0.8, abs=0.05) and r.hit is None


def test_clustered_firings_count_once():
    obs = [Obs("EURUSD", i * 2, 0, {"fvg:bullish": 1}, {h: 0.1 for h in H}) for i in range(50)]  # bars 0..98
    r = row(evaluate(obs, H), "fvg:bullish")
    assert r.n == 5          # bars 0, 24, 48, 72, 96


def test_groups_are_separate():
    a = [Obs("EURUSD", i * 100, 0, {"fvg:bullish": 1}, {h: 0.1 for h in H}) for i in range(30)]
    b = [Obs("Step Index", i * 100, 0, {"fvg:bullish": 1}, {h: 0.1 for h in H}) for i in range(30)]
    groups = {r.group for r in evaluate(a + b, H)}
    assert groups == {"forex", "step"}


# ---- end to end on a random walk -------------------------------------------------


def test_a_random_walk_has_no_candidates_and_the_placebo_looks_like_noise():
    candles = {"EURUSD": random_walk(1500, seed=11), "Step Index": random_walk(1500, seed=12)}
    result = run(candles, "H1", H, window=200, step=3)
    real = [r for r in result.rows if r.verdict != "placebo"]
    placebo = [r for r in result.rows if r.verdict == "placebo"]

    assert real and placebo
    assert not [r for r in real if r.verdict == "CANDIDATE"]
    assert all(abs(r.t) < 3 for r in placebo)
    assert "CANDIDATES: 0" in result.text()


def test_results_save_as_readable_json(tmp_path):
    result = run({"EURUSD": random_walk(700, seed=2)}, "H1", H, window=200, step=5)
    path = result.save(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["timeframe"] == "H1" and data["tests"] == result.tests and data["rows"]
