from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from trading.broker.metatrader import AccountState, Position
from trading.config import Settings
from trading.loop import AgentError
from trading.runner import TradingRunner


class FakeBroker:
    def __init__(self, ping: float | None = 50.0, reachable: bool = True) -> None:
        self._ping = ping
        self._reachable = reachable

    def ensure_connected(self):
        return self._reachable

    def get_bot_entries(self, since):
        return []

    def get_positions(self):
        return []

    def get_account(self):
        return AccountState(100.0, 100.0, 0.0, 100.0, 0.0, 0.0)

    def ping_ms(self):
        return self._ping

    def get_symbol_info(self, symbol):
        return None


class FakePaper:
    def __init__(self, daily_pnl: float = 0.0, positions=()) -> None:
        self._pnl = daily_pnl
        self._positions = list(positions)

    def get_daily_pnl(self):
        return self._pnl

    def get_positions(self):
        return self._positions


class ExplodingAgent:
    calls: list[str]

    def __init__(self) -> None:
        self.calls = []

    def decide(self, symbol, account, positions):
        self.calls.append(symbol)
        raise AssertionError("the model must not be called")


class FailingAgent(ExplodingAgent):
    """Records the call, then fails the way a model error does, which the runner handles."""

    def decide(self, symbol, account, positions):
        self.calls.append(symbol)
        raise AgentError("stub")


@pytest.fixture
def runner(tmp_path):
    s = replace(
        Settings.from_env(tmp_path / "none.env"),
        mode="paper", state_db=tmp_path / "agent.sqlite", paper_db=tmp_path / "paper.sqlite",
        lockout_file=tmp_path / "LOCKOUT.paper", kill_switch_file=tmp_path / "STOP",
        daily_loss_limit_pct=60.0, blackout_utc="", max_latency_ms=1000.0,
    )
    r = TradingRunner(s)
    r._agent = ExplodingAgent()
    r._broker = FakeBroker()
    r._paper = FakePaper()
    return r


def test_loss_limit_trips_lockout_and_makes_no_model_calls(runner):
    runner._paper = FakePaper(daily_pnl=-61.0)   # limit is 60% of a $100 balance
    summary = runner.run(["EURUSD", "XAUUSD"])
    assert "drawdown lockout" in summary.halted
    assert runner._agent.calls == []
    assert runner.s.lockout_file.exists()


def test_lockout_persists_into_the_next_scan_even_if_pnl_recovers(runner):
    runner._paper = FakePaper(daily_pnl=-61.0)
    runner.run(["EURUSD"])
    runner._paper = FakePaper(daily_pnl=0.0)
    summary = runner.run(["EURUSD"])
    assert "drawdown lockout" in summary.halted
    assert runner._agent.calls == []


def test_unreachable_broker_skips_the_scan_without_model_calls(runner):
    runner._broker = FakeBroker(reachable=False)
    summary = runner.run(["EURUSD", "XAUUSD"])
    assert "unreachable" in summary.halted
    assert runner._agent.calls == []
    assert not runner.s.lockout_file.exists()       # an outage is not a drawdown


def test_symbol_order_is_shuffled_each_scan_and_recorded(runner):
    symbols = [f"SYM{i}" for i in range(12)]
    orders = []
    for i in range(4):
        runner._agent = FailingAgent()
        runner.run(list(symbols))
        orders.append(list(runner._agent.calls))
        assert sorted(orders[-1]) == sorted(symbols)                  # every symbol still scanned once
        if i == 0:
            # Run ids are timestamps to the second, so only the first of these back-to-back runs
            # has its own row; real scans are minutes apart.
            assert json.loads(runner._audit.runs(1)[0]["symbols"]) == orders[0]   # the order actually used

    assert len({tuple(o) for o in orders}) > 1                        # not the same order every time


def test_loss_below_the_limit_does_not_lock(runner):
    runner._paper = FakePaper(daily_pnl=-59.0)
    runner._agent = FailingAgent()   # the scan should get as far as the model
    summary = runner.run(["EURUSD"])
    assert not summary.halted
    assert runner._agent.calls == ["EURUSD"]
    assert not runner.s.lockout_file.exists()


def test_high_latency_skips_symbols_with_no_position(runner):
    runner._broker = FakeBroker(ping=2500.0)
    summary = runner.run(["EURUSD", "Volatility 75 Index"])
    assert runner._agent.calls == []
    assert summary.blocked["halted"] == 2
    assert not runner.s.lockout_file.exists()


def test_halted_symbol_with_an_open_position_still_reaches_the_model(runner):
    held = Position(7, "EURUSD", "buy", 0.01, 1.0850, 1.0800, 1.0950, 0.0, datetime.now(timezone.utc))
    runner._agent = FailingAgent()
    runner._broker = FakeBroker(ping=2500.0)
    runner._paper = FakePaper(positions=[
        type("P", (), dict(ticket=7, symbol="EURUSD", direction="buy", lots=0.01,
                           open_price=1.0850, sl=1.0800, tp=1.0950, open_time=held.open_time))(),
    ])
    runner.run(["EURUSD", "XAUUSD"])
    assert runner._agent.calls == ["EURUSD"]
