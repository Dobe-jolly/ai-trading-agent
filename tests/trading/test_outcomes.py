from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trading.audit import TradeAudit
from trading.broker.metatrader import AccountState, ClosedTrade, MT5Broker, SymbolInfo
from trading.config import Settings
from trading.decision import OPEN, TradeDecision
from trading.runner import TradingRunner

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def record_open(audit: TradeAudit, symbol="EURUSD", ticket=101, setup="trend_continuation", mode="live") -> int:
    return audit.record(run_id="r", mode=mode, symbol=symbol, proposed_action="open", final_action="open",
                        direction="buy", setup=setup, position_ticket=ticket)


# ---- audit -----------------------------------------------------------------------


def test_only_opens_with_a_ticket_and_no_result_are_unresolved(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    with_ticket = record_open(a, ticket=101)
    record_open(a, ticket=None)                                        # opened before tickets were kept
    a.record(run_id="r", mode="live", symbol="EURUSD", proposed_action="wait", final_action="wait")
    done = record_open(a, ticket=102)
    a.record_outcome(done, 1.1, 5.0, T0)

    assert [r["id"] for r in a.unresolved_opens("live")] == [with_ticket]
    assert a.unresolved_opens("paper") == []


def test_record_outcome_classifies_and_keeps_the_broker_close_time(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    win, loss, flat = record_open(a, ticket=1), record_open(a, ticket=2), record_open(a, ticket=3)
    a.record_outcome(win, 1.2, 7.5, T0)
    a.record_outcome(loss, 1.0, -3.0, T0)
    a.record_outcome(flat, 1.1, 0.0, T0)

    rows = {r["id"]: r for r in a.recent(10)}
    assert (rows[win]["outcome"], rows[loss]["outcome"], rows[flat]["outcome"]) == ("win", "loss", "breakeven")
    assert rows[win]["closed_at"] == "2026-10-27T12:00:00+00:00"


def test_stats_win_rate_is_over_closed_trades_not_all_opens(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    a.record_outcome(record_open(a, ticket=1), 1.2, 10.0, T0)
    a.record_outcome(record_open(a, ticket=2), 1.0, -4.0, T0)
    record_open(a, ticket=3)                                           # still open

    s = a.stats()
    assert (s["opens"], s["closed"], s["no_result_yet"]) == (3, 2, 1)
    assert s["win_rate_pct"] == 50.0
    assert s["total_pnl"] == 6.0


def test_stats_win_rate_is_none_until_something_closes(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    record_open(a, ticket=1)
    s = a.stats()
    assert s["win_rate_pct"] is None and s["avg_pnl_per_trade"] is None and s["pnl_t_stat"] is None


def test_stats_average_pnl_and_t_stat(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    for ticket, pnl in enumerate([10.0, -4.0, 6.0, -2.0, 8.0], start=1):
        a.record_outcome(record_open(a, ticket=ticket), 1.0, pnl, T0)
    record_open(a, ticket=99)                                          # still open: ignored
    s = a.stats()
    assert s["avg_pnl_per_trade"] == 3.6
    # mean 3.6, sample sd 6.229, n 5  ->  t = 3.6 / (6.229 / sqrt(5)) = 1.29
    assert s["pnl_t_stat"] == pytest.approx(1.29, abs=0.01)


def test_t_stat_is_none_with_one_trade_or_no_variation(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    a.record_outcome(record_open(a, ticket=1), 1.0, 5.0, T0)
    assert a.stats()["pnl_t_stat"] is None
    a.record_outcome(record_open(a, ticket=2), 1.0, 5.0, T0)
    assert a.stats()["pnl_t_stat"] is None


def test_breakdown_groups_closed_trades_best_first(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    a.record_outcome(record_open(a, "EURUSD", 1, "breakout"), 1, 10.0, T0)
    a.record_outcome(record_open(a, "EURUSD", 2, "breakout"), 1, -2.0, T0)
    a.record_outcome(record_open(a, "XAUUSD", 3, "reversal"), 1, -5.0, T0)
    record_open(a, "XAUUSD", 4, "reversal")                            # open: not counted

    by_setup = a.breakdown("setup")
    assert [(r["key"], r["closed"], r["wins"], r["pnl"]) for r in by_setup] == [
        ("breakout", 2, 1, 8.0), ("reversal", 1, 0, -5.0)]
    assert [r["key"] for r in a.breakdown("symbol")] == ["EURUSD", "XAUUSD"]


def test_breakdown_rejects_unknown_columns(tmp_path):
    with pytest.raises(ValueError):
        TradeAudit(tmp_path / "a.sqlite").breakdown("symbol; DROP TABLE trade_decisions")


# ---- broker deal history ---------------------------------------------------------


def deal(entry, profit=0.0, commission=0.0, swap=0.0, price=1.0, time=0):
    return SimpleNamespace(entry=entry, profit=profit, commission=commission, swap=swap, price=price, time=time)


def fake_mt5(deals, still_open=()):
    return SimpleNamespace(
        DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_INOUT=2, DEAL_ENTRY_OUT_BY=3,
        terminal_info=lambda: SimpleNamespace(connected=True),
        positions_get=lambda ticket: list(still_open),
        history_deals_get=lambda position: deals,
    )


def broker_with(mt5) -> MT5Broker:
    b = MT5Broker()
    b._mt5, b._connected = mt5, True
    return b


def test_closed_trade_nets_commission_and_swap_across_entry_and_exit_deals():
    entry = deal(0, commission=-0.4, price=1.1000, time=100)
    exit_ = deal(1, profit=12.0, commission=-0.4, swap=-0.2, price=1.1050, time=200)
    got = broker_with(fake_mt5([entry, exit_])).get_closed_trade(7)
    assert got == ClosedTrade(7, pytest.approx(11.0), 1.1050, datetime.fromtimestamp(200, tz=timezone.utc),
                              open_price=1.1000, opened_at=datetime.fromtimestamp(100, tz=timezone.utc))


def test_closed_trade_is_none_while_the_position_is_open_or_has_no_exit():
    entry = deal(0, price=1.1, time=100)
    assert broker_with(fake_mt5([entry, deal(1, price=1.2, time=200)], still_open=[object()])).get_closed_trade(7) is None
    assert broker_with(fake_mt5([entry])).get_closed_trade(7) is None
    assert broker_with(fake_mt5([])).get_closed_trade(7) is None


def test_closed_trade_uses_the_last_exit_when_closed_in_parts():
    deals = [deal(0, time=1), deal(1, profit=2.0, price=1.10, time=2), deal(1, profit=3.0, price=1.20, time=3)]
    got = broker_with(fake_mt5(deals)).get_closed_trade(7)
    assert got.profit == 5.0 and got.close_price == 1.20


# ---- runner ----------------------------------------------------------------------


class FakeBroker:
    def __init__(self, closed=None):
        self._closed = closed or {}
        self.lookups = []

    def ensure_connected(self):
        return True

    def get_closed_trade(self, ticket):
        self.lookups.append(ticket)
        return self._closed.get(ticket)

    def calculate_lots(self, *a):
        return 0.01

    def place_order(self, *a):
        return 4242

    def get_symbol_info(self, symbol):
        return None


@pytest.fixture
def runner(tmp_path):
    s = replace(Settings.from_env(tmp_path / "none.env"), mode="live",
                state_db=tmp_path / "a.sqlite", paper_db=tmp_path / "p.sqlite")
    r = TradingRunner(s)
    r._broker = FakeBroker()
    return r


def test_reconcile_records_closed_positions_and_leaves_open_ones(runner):
    done = record_open(runner._audit, ticket=11)
    open_still = record_open(runner._audit, ticket=12)
    runner._broker = FakeBroker({11: ClosedTrade(11, -4.25, 1.0950, T0)})

    assert runner.reconcile_outcomes() == 1
    rows = {r["id"]: r for r in runner._audit.recent(10)}
    assert (rows[done]["outcome"], rows[done]["profit"], rows[done]["close_price"]) == ("loss", -4.25, 1.0950)
    assert rows[open_still]["outcome"] == "open"


def test_reconcile_is_idempotent(runner):
    record_open(runner._audit, ticket=11)
    runner._broker = FakeBroker({11: ClosedTrade(11, 3.0, 1.1, T0)})
    assert runner.reconcile_outcomes() == 1
    assert runner.reconcile_outcomes() == 0
    # once to record the result and once to measure it; the second run looks up nothing
    assert runner._broker.lookups == [11, 11]


def test_stop_and_target_are_priced_off_the_quote_at_execution_not_at_analysis(runner):
    stale = SymbolInfo("EURUSD", 5, 0.00001, 0.00001, 1.0, 100000, 0.01, 100.0, 0.01, 10, 1.0850, 1.0851)
    fresh = SymbolInfo("EURUSD", 5, 0.00001, 0.00001, 1.0, 100000, 0.01, 100.0, 0.01, 10, 1.0800, 1.0801)
    placed = {}

    class Broker(FakeBroker):
        def get_symbol_info(self, symbol):
            return fresh                     # the market has moved since the decision

        def place_order(self, symbol, direction, lots, sl, tp):
            placed.update(sl=sl, tp=tp)
            return 4242

    runner._broker = Broker()
    acct = AccountState(100.0, 100.0, 0.0, 100.0, 0.0, 0.0)
    dec = TradeDecision(OPEN, "EURUSD", "sell", 5.0, 50, 100, "r", 0.9, "trend_continuation")

    runner._execute("EURUSD", dec, stale, acct)
    assert placed["sl"] == round(1.0800 + 50 * 0.0001, 5)       # 50 pips above the current bid
    assert placed["tp"] == round(1.0800 - 100 * 0.0001, 5)      # not above the stale 1.0850


def test_the_analysis_time_quote_is_the_fallback_when_no_fresh_one_is_available(runner):
    stale = SymbolInfo("EURUSD", 5, 0.00001, 0.00001, 1.0, 100000, 0.01, 100.0, 0.01, 10, 1.0850, 1.0851)
    placed = {}

    class Broker(FakeBroker):
        def get_symbol_info(self, symbol):
            return None

        def place_order(self, symbol, direction, lots, sl, tp):
            placed.update(sl=sl)
            return 1

    runner._broker = Broker()
    acct = AccountState(100.0, 100.0, 0.0, 100.0, 0.0, 0.0)
    dec = TradeDecision(OPEN, "EURUSD", "sell", 5.0, 50, 100, "r", 0.9, "trend_continuation")
    runner._execute("EURUSD", dec, stale, acct)
    assert placed["sl"] == round(1.0850 + 50 * 0.0001, 5)


def test_open_records_the_broker_ticket(runner):
    info = SymbolInfo("EURUSD", 5, 0.00001, 0.00001, 1.0, 100000, 0.01, 100.0, 0.01, 10, 1.0850, 1.0851)
    acct = AccountState(100.0, 100.0, 0.0, 100.0, 0.0, 0.0)
    dec = TradeDecision(OPEN, "EURUSD", "buy", 5.0, 50, 100, "r", 0.9, "trend_continuation")

    *_, ticket = runner._execute("EURUSD", dec, info, acct)
    assert ticket == 4242
