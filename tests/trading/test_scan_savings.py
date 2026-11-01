from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from trading import loop
from trading.breakers import closed_markets
from trading.config import Settings
from trading.decision import TradeDecision
from trading.loop import AgentError, MarketAgent, TradeResult, UsageLimitError, is_usage_limit
from trading.runner import TradingRunner

from .test_runner_breakers import ExplodingAgent, FakeBroker, FakePaper

NOW = 1_800_000_000
LIMIT_TEXT = "You've hit your limit · resets Nov 1, 2am (Africa/Johannesburg) (HTTP 429)"


# ---- which markets are closed ----------------------------------------------------


def test_forex_and_metals_with_stale_quotes_are_closed_while_synthetics_never_are():
    ticks = {"EURUSD": NOW - 2 * 86400, "XAUUSD": NOW - 2 * 86400,
             "Volatility 75 Index": NOW - 1, "Boom 500 Index": NOW - 5}
    closed = closed_markets(ticks, stale_min=30)
    assert set(closed) == {"EURUSD", "XAUUSD"}
    assert "closed" in closed["EURUSD"]


def test_a_synthetic_is_never_reported_even_with_an_old_quote():
    ticks = {"EURUSD": NOW, "Step Index": NOW - 3 * 86400}
    assert closed_markets(ticks, 30) == {}


def test_the_staleness_boundary_is_thirty_minutes():
    ticks = {"EURUSD": NOW - 30 * 60, "GBPUSD": NOW - 30 * 60 - 1, "Volatility 10 Index": NOW}
    assert set(closed_markets(ticks, 30)) == {"GBPUSD"}


def test_zero_disables_the_check():
    assert closed_markets({"EURUSD": NOW - 99999, "Volatility 10 Index": NOW}, 0) == {}


def test_a_missing_quote_counts_as_closed_for_forex_only():
    ticks = {"EURUSD": None, "Boom 300 Index": None, "XAUUSD": NOW}
    assert set(closed_markets(ticks, 30)) == {"EURUSD"}


def test_nothing_to_compare_against_means_nothing_is_closed():
    assert closed_markets({}, 30) == {}
    assert closed_markets({"EURUSD": None}, 30) == {}


# ---- telling a usage limit from an ordinary error --------------------------------


def test_the_real_usage_limit_message_is_recognised():
    assert is_usage_limit(f"Model call failed (Agent SDK): {LIMIT_TEXT}")
    assert is_usage_limit("You have reached your usage limit")


@pytest.mark.parametrize("text", ["HTTP 429: rate limited, retry in 5 seconds", "timed out", "connection reset", ""])
def test_other_errors_are_not_usage_limits(text):
    assert not is_usage_limit(text)


def test_the_agent_raises_a_usage_limit_error_only_for_a_usage_limit(monkeypatch):
    from agentcore.sdk_backend import SdkError

    agent = MarketAgent(client=object(), model="m", system_prompt="p", toolbox=SimpleNamespace(schemas=lambda: []))
    monkeypatch.setattr(loop, "is_sdk", lambda client: True)

    def fail_with(text):
        def boom(*a, **k):
            raise SdkError(text)
        monkeypatch.setattr(loop, "_sdk_decide_trade", boom)
        with pytest.raises(AgentError) as info:
            agent.decide("EURUSD", None, [])
        return info.value

    assert isinstance(fail_with(LIMIT_TEXT), UsageLimitError)
    other = fail_with("timed out")
    assert type(other) is AgentError and not isinstance(other, UsageLimitError)


# ---- the scan -------------------------------------------------------------------


class QuotesBroker(FakeBroker):
    def __init__(self, ticks):
        super().__init__()
        self._ticks = ticks

    def last_tick_time(self, symbol):
        return self._ticks.get(symbol)


class WaitingAgent(ExplodingAgent):
    def decide(self, symbol, account, positions):
        self.calls.append(symbol)
        return TradeResult(decision=TradeDecision.fallback("nothing to do"))


class LimitedAgent(ExplodingAgent):
    """Decides normally once, then reports the usage limit on every later call."""

    def decide(self, symbol, account, positions):
        self.calls.append(symbol)
        if len(self.calls) > 1:
            raise UsageLimitError(f"Model call failed (Agent SDK): {LIMIT_TEXT}")
        return TradeResult(decision=TradeDecision.fallback("nothing to do"))


@pytest.fixture
def runner(tmp_path):
    s = replace(
        Settings.from_env(tmp_path / "none.env"),
        mode="paper", state_db=tmp_path / "agent.sqlite", paper_db=tmp_path / "paper.sqlite",
        lockout_file=tmp_path / "LOCKOUT.paper", kill_switch_file=tmp_path / "STOP",
        daily_loss_limit_pct=60.0, blackout_utc="", max_latency_ms=1000.0, market_stale_min=30.0,
    )
    r = TradingRunner(s)
    r._agent = WaitingAgent()
    r._broker = FakeBroker()
    r._paper = FakePaper()
    return r


SYMBOLS = ["EURUSD", "XAUUSD", "Volatility 75 Index", "Boom 500 Index", "Step Index", "GBPUSD"]


def test_closed_markets_are_skipped_without_a_model_call_and_recorded(runner):
    runner._broker = QuotesBroker({"EURUSD": NOW - 86400, "XAUUSD": NOW - 86400, "GBPUSD": NOW - 86400,
                                   "Volatility 75 Index": NOW, "Boom 500 Index": NOW, "Step Index": NOW})
    summary = runner.run(SYMBOLS)
    assert sorted(runner._agent.calls) == ["Boom 500 Index", "Step Index", "Volatility 75 Index"]
    assert summary.blocked["market_closed"] == 3
    rows = [r for r in runner._audit.recent(50) if r["symbol"] == "EURUSD"]
    assert rows and rows[0]["final_action"] == "wait" and "closed" in rows[0]["guardrail_notes"]


def test_nothing_is_skipped_when_every_market_is_open(runner):
    runner._broker = QuotesBroker({s: NOW for s in SYMBOLS})
    runner.run(SYMBOLS)
    assert sorted(runner._agent.calls) == sorted(SYMBOLS)


def test_the_check_can_be_switched_off(runner):
    runner.s = replace(runner.s, market_stale_min=0.0)
    runner._broker = QuotesBroker({"EURUSD": NOW - 86400, "Volatility 75 Index": NOW})
    runner.run(["EURUSD", "Volatility 75 Index"])
    assert sorted(runner._agent.calls) == ["EURUSD", "Volatility 75 Index"]


def test_a_failing_quote_check_does_not_stop_the_scan(runner):
    class Broken(QuotesBroker):
        def last_tick_time(self, symbol):
            raise RuntimeError("terminal hiccup")
    runner._broker = Broken({})
    runner.run(["EURUSD", "XAUUSD"])
    assert sorted(runner._agent.calls) == ["EURUSD", "XAUUSD"]


def test_the_scan_stops_at_the_first_usage_limit_and_says_so(runner):
    runner._agent = LimitedAgent()
    summary = runner.run(SYMBOLS)
    assert len(runner._agent.calls) == 2                       # one decision, then the limit; the other four never asked
    assert summary.errors == 1 and "hit your limit" in summary.usage_limit
    assert "STOPPED EARLY" in summary.line()


def test_an_ordinary_agent_error_does_not_stop_the_scan(runner):
    class Flaky(ExplodingAgent):
        def decide(self, symbol, account, positions):
            self.calls.append(symbol)
            raise AgentError("timed out")
    runner._agent = Flaky()
    summary = runner.run(SYMBOLS)
    assert len(runner._agent.calls) == len(SYMBOLS) and summary.errors == len(SYMBOLS) and not summary.usage_limit
