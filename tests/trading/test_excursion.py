from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading.audit import TradeAudit
from trading.broker.metatrader import ClosedTrade, MT5Broker
from trading.config import Settings
from trading.runner import TradingRunner

OPENED = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
CLOSED = OPENED + timedelta(hours=3)


# ---- broker: best and worst price while a trade was open -------------------------


def bars(*pairs):
    return [{"high": h, "low": l} for h, l in pairs]


def fake_mt5(by_timeframe: dict, deals=()):
    calls = []

    def copy_rates_range(symbol, tf, start, end):
        calls.append((symbol, tf, start, end))
        return by_timeframe.get(tf)

    return SimpleNamespace(
        DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_INOUT=2, DEAL_ENTRY_OUT_BY=3,
        terminal_info=lambda: SimpleNamespace(connected=True),
        copy_rates_range=copy_rates_range, calls=calls,
        positions_get=lambda ticket: [], history_deals_get=lambda position: list(deals),
    )


def broker_with(mt5) -> MT5Broker:
    b = MT5Broker()
    b._mt5, b._connected = mt5, True
    return b


def test_a_sell_gains_when_price_falls_and_is_hurt_when_it_rises():
    b = broker_with(fake_mt5({1: bars((100.5, 99.0), (101.5, 97.0), (99.0, 98.0))}))
    assert b.get_price_excursion("EURUSD", "sell", 100.0, OPENED, CLOSED) == (3.0, 1.5)


def test_a_buy_is_the_mirror():
    b = broker_with(fake_mt5({1: bars((100.5, 99.0), (101.5, 97.0), (99.0, 98.0))}))
    assert b.get_price_excursion("EURUSD", "buy", 100.0, OPENED, CLOSED) == (1.5, 3.0)


def test_distances_are_never_negative():
    # price never traded above the entry, so a sell was never hurt
    b = broker_with(fake_mt5({1: bars((99.5, 98.0))}))
    assert b.get_price_excursion("X", "sell", 100.0, OPENED, CLOSED) == (2.0, 0.0)


def test_the_window_is_exactly_the_trades_lifetime():
    mt5 = fake_mt5({1: bars((100.5, 99.5))})
    broker_with(mt5).get_price_excursion("EURUSD", "sell", 100.0, OPENED, CLOSED)
    assert mt5.calls == [("EURUSD", 1, OPENED, CLOSED)]


def test_falls_back_to_coarser_candles_when_finer_ones_are_missing():
    mt5 = fake_mt5({1: None, 5: [], 15: bars((102.0, 98.0))})
    assert broker_with(mt5).get_price_excursion("X", "buy", 100.0, OPENED, CLOSED) == (2.0, 2.0)
    assert [c[1] for c in mt5.calls] == [1, 5, 15]


def test_no_candles_at_all_gives_none():
    assert broker_with(fake_mt5({})).get_price_excursion("X", "buy", 100.0, OPENED, CLOSED) is None


def test_closed_trade_carries_the_actual_fill_of_the_entry_deal():
    deals = [
        SimpleNamespace(entry=0, profit=0.0, commission=0.0, swap=0.0, price=1.11712, time=int(OPENED.timestamp())),
        SimpleNamespace(entry=1, profit=-5.0, commission=0.0, swap=0.0, price=1.11962, time=int(CLOSED.timestamp())),
    ]
    got = broker_with(fake_mt5({}, deals)).get_closed_trade(7)
    assert got.open_price == 1.11712 and got.opened_at == OPENED and got.close_price == 1.11962


# ---- audit -----------------------------------------------------------------------


def test_an_existing_database_gains_the_new_columns_and_keeps_its_rows(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trade_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
                 "created_at TEXT NOT NULL, mode TEXT NOT NULL, symbol TEXT NOT NULL, "
                 "proposed_action TEXT NOT NULL, final_action TEXT NOT NULL)")
    conn.execute("INSERT INTO trade_decisions (run_id, created_at, mode, symbol, proposed_action, final_action) "
                 "VALUES ('r','2026-10-20T00:00:00','live','EURUSD','open','open')")
    conn.commit()
    conn.close()

    TradeAudit(path)
    TradeAudit(path)                                      # opening it again must not fail or duplicate

    conn = sqlite3.connect(path)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(trade_decisions)")}
    assert {"mfe_r", "mae_r", "final_r", "excursion_tried"} <= columns
    assert conn.execute("SELECT symbol FROM trade_decisions").fetchall() == [("EURUSD",)]


def test_exit_stats_with_nothing_measured(tmp_path):
    assert TradeAudit(tmp_path / "a.sqlite").exit_stats() == {"measured": 0}


def test_exit_stats_summarise_best_worst_and_what_was_kept(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    # (best, worst, final) in R: a winner that kept half, a loser that was 1.5R ahead first, a plain stop-out
    for mfe, mae, final in [(2.0, 0.5, 1.0), (1.5, 0.2, -0.5), (0.0, 1.0, -1.0)]:
        i = a.record(run_id="r", mode="live", symbol="X", proposed_action="open", final_action="open", position_ticket=1)
        a.record_excursion(i, mfe, mae, final)
    s = a.exit_stats()
    assert s["measured"] == 3 and s["avg_best_r"] == 1.17 and s["avg_worst_r"] == -0.57 and s["avg_final_r"] == -0.17
    assert s["kept_pct"] == pytest.approx((1.0 - 0.5) / (2.0 + 1.5) * 100, abs=0.1)   # only trades that were ever ahead
    assert (s["ever_1r"], s["gave_back"]) == (2, 1)
    # final R values 1.0, -0.5, -1.0: mean -0.167, sd 1.041, n 3  ->  t = -0.28
    assert s["final_r_t_stat"] == pytest.approx(-0.28, abs=0.01)


def test_final_r_t_stat_needs_variation_and_at_least_two_trades(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    i = a.record(run_id="r", mode="live", symbol="X", proposed_action="open", final_action="open", position_ticket=1)
    a.record_excursion(i, 1.0, 0.2, 0.5)
    assert a.exit_stats()["final_r_t_stat"] is None            # one trade
    j = a.record(run_id="r", mode="live", symbol="X", proposed_action="open", final_action="open", position_ticket=2)
    a.record_excursion(j, 1.0, 0.2, 0.5)
    assert a.exit_stats()["final_r_t_stat"] is None            # two identical results: no spread to measure


# ---- runner ----------------------------------------------------------------------


class FakeBroker:
    def __init__(self, closed, span=(3.0, 1.0), reachable=True):
        self._closed, self._span, self._reachable = closed, span, reachable
        self.lookups, self.span_calls = [], []

    def ensure_connected(self):
        return self._reachable

    def get_closed_trade(self, ticket):
        self.lookups.append(ticket)
        return self._closed.get(ticket)

    def get_price_excursion(self, *args):
        self.span_calls.append(args)
        return self._span


@pytest.fixture
def runner(tmp_path):
    s = replace(Settings.from_env(tmp_path / "none.env"), mode="live",
                state_db=tmp_path / "a.sqlite", paper_db=tmp_path / "p.sqlite")
    return TradingRunner(s)


def open_row(runner, direction="sell", sl_price=102.0, ticket=11):
    return runner._audit.record(run_id="r", mode="live", symbol="EURUSD", proposed_action="open",
                                final_action="open", direction=direction, sl_price=sl_price,
                                position_ticket=ticket)


def closed(profit=-5.0, close=101.0, open_price=100.0):
    return ClosedTrade(11, profit, close, CLOSED, open_price=open_price, opened_at=OPENED)


def row(runner, i):
    return next(r for r in runner._audit.recent(20) if r["id"] == i)


def test_excursions_are_stored_in_multiples_of_the_initial_risk(runner):
    i = open_row(runner)                                  # sell at 100, stop at 102: 1R is 2.0
    runner._broker = FakeBroker({11: closed(close=101.0)}, span=(3.0, 1.0))
    runner.reconcile_outcomes()
    r = row(runner, i)
    assert (r["mfe_r"], r["mae_r"], r["final_r"]) == (1.5, 0.5, -0.5)   # a sell that closed 1.0 higher lost 0.5R


def test_a_buy_is_signed_the_other_way(runner):
    i = open_row(runner, direction="buy", sl_price=98.0)  # buy at 100, stop at 98
    runner._broker = FakeBroker({11: closed(close=103.0)}, span=(4.0, 0.5))
    runner.reconcile_outcomes()
    assert row(runner, i)["final_r"] == 1.5


def test_the_measurement_uses_the_actual_fill_and_the_trades_lifetime(runner):
    open_row(runner)
    runner._broker = FakeBroker({11: closed(open_price=100.2)})
    runner.reconcile_outcomes()
    assert runner._broker.span_calls == [("EURUSD", "sell", 100.2, OPENED, CLOSED)]


def test_each_trade_is_measured_once(runner):
    open_row(runner)
    runner._broker = FakeBroker({11: closed()})
    runner.reconcile_outcomes()
    runner.reconcile_outcomes()
    assert len(runner._broker.span_calls) == 1
    assert runner._broker.lookups == [11, 11]             # once for the result, once to measure; none on the second run


def test_a_trade_with_no_recorded_stop_is_skipped_and_not_retried(runner):
    i = open_row(runner, sl_price=0.0)
    runner._broker = FakeBroker({11: closed()})
    runner.reconcile_outcomes()
    runner.reconcile_outcomes()
    assert row(runner, i)["mfe_r"] is None and runner._broker.span_calls == []
    assert runner._broker.lookups == [11, 11]


def test_missing_price_history_is_skipped_and_not_retried(runner):
    i = open_row(runner)
    runner._broker = FakeBroker({11: closed()}, span=None)
    runner.reconcile_outcomes()
    runner.reconcile_outcomes()
    assert row(runner, i)["mfe_r"] is None and len(runner._broker.span_calls) == 1


def test_nothing_is_attempted_while_the_broker_is_unreachable(runner):
    i = open_row(runner)
    runner._audit.record_outcome(i, 101.0, -5.0, CLOSED)  # result already known
    runner._broker = FakeBroker({11: closed()}, reachable=False)
    assert runner.reconcile_outcomes() == 0
    assert runner._broker.span_calls == [] and row(runner, i)["excursion_tried"] == 0   # still to do later


def test_a_trade_that_closed_earlier_is_measured_on_a_later_run(runner):
    i = open_row(runner)
    runner._audit.record_outcome(i, 101.0, -5.0, CLOSED)  # outcome recorded before measuring existed
    runner._broker = FakeBroker({11: closed()})
    runner.reconcile_outcomes()
    assert row(runner, i)["mfe_r"] == 1.5
