from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading.audit import TradeAudit
from trading.broker.metatrader import MAGIC, BotEntry, ClosedTrade, MT5Broker, Position
from trading.config import Settings
from trading.runner import TradingRunner

NOW = datetime.now(timezone.utc)
HOURS_AGO = NOW - timedelta(hours=5)


# ---- broker: which deals count as the bot's entries ------------------------------


def deal(entry=0, magic=MAGIC, type_=0, position_id=1, symbol="EURUSD", volume=0.1, price=1.1, time=0):
    return SimpleNamespace(entry=entry, magic=magic, type=type_, position_id=position_id,
                           symbol=symbol, volume=volume, price=price, time=time)


def broker_with_deals(deals) -> MT5Broker:
    mt5 = SimpleNamespace(
        DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_TYPE_BUY=0, DEAL_TYPE_SELL=1,
        terminal_info=lambda: SimpleNamespace(connected=True),
        history_deals_get=lambda start, end: deals,
    )
    b = MT5Broker()
    b._mt5, b._connected = mt5, True
    return b


def test_only_entry_deals_with_the_bots_magic_number_are_listed():
    deals = [
        deal(position_id=1, type_=0, symbol="EURUSD", volume=0.5, price=1.2, time=1000),   # ours, buy
        deal(position_id=2, type_=1, symbol="XAUUSD"),                                      # ours, sell
        deal(position_id=3, magic=0),                                                       # opened by hand
        deal(position_id=4, entry=1),                                                       # an exit deal
        deal(position_id=5, type_=2),                                                       # balance operation
    ]
    got = broker_with_deals(deals).get_bot_entries(NOW - timedelta(days=30))
    assert [(e.position_id, e.symbol, e.direction) for e in got] == [(1, "EURUSD", "buy"), (2, "XAUUSD", "sell")]
    assert (got[0].lots, got[0].price) == (0.5, 1.2)
    assert got[0].opened_at == datetime.fromtimestamp(1000, tz=timezone.utc)


def test_no_history_gives_an_empty_list():
    assert broker_with_deals(None).get_bot_entries(NOW) == []


# ---- audit -----------------------------------------------------------------------


def test_close_rows_do_not_make_a_ticket_known(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    a.record(run_id="r", mode="live", symbol="EURUSD", proposed_action="open", final_action="open", position_ticket=1)
    a.record(run_id="r", mode="live", symbol="EURUSD", proposed_action="close", final_action="close", position_ticket=2)
    a.record(run_id="r", mode="paper", symbol="EURUSD", proposed_action="open", final_action="open", position_ticket=3)
    assert a.known_open_tickets("live") == {1}


def test_record_can_backdate_the_row(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    a.record(run_id="r", mode="live", symbol="X", proposed_action="open", final_action="open", created_at=HOURS_AGO)
    assert a.recent(1)[0]["created_at"] == HOURS_AGO.isoformat(timespec="seconds")


# ---- runner ----------------------------------------------------------------------


class FakeBroker:
    def __init__(self, entries, positions=(), closed=None):
        self._entries, self._positions, self._closed = list(entries), list(positions), closed or {}

    def get_bot_entries(self, since):
        return [e for e in self._entries if e.opened_at >= since]

    def get_positions(self):
        return self._positions

    def get_closed_trade(self, ticket):
        return self._closed.get(ticket)

    def ensure_connected(self):
        return True


def entry(ticket, symbol="EURUSD", opened_at=HOURS_AGO, direction="sell"):
    return BotEntry(ticket, symbol, direction, 2.0, 1.11712, opened_at)


@pytest.fixture
def fresh_runner(tmp_path):
    """A runner that has never run crash recovery before."""
    s = replace(Settings.from_env(tmp_path / "none.env"), mode="live",
                state_db=tmp_path / "a.sqlite", paper_db=tmp_path / "p.sqlite")
    return TradingRunner(s)


@pytest.fixture
def runner(fresh_runner):
    """Crash recovery has been counting since two days ago."""
    fresh_runner._audit.set_meta("adopt_since", (NOW - timedelta(days=2)).isoformat())
    return fresh_runner


def test_the_first_run_only_sets_the_starting_point(fresh_runner):
    fresh_runner._broker = FakeBroker([entry(101)])           # a trade from before the feature existed
    assert fresh_runner.adopt_orphans() == 0
    assert fresh_runner._audit.get_meta("adopt_since") is not None
    assert fresh_runner.adopt_orphans() == 0                  # still not adopted on later runs
    assert fresh_runner._audit.recent(5) == []


def test_only_trades_opened_after_the_starting_point_are_adopted(runner):
    runner._audit.set_meta("adopt_since", (NOW - timedelta(hours=1)).isoformat())
    runner._broker = FakeBroker([
        entry(101, opened_at=NOW - timedelta(hours=3)),       # before: legacy, ignored
        entry(102, "XAUUSD", opened_at=NOW - timedelta(minutes=30)),
    ])
    assert runner.adopt_orphans() == 1
    assert runner._audit.recent(1)[0]["position_ticket"] == 102


def test_a_position_missing_from_the_database_is_adopted(runner):
    runner._broker = FakeBroker([entry(101)])
    assert runner.adopt_orphans() == 1
    row = runner._audit.recent(1)[0]
    assert (row["symbol"], row["direction"], row["lots"], row["entry_price"]) == ("EURUSD", "sell", 2.0, 1.11712)
    assert (row["setup"], row["position_ticket"], row["outcome"]) == ("adopted", 101, "open")
    assert row["created_at"] == HOURS_AGO.isoformat(timespec="seconds")       # when it was really opened


def test_adopted_positions_that_are_still_open_keep_their_stop_and_target(runner):
    held = Position(101, "EURUSD", "sell", 2.0, 1.11712, 1.11962, 1.11162, 0.0, HOURS_AGO)
    runner._broker = FakeBroker([entry(101)], positions=[held])
    runner.adopt_orphans()
    row = runner._audit.recent(1)[0]
    assert (row["sl_price"], row["tp_price"]) == (1.11962, 1.11162)


def test_known_positions_are_not_adopted_again(runner):
    runner._audit.record(run_id="r", mode="live", symbol="EURUSD", proposed_action="open",
                         final_action="open", position_ticket=101)
    runner._broker = FakeBroker([entry(101), entry(102, "XAUUSD")])
    assert runner.adopt_orphans() == 1
    assert runner.adopt_orphans() == 0                      # idempotent
    assert len([r for r in runner._audit.recent(10) if r["final_action"] == "open"]) == 2


def test_an_adopted_trade_gets_its_result_on_the_next_reconcile(runner):
    runner._broker = FakeBroker([entry(101)], closed={101: ClosedTrade(101, -12.5, 1.1196, NOW)})
    runner.adopt_orphans()
    assert runner.reconcile_outcomes() == 1
    row = runner._audit.recent(1)[0]
    assert (row["outcome"], row["profit"]) == ("loss", -12.5)


def test_entries_newer_than_the_grace_period_are_left_for_the_scan_that_made_them(runner):
    runner._broker = FakeBroker([entry(101, opened_at=NOW - timedelta(minutes=2))])
    assert runner.adopt_orphans() == 0
    assert runner.adopt_orphans(grace_minutes=0) == 1


def test_entries_older_than_the_lookback_are_ignored(runner):
    runner._broker = FakeBroker([entry(101, opened_at=NOW - timedelta(days=45))])
    assert runner.adopt_orphans(lookback_days=30) == 0


def test_paper_mode_does_not_adopt(runner):
    runner.s = replace(runner.s, mode="paper")
    runner._broker = FakeBroker([entry(101)])
    assert runner.adopt_orphans() == 0
