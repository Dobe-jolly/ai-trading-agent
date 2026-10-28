from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading.broker.metatrader import AccountState, Position, SymbolInfo
from trading.decision import OPEN, TradeDecision
from trading.guardrails import apply_guardrails, open_risk_pct

BALANCE = 100.0


def account() -> AccountState:
    return AccountState(BALANCE, BALANCE, 0.0, BALANCE, 0.0, 0.0)


def eurusd() -> SymbolInfo:
    # 1 pip = 0.0001 = 10 ticks of 0.00001; 1 tick is worth $1 per lot, so $10 per pip per lot.
    return SymbolInfo("EURUSD", 5, 0.00001, 0.00001, 1.0, 100000, 0.01, 100.0, 0.01, 10, 1.0850, 1.0851)


def position(sl: float = 1.0800, lots: float = 0.01, symbol: str = "EURUSD", ticket: int = 1) -> Position:
    return Position(ticket, symbol, "buy", lots, 1.0850, sl, 1.0950, 0.0, datetime.now(timezone.utc))


def decision(risk_pct: float = 10.0) -> TradeDecision:
    return TradeDecision(OPEN, "EURUSD", "buy", risk_pct, 50, 100, "r", 0.9, "trend_continuation")


def guard(dec: TradeDecision, open_risk: float, cap: float = 40.0):
    return apply_guardrails(
        dec, account(), [], eurusd(),
        max_risk_pct=20.0, max_positions=30, daily_loss_limit_pct=60.0,
        spread_max_pips=5.0, confidence_threshold=0.72,
        open_risk_pct=open_risk, max_total_risk_pct=cap,
    )


def test_open_risk_measures_to_the_stop():
    # 0.01 lot, stop 50 pips away: 0.01 * 50 * $10 = $5 on a $100 balance
    assert open_risk_pct([position()], BALANCE, lambda s: eurusd(), 20.0) == pytest.approx(5.0)


def test_open_risk_counts_positions_without_a_stop_at_the_fallback():
    assert open_risk_pct([position(sl=0.0)], BALANCE, lambda s: eurusd(), 20.0) == 20.0
    assert open_risk_pct([position()], BALANCE, lambda s: None, 20.0) == 20.0


def test_open_risk_sums_positions_and_handles_zero_balance():
    assert open_risk_pct([position(), position()], BALANCE, lambda s: eurusd(), 20.0) == pytest.approx(10.0)
    assert open_risk_pct([position()], 0.0, lambda s: eurusd(), 20.0) == 0.0


def test_trade_within_the_cap_passes_unchanged():
    g = guard(decision(10.0), open_risk=20.0)
    assert not g.blocked and g.final.risk_pct == 10.0 and not g.notes


def test_trade_is_trimmed_to_the_room_left():
    g = guard(decision(10.0), open_risk=35.0)
    assert not g.blocked and g.final.risk_pct == 5.0
    assert any("trimmed" in n for n in g.notes)


def test_trade_is_blocked_when_the_cap_is_reached():
    g = guard(decision(10.0), open_risk=40.0)
    assert g.blocked and g.final.action != OPEN


def test_trade_is_blocked_when_almost_no_room_remains():
    assert guard(decision(10.0), open_risk=39.95).blocked


def test_fourth_open_in_one_scan_is_blocked():
    def scan(n: int):
        return apply_guardrails(
            decision(5.0), account(), [], eurusd(),
            max_risk_pct=20.0, max_positions=30, daily_loss_limit_pct=60.0,
            spread_max_pips=5.0, confidence_threshold=0.72, opens_this_scan=n,
        )
    assert not scan(2).blocked
    assert scan(3).blocked


def test_second_open_on_the_same_symbol_is_blocked():
    def with_positions(positions, symbol="EURUSD"):
        dec = TradeDecision(OPEN, symbol, "buy", 5.0, 50, 100, "r", 0.9, "trend_continuation")
        return apply_guardrails(
            dec, account(), positions, eurusd(),
            max_risk_pct=20.0, max_positions=30, daily_loss_limit_pct=60.0,
            spread_max_pips=5.0, confidence_threshold=0.72,
        )

    assert with_positions([position(symbol="EURUSD")]).blocked
    assert not with_positions([position(symbol="GBPUSD")]).blocked        # a different symbol is fine


def test_close_and_modify_on_a_held_symbol_still_work():
    held = position(symbol="EURUSD", ticket=7)
    kw = dict(max_risk_pct=20.0, max_positions=30, daily_loss_limit_pct=60.0,
              spread_max_pips=5.0, confidence_threshold=0.72)
    for action in ("close", "modify"):
        dec = TradeDecision(action, "EURUSD", "", 5.0, 40, 80, "r", 0.9, "x", position_ticket=7)
        assert not apply_guardrails(dec, account(), [held], eurusd(), **kw).blocked


def test_cap_of_zero_disables_the_check():
    g = guard(decision(10.0), open_risk=500.0, cap=0.0)
    assert not g.blocked and g.final.risk_pct == 10.0
