from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading.audit import TradeAudit
from trading.config import Settings
from trading.decision import TradeDecision
from trading.loop import TradeResult
from trading.runner import TradingRunner

from .test_runner_breakers import ExplodingAgent, FakeBroker, FakePaper

T0 = datetime(2026, 11, 4, 12, 0, 5, tzinfo=timezone.utc)


# ---- audit -----------------------------------------------------------------------


def test_the_decision_time_and_quote_are_stored_next_to_the_order_time(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    rid = a.record(run_id="r", mode="live", symbol="EURUSD", proposed_action="open", final_action="open",
                   direction="sell", created_at=T0 + timedelta(minutes=95),
                   decided_at=T0, decision_bid=1.11700, decision_ask=1.11712)
    row = {r["id"]: r for r in a.recent(5)}[rid]
    assert row["decided_at"] == "2026-11-04T12:00:05+00:00"
    assert row["created_at"] == "2026-11-04T13:35:05+00:00"
    assert (row["decision_bid"], row["decision_ask"]) == (1.117, 1.11712)


def test_rows_without_a_decision_are_stored_with_nulls(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    rid = a.record(run_id="r", mode="live", symbol="EURUSD", proposed_action="wait", final_action="wait")
    row = {r["id"]: r for r in a.recent(5)}[rid]
    assert row["decided_at"] is None and row["decision_bid"] is None and row["decision_ask"] is None


def test_an_existing_database_gets_the_new_columns_without_losing_rows(tmp_path):
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE trade_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, created_at TEXT NOT NULL,
            mode TEXT NOT NULL, symbol TEXT NOT NULL, proposed_action TEXT NOT NULL, final_action TEXT NOT NULL,
            direction TEXT, risk_pct REAL, sl_pips INTEGER, tp_pips INTEGER, lots REAL, entry_price REAL, sl_price REAL,
            tp_price REAL, confidence REAL, setup TEXT, reason TEXT, evidence TEXT, guardrail_notes TEXT,
            position_ticket INTEGER, executed TEXT, error TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
            turns INTEGER DEFAULT 0, transcript TEXT, closed_at TEXT, close_price REAL, profit REAL, outcome TEXT);
        INSERT INTO trade_decisions (run_id, created_at, mode, symbol, proposed_action, final_action, outcome)
            VALUES ('r', '2026-10-27T00:00:00+00:00', 'live', 'XAUUSD', 'open', 'open', 'open');
    """)
    old.commit()
    old.close()
    a = TradeAudit(path)
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(trade_decisions)")}
    assert {"decided_at", "decision_bid", "decision_ask"} <= cols
    assert len(a.recent(5)) == 1
    a.record(run_id="r", mode="live", symbol="EURUSD", proposed_action="wait", final_action="wait", decided_at=T0)


# ---- the scan stamps each decision -----------------------------------------------


class QuoteBroker(FakeBroker):
    def __init__(self, quotes):
        super().__init__()
        self._quotes = quotes

    def get_symbol_info(self, symbol):
        bid, ask = self._quotes[symbol]
        return SimpleNamespace(bid=bid, ask=ask, pip_size=0.0001, spread=1)


class WaitingAgent(ExplodingAgent):
    def decide(self, symbol, account, positions):
        self.calls.append(symbol)
        return TradeResult(decision=TradeDecision.fallback("nothing to do"))


@pytest.fixture
def runner(tmp_path):
    s = replace(
        Settings.from_env(tmp_path / "none.env"),
        mode="paper", state_db=tmp_path / "agent.sqlite", paper_db=tmp_path / "paper.sqlite",
        lockout_file=tmp_path / "LOCKOUT.paper", kill_switch_file=tmp_path / "STOP",
        daily_loss_limit_pct=60.0, blackout_utc="", max_latency_ms=1000.0, market_stale_min=0.0,
    )
    r = TradingRunner(s)
    r._agent = WaitingAgent()
    r._broker = QuoteBroker({"EURUSD": (1.1170, 1.1172), "Volatility 75 Index": (36123.1, 36123.9)})
    r._paper = FakePaper()
    return r


def test_each_scanned_symbol_is_stamped_with_when_and_at_what_quote_it_was_decided(runner):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    runner.run(["EURUSD", "Volatility 75 Index"])
    after = datetime.now(timezone.utc)
    rows = {r["symbol"]: r for r in runner._audit.recent(10)}
    assert (rows["EURUSD"]["decision_bid"], rows["EURUSD"]["decision_ask"]) == (1.117, 1.1172)
    assert (rows["Volatility 75 Index"]["decision_bid"], rows["Volatility 75 Index"]["decision_ask"]) == (36123.1, 36123.9)
    for row in rows.values():
        decided = datetime.fromisoformat(row["decided_at"])
        assert before <= decided <= after
        assert decided <= datetime.fromisoformat(row["created_at"]) + timedelta(seconds=1)


def test_the_decision_is_stamped_before_the_later_symbols_are_analysed(runner):
    """created_at is written in the execution phase, after every symbol was analysed; decided_at is not."""
    import time
    original = runner._agent.decide

    def slow(symbol, account, positions):
        time.sleep(1.2)
        return original(symbol, account, positions)

    runner._agent.decide = slow
    runner.run(["EURUSD", "Volatility 75 Index"])
    rows = sorted(runner._audit.recent(10), key=lambda r: r["decided_at"])
    first, second = (datetime.fromisoformat(r["decided_at"]) for r in rows)
    assert (second - first).total_seconds() >= 1.0                 # stamped as each symbol was decided
    assert datetime.fromisoformat(rows[0]["created_at"]) - first >= timedelta(seconds=1)   # the order time is later


def test_symbols_skipped_without_a_decision_have_no_stamp(runner):
    runner.s = replace(runner.s, market_stale_min=30.0)
    runner._broker = type("B", (QuoteBroker,), {"last_tick_time": lambda self, s: 1_800_000_000 if "Index" in s else 1_700_000_000})(
        {"EURUSD": (1.1, 1.2), "Volatility 75 Index": (3.0, 4.0)})
    runner.run(["EURUSD", "Volatility 75 Index"])
    rows = {r["symbol"]: r for r in runner._audit.recent(10)}
    assert rows["EURUSD"]["decided_at"] is None and rows["Volatility 75 Index"]["decided_at"] is not None
