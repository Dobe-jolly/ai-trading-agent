from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading.broker.metatrader import MT5Broker, MT5Error

CREDS = (123, "pw", "Deriv-Demo")
BACKOFF = (1.0, 2.0, 4.0, 8.0)


class FakeMT5:
    """Stands in for the MetaTrader5 module. `initialize_results` are consumed one per attempt."""

    def __init__(self, link_up: bool = True, initialize_results=()):
        self.link_up = link_up
        self.initialize_results = list(initialize_results)
        self.calls: list = []

    def terminal_info(self):
        return SimpleNamespace(connected=self.link_up, ping_last=50_000)

    def shutdown(self):
        self.calls.append("shutdown")

    def initialize(self, **kwargs):
        self.calls.append(("initialize", kwargs))
        ok = self.initialize_results.pop(0) if self.initialize_results else True
        self.link_up = ok
        return ok

    def account_info(self):
        return SimpleNamespace(balance=100.0, equity=100.0, margin=0.0, margin_free=100.0,
                               profit=0.0, currency="USD")


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def __call__(self):
        return self.now


def broker(mt5: FakeMT5, clock: Clock | None = None) -> tuple[MT5Broker, Clock]:
    clock = clock or Clock()
    b = MT5Broker(backoff=BACKOFF, cooldown_s=120.0)
    b._mt5, b._connected, b._creds = mt5, True, CREDS
    b._sleep, b._clock = clock.sleep, clock
    return b, clock


def initializes(mt5: FakeMT5) -> int:
    return sum(1 for c in mt5.calls if isinstance(c, tuple) and c[0] == "initialize")


def test_a_healthy_link_is_left_alone():
    mt5 = FakeMT5(link_up=True)
    b, clock = broker(mt5)
    assert b.ensure_connected() is True
    assert clock.slept == [] and mt5.calls == []


def test_a_dropped_link_reconnects_with_the_stored_credentials():
    mt5 = FakeMT5(link_up=False)
    b, clock = broker(mt5)
    assert b.ensure_connected() is True
    assert clock.slept == [1.0]
    login, password, server = CREDS
    assert ("initialize", {"login": login, "password": password, "server": server}) in mt5.calls
    assert mt5.calls[0] == "shutdown"            # the dead session is closed before a new one starts


def test_waits_back_off_exponentially_between_failed_attempts():
    mt5 = FakeMT5(link_up=False, initialize_results=[False, False, False, True])
    b, clock = broker(mt5)
    assert b.ensure_connected() is True
    assert clock.slept == [1.0, 2.0, 4.0, 8.0]
    assert initializes(mt5) == 4


def test_gives_up_after_the_last_attempt_and_then_stops_trying_for_a_while():
    mt5 = FakeMT5(link_up=False, initialize_results=[False] * 20)
    b, clock = broker(mt5)
    assert b.ensure_connected() is False
    assert clock.slept == list(BACKOFF)

    slept, tried = len(clock.slept), initializes(mt5)
    assert b.ensure_connected() is False         # inside the cool-down: no waiting, no attempts
    assert (len(clock.slept), initializes(mt5)) == (slept, tried)

    clock.now += 121.0                           # cool-down over: it tries again
    mt5.initialize_results = [True]
    assert b.ensure_connected() is True


def test_calls_reconnect_transparently():
    mt5 = FakeMT5(link_up=False)
    b, _ = broker(mt5)
    account = b.get_account()
    assert account is not None and account.balance == 100.0


def test_calls_return_defaults_when_the_broker_is_unreachable():
    mt5 = FakeMT5(link_up=False, initialize_results=[False] * 20)
    b, _ = broker(mt5)
    assert b.get_account() is None
    assert b.get_positions() == [] and b.get_candles("EURUSD", "H1", 10) == []
    assert b.ping_ms() is None


def test_orders_raise_when_the_broker_is_unreachable():
    mt5 = FakeMT5(link_up=False, initialize_results=[False] * 20)
    b, _ = broker(mt5)
    with pytest.raises(MT5Error):
        b.place_order("EURUSD", "buy", 0.01, 0.0, 0.0)
    with pytest.raises(MT5Error):
        b.close_position(1)


def test_a_broker_that_never_connected_or_was_disconnected_does_not_reconnect_itself():
    b = MT5Broker()
    assert b.ensure_connected() is False
    mt5 = FakeMT5(link_up=False)
    b2, _ = broker(mt5)
    b2._connected = False                         # as after disconnect()
    assert b2.ensure_connected() is False and initializes(mt5) == 0
