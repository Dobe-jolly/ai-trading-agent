from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading.breakers import Lockout, halt_reason, in_blackout, parse_blackouts
from trading.broker.metatrader import AccountState, SymbolInfo
from trading.decision import OPEN, TradeDecision
from trading.guardrails import apply_guardrails


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 27, hour, minute, tzinfo=timezone.utc)


def test_parse_blackouts_reads_several_windows():
    assert parse_blackouts("21:00-22:00, 23:30-00:15") == [(1260, 1320), (1410, 15)]
    assert parse_blackouts("") == []


@pytest.mark.parametrize("bad", ["21:00", "25:00-26:00", "ab-cd", "21:00-22:00-23:00"])
def test_parse_blackouts_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        parse_blackouts(bad)


def test_in_blackout_is_start_inclusive_end_exclusive():
    w = parse_blackouts("21:00-22:00")
    assert in_blackout(at(21, 0), w)
    assert in_blackout(at(21, 59), w)
    assert not in_blackout(at(22, 0), w)
    assert not in_blackout(at(20, 59), w)


def test_in_blackout_wraps_midnight():
    w = parse_blackouts("23:30-00:15")
    assert in_blackout(at(23, 45), w)
    assert in_blackout(at(0, 10), w)
    assert not in_blackout(at(0, 15), w)
    assert not in_blackout(at(12, 0), w)


def test_blackout_skips_synthetics():
    w = parse_blackouts("21:00-22:00")
    assert halt_reason("EURUSD", at(21, 30), w, None, 0)
    assert halt_reason("Volatility 75 Index", at(21, 30), w, None, 0) is None


def test_latency_halts_every_symbol_when_too_slow():
    assert halt_reason("EURUSD", at(12), [], 1500.0, 1000.0)
    assert halt_reason("Volatility 75 Index", at(12), [], 1500.0, 1000.0)
    assert halt_reason("EURUSD", at(12), [], 400.0, 1000.0) is None


def test_unknown_latency_or_disabled_limit_does_not_halt():
    assert halt_reason("EURUSD", at(12), [], None, 1000.0) is None
    assert halt_reason("EURUSD", at(12), [], 9999.0, 0) is None


def test_lockout_holds_until_expiry_then_clears(tmp_path):
    lock = Lockout(tmp_path / "state" / "LOCKOUT")
    assert lock.active(at(12)) is None
    until = lock.trip(at(12), 24, "daily loss")
    assert until == at(12) + timedelta(hours=24)

    held = lock.active(at(12) + timedelta(hours=23))
    assert held == (until, "daily loss")

    assert lock.active(until) is None
    assert not (tmp_path / "state" / "LOCKOUT").exists()


def test_lockout_survives_a_new_instance(tmp_path):
    Lockout(tmp_path / "L").trip(at(12), 24, "x")
    assert Lockout(tmp_path / "L").active(at(13)) is not None


def test_unreadable_lockout_file_fails_closed(tmp_path):
    path = tmp_path / "L"
    path.write_text("not json", encoding="utf-8")
    assert Lockout(path).active(at(12)) is not None


def test_halted_guardrail_blocks_opens_but_not_closes():
    acct = AccountState(100.0, 100.0, 0.0, 100.0, 0.0, 0.0)
    info = SymbolInfo("EURUSD", 5, 0.00001, 0.00001, 1.0, 100000, 0.01, 100.0, 0.01, 10, 1.0850, 1.0851)
    dec = TradeDecision(OPEN, "EURUSD", "buy", 5.0, 50, 100, "r", 0.9, "trend_continuation")
    kw = dict(max_risk_pct=20.0, max_positions=30, daily_loss_limit_pct=60.0,
              spread_max_pips=5.0, confidence_threshold=0.72)

    assert not apply_guardrails(dec, acct, [], info, **kw).blocked
    g = apply_guardrails(dec, acct, [], info, halted="latency", **kw)
    assert g.blocked and "latency" in g.notes[0]
