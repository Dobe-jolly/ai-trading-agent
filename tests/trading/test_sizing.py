from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trading.broker.metatrader import AccountState, MT5Broker, Position, SymbolInfo
from trading.decision import OPEN, TradeDecision
from trading.guardrails import apply_guardrails, open_risk_pct

BALANCE = 10_000.0


def fake_mt5(spec: dict, price: float, dollars_per_unit_per_lot: float | None):
    """A terminal whose symbol reports `spec` but whose profit calculator uses the real rate.

    `dollars_per_unit_per_lot` is what one lot truly earns per 1.0 of price movement; None makes
    the calculator fail the way it does for a symbol it can't price.
    """
    def order_calc_profit(order_type, symbol, volume, price_open, price_close):
        if dollars_per_unit_per_lot is None:
            return None
        return (price_close - price_open) * volume * dollars_per_unit_per_lot

    return SimpleNamespace(
        ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1,
        terminal_info=lambda: SimpleNamespace(connected=True),
        symbol_info_tick=lambda s: SimpleNamespace(bid=price, ask=price),
        symbol_info=lambda s: SimpleNamespace(**spec),
        order_calc_profit=order_calc_profit,
    )


def broker(spec, price, truth) -> MT5Broker:
    b = MT5Broker()
    b._mt5, b._connected = fake_mt5(spec, price, truth), True
    return b


# Volatility 75 as the terminal reports it: the tick value is a hundredth of what is really charged.
V75 = dict(digits=2, point=0.01, trade_tick_size=0.01, trade_tick_value=0.0001, trade_contract_size=1.0,
           volume_min=0.001, volume_max=100.0, volume_step=0.001, spread=0)
# EURUSD: the reported values are right.
EURUSD = dict(digits=5, point=0.00001, trade_tick_size=0.00001, trade_tick_value=1.0, trade_contract_size=100000,
              volume_min=0.01, volume_max=100.0, volume_step=0.01, spread=10)


def true_loss(info: SymbolInfo, lots: float, sl_pips: int) -> float:
    return lots * sl_pips * info.measured_pip_value


# ---- measured pip value ----------------------------------------------------------


def test_the_pip_value_is_measured_from_the_profit_calculator_not_the_reported_ticks():
    info = broker(V75, 33_000.0, truth=1.0).get_symbol_info("Volatility 75 Index")
    assert info.pip_value_per_lot == pytest.approx(0.01)       # $1 per price unit, pip = 0.01
    reported = (info.pip_size / info.trade_tick_size) * info.trade_tick_value
    assert reported == pytest.approx(0.0001)                   # what the symbol claims: 100x too small


def test_a_symbol_that_reports_correctly_measures_the_same():
    info = broker(EURUSD, 1.0850, truth=100_000.0).get_symbol_info("EURUSD")
    assert info.pip_value_per_lot == pytest.approx(10.0)


def test_an_unmeasurable_symbol_falls_back_to_the_reported_value():
    info = broker(EURUSD, 1.0850, truth=None).get_symbol_info("EURUSD")
    assert info.measured_pip_value == 0.0 and info.pip_value_per_lot == pytest.approx(10.0)


# ---- lot sizing ------------------------------------------------------------------


def test_lots_are_sized_so_the_stop_costs_the_intended_amount_on_a_misreporting_symbol():
    b = broker(V75, 33_000.0, truth=1.0)
    lots = b.calculate_lots("Volatility 75 Index", BALANCE, 2.0, 90_000)       # a 900-point stop
    loss = true_loss(b.get_symbol_info("Volatility 75 Index"), lots, 90_000)
    assert loss == pytest.approx(200.0, rel=0.02)                              # 2% of 10,000 (was ~2,000)


def test_forex_sizing_is_unchanged():
    assert broker(EURUSD, 1.0850, truth=100_000.0).calculate_lots("EURUSD", BALANCE, 2.0, 25) == 0.8


def test_without_a_measurement_sizing_is_as_before():
    # unmeasured V75: the reported 0.0001 is floored at 0.001, as the old code did
    assert broker(V75, 33_000.0, truth=None).calculate_lots("Volatility 75 Index", BALANCE, 2.0, 90_000) \
        == pytest.approx(2.222, abs=0.001)


def test_the_minimum_lot_is_kept_when_even_it_is_too_big():
    spec = {**V75, "volume_min": 1.0, "volume_step": 1.0}
    b = broker(spec, 33_000.0, truth=1.0)
    assert b.calculate_lots("Volatility 100 Index", BALANCE, 2.0, 50_000) == 1.0


# ---- guardrails use the measured value -------------------------------------------


def v100_info() -> SymbolInfo:
    return broker({**V75, "volume_min": 1.0, "volume_step": 1.0}, 600.0, truth=1.0).get_symbol_info("Volatility 100 Index")


def guard(risk_pct: float, **kw):
    dec = TradeDecision(OPEN, "Volatility 100 Index", "sell", risk_pct, 50_000, 100_000, "r", 0.9, "x")
    base = dict(max_risk_pct=2.0, max_positions=12, daily_loss_limit_pct=60.0,
                spread_max_pips=5.0, synthetic_spread_max_pips=1e9, confidence_threshold=0.72)
    return apply_guardrails(dec, AccountState(BALANCE, BALANCE, 0.0, BALANCE, 0.0, 0.0), [], v100_info(), **{**base, **kw})


def test_a_symbol_whose_minimum_lot_is_too_big_for_the_risk_is_refused():
    # the smallest lot risks 50,000 pips x $0.01 = $500, and a 2% trade may only risk $200
    g = guard(2.0)
    assert g.blocked and "minimum lot" in g.notes[0]


def test_the_check_uses_the_risk_after_the_caps_not_the_risk_the_model_asked_for():
    # the model asks for 10% ($1,000, which would let the $500 minimum lot through) but is capped to 2%
    assert guard(10.0).blocked


def test_the_same_trade_is_allowed_when_the_allowed_risk_covers_the_minimum_lot():
    assert not guard(5.0, max_risk_pct=5.0).blocked


# ---- the total-risk cap counts positions at their true risk ----------------------


def test_open_risk_counts_a_misreporting_symbol_at_its_true_cost():
    held = Position(1, "Volatility 75 Index", "sell", 2.184, 33_183.15, 34_089.95, 31_389.95, 0.0,
                    datetime.now(timezone.utc))
    info = broker(V75, 33_000.0, truth=1.0).get_symbol_info("Volatility 75 Index")
    pct = open_risk_pct([held], BALANCE, lambda s: info, fallback_pct=2.0)
    assert pct == pytest.approx(19.8, abs=0.1)         # a 20% position; the reported ticks would say 0.2%
