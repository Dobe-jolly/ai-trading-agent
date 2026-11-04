from __future__ import annotations

from types import SimpleNamespace

from trading.decision import OPEN, TradeDecision
from trading.runner import TradingRunner

MARK = (None, 1.1000, 1.1002)        # (time, bid at decision, ask at decision); mid 1.1001


def runner(drift, bid, ask, pip=0.0001):
    r = object.__new__(TradingRunner)
    r.s = SimpleNamespace(max_entry_drift=drift)
    info = SimpleNamespace(bid=bid, ask=ask, pip_size=pip) if bid else None
    r._broker = SimpleNamespace(get_symbol_info=lambda s: info)
    return r


def decision(sl_pips=100):
    return TradeDecision(action=OPEN, symbol="EURUSD", direction="buy", sl_pips=sl_pips, tp_pips=200, risk_pct=1.0,
                         reason="t", confidence=0.8, setup="x")


def test_small_move_goes_through():
    assert runner(0.3, 1.1020, 1.1022)._stale_entry("EURUSD", decision(), MARK) == ""    # 20 pips of a 100-pip stop


def test_move_past_the_fraction_is_stale_in_either_direction():
    assert "stale" in runner(0.3, 1.1040, 1.1042)._stale_entry("EURUSD", decision(), MARK)   # 40 pips up
    assert "stale" in runner(0.3, 1.0960, 1.0962)._stale_entry("EURUSD", decision(), MARK)   # 40 pips down


def test_exactly_at_the_limit_goes_through():
    assert runner(0.3, 1.1031, 1.1031)._stale_entry("EURUSD", decision(), MARK) == ""        # 30 pips


def test_limit_scales_with_the_stop():
    assert runner(0.3, 1.1040, 1.1042)._stale_entry("EURUSD", decision(sl_pips=200), MARK) == ""


def test_off_when_zero_or_data_missing():
    assert runner(0.0, 1.5, 1.5)._stale_entry("EURUSD", decision(), MARK) == ""
    assert runner(0.3, 1.5, 1.5)._stale_entry("EURUSD", decision(), None) == ""
    assert runner(0.3, 0, 0)._stale_entry("EURUSD", decision(), MARK) == ""
    assert runner(0.3, 1.5, 1.5)._stale_entry("EURUSD", decision(), (None, 0.0, 0.0)) == ""
