from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trading.audit import TradeAudit
from trading.broker.metatrader import AccountState, Position, SymbolInfo
from trading.config import Settings
from trading.decision import MODIFY, OPEN, WAIT, CLOSE, TradeDecision
from trading.guardrails import apply_guardrails, atr_for
from trading.loop import TradeResult
from trading.runner import TradingRunner
from trading.tools import TradeEvidence

BALANCE = 10_000.0
ATR = 0.0010                               # EURUSD-like: 10 pips of 0.0001


def info(symbol="EURUSD", pip_digits=5) -> SymbolInfo:
    return SymbolInfo(symbol, pip_digits, 10 ** -pip_digits, 10 ** -pip_digits, 1.0, 100000,
                      0.01, 100.0, 0.01, 10, 1.0850, 1.0851, measured_pip_value=10.0)


def guard(sl_pips: int, atr_price=ATR, min_stop_atr=1.0, symbol_info=..., action=OPEN, stop_timeframe="H1", **kw):
    dec = TradeDecision(action, "EURUSD", "buy" if action == OPEN else "", 2.0, sl_pips, sl_pips * 2, "r", 0.9,
                        "x", position_ticket=7 if action == MODIFY else None, stop_timeframe=stop_timeframe)
    held = [Position(7, "EURUSD", "buy", 0.1, 1.08, 1.07, 1.09, 0.0, datetime.now(timezone.utc))] if action == MODIFY else []
    base = dict(max_risk_pct=5.0, max_positions=12, daily_loss_limit_pct=60.0, spread_max_pips=5.0,
                confidence_threshold=0.72, atr_price=atr_price, min_stop_atr=min_stop_atr)
    return apply_guardrails(dec, AccountState(BALANCE, BALANCE, 0.0, BALANCE, 0.0, 0.0), held,
                            info() if symbol_info is ... else symbol_info, **{**base, **kw})


# ---- the rule --------------------------------------------------------------------


def test_a_stop_tighter_than_one_atr_is_blocked_and_says_what_is_needed():
    g = guard(sl_pips=6)                                   # 6 pips against a 10-pip ATR
    assert g.blocked
    assert "0.60× the H1 ATR" in g.notes[0] and "at least 1×" in g.notes[0]


def test_the_note_gives_the_required_distance_in_pips():
    # pip 0.0001 here: 1 ATR = 0.0010 = 10 pips, less the 2% slack -> 10 pips (rounded up)
    g = guard(sl_pips=5, atr_price=ATR)
    assert g.blocked and "(10 pips)" in g.notes[0]


@pytest.mark.parametrize("sl_pips, allowed", [(5, False), (9, False), (10, True), (11, True), (50, True)])
def test_the_boundary(sl_pips, allowed):
    assert (not guard(sl_pips=sl_pips).blocked) is allowed


def test_a_stop_a_hair_under_one_atr_still_passes_because_the_model_rounds_to_whole_pips():
    # ATR is 10.2 pips and the model's round(atr / pip) gives 10: 98.04% of the ATR, inside the 2% slack
    assert not guard(sl_pips=10, atr_price=0.00102).blocked
    assert guard(sl_pips=9, atr_price=0.00102).blocked          # 88% of it is not


def test_a_larger_multiple_can_be_required():
    assert guard(sl_pips=15, min_stop_atr=2.0).blocked
    assert not guard(sl_pips=21, min_stop_atr=2.0).blocked


def test_the_rule_applies_to_every_symbol_not_just_synthetics():
    # a JPY-style pair and a synthetic index with very different pip sizes both use the same test
    assert guard(sl_pips=300, atr_price=5.0, symbol_info=info("Volatility 75 Index", 2)).blocked      # 300 x 0.01 = 3.0 < 5.0
    assert not guard(sl_pips=500, atr_price=5.0, symbol_info=info("Volatility 75 Index", 2)).blocked


def test_the_old_per_symbol_table_is_gone():
    from trading import guardrails
    assert not hasattr(guardrails, "_SYNTHETIC_MIN_SL_PIPS") and not hasattr(guardrails, "h1_atr")


# ---- the timeframe the stop is anchored to ---------------------------------------


def test_the_note_names_the_declared_timeframe():
    g = guard(sl_pips=5, stop_timeframe="D1")
    assert g.blocked and "D1 ATR" in g.notes[0] and "H1 ATR" not in g.notes[0]


def test_a_stop_anchored_to_a_slower_timeframe_must_clear_that_timeframes_atr():
    # the same 25-pip stop: fine against a 10-pip H1 ATR, far too tight against a 50-pip D1 ATR
    assert not guard(sl_pips=25, atr_price=0.0010, stop_timeframe="H1").blocked
    assert guard(sl_pips=25, atr_price=0.0050, stop_timeframe="D1").blocked
    assert not guard(sl_pips=50, atr_price=0.0050, stop_timeframe="D1").blocked


def test_the_declared_timeframe_must_be_one_that_was_analysed():
    g = guard(sl_pips=500, stop_timeframe="D1", analysed_timeframes=frozenset({"H1", "H4"}))
    assert g.blocked and "no D1 data was looked at" in g.notes[0] and "H1, H4" in g.notes[0]


def test_a_declared_timeframe_that_was_analysed_passes():
    assert not guard(sl_pips=500, stop_timeframe="D1", analysed_timeframes=frozenset({"H1", "D1"})).blocked


def test_the_analysed_check_is_skipped_when_no_record_is_given():
    assert not guard(sl_pips=500, stop_timeframe="D1", analysed_timeframes=None).blocked


def test_a_session_that_looked_at_nothing_cannot_anchor_a_stop():
    g = guard(sl_pips=500, analysed_timeframes=frozenset())
    assert g.blocked and "looked at: nothing" in g.notes[0]


def test_an_unset_timeframe_is_treated_as_h1():
    assert guard(sl_pips=5, stop_timeframe="").blocked
    assert "H1 ATR" in guard(sl_pips=5, stop_timeframe="").notes[0]


# ---- when the ATR is unknown or the rule is off ----------------------------------


def test_no_atr_means_the_stop_cannot_be_checked_so_the_trade_is_blocked():
    g = guard(sl_pips=500, atr_price=None, stop_timeframe="H4")
    assert g.blocked and "no H4 ATR" in g.notes[0]


def test_the_rule_can_be_switched_off():
    assert not guard(sl_pips=5, min_stop_atr=0.0).blocked
    assert not guard(sl_pips=5, atr_price=None, min_stop_atr=0.0).blocked      # and then no ATR is needed
    assert not guard(sl_pips=5, min_stop_atr=0.0, stop_timeframe="D1", analysed_timeframes=frozenset()).blocked


def test_modifying_a_stop_is_not_held_to_the_floor():
    # tightening to protect profit is legitimate; the floor is about where a trade is entered
    assert not guard(sl_pips=5, action=MODIFY).blocked


# ---- the decision carries the timeframe ------------------------------------------

OPEN_INPUT = dict(action="open", symbol="EURUSD", direction="buy", risk_pct=2.0, sl_pips=50, tp_pips=100,
                  reason="r", confidence=0.8, setup="x", stop_timeframe="H4")


def test_an_open_must_declare_its_stop_timeframe():
    ok = TradeDecision.from_tool_input(OPEN_INPUT)
    assert ok.stop_timeframe == "H4"
    for missing in ({k: v for k, v in OPEN_INPUT.items() if k != "stop_timeframe"}, {**OPEN_INPUT, "stop_timeframe": ""}):
        with pytest.raises(ValueError, match="stop_timeframe is required"):
            TradeDecision.from_tool_input(missing)


def test_the_timeframe_must_be_a_known_one_and_case_does_not_matter():
    assert TradeDecision.from_tool_input({**OPEN_INPUT, "stop_timeframe": "d1"}).stop_timeframe == "D1"
    for bad in ("M15", "W1", "daily"):
        with pytest.raises(ValueError):
            TradeDecision.from_tool_input({**OPEN_INPUT, "stop_timeframe": bad})


@pytest.mark.parametrize("action", [WAIT, CLOSE, MODIFY])
def test_other_actions_do_not_need_it(action):
    dec = TradeDecision.from_tool_input({**OPEN_INPUT, "action": action, "direction": "", "stop_timeframe": "",
                                         "position_ticket": 7})
    assert dec.stop_timeframe == ""


def test_the_tool_schema_offers_exactly_the_allowed_timeframes():
    from trading.decision import SUBMIT_TRADE_TOOL
    assert SUBMIT_TRADE_TOOL["input_schema"]["properties"]["stop_timeframe"]["enum"] == ["H1", "H4", "D1"]


# ---- measuring the ATR -----------------------------------------------------------


def candles(n, high_low=0.002):
    return [SimpleNamespace(high=1.1 + high_low, low=1.1, close=1.1 + high_low / 2) for _ in range(n)]


class Broker:
    def __init__(self, data):
        self.data, self.asked = data, []

    def get_candles(self, symbol, timeframe, count):
        self.asked.append((symbol, timeframe))
        return self.data


def test_atr_for_reads_the_requested_timeframe():
    b = Broker(candles(60))
    assert atr_for(b, "EURUSD") == pytest.approx(0.002)
    assert atr_for(b, "EURUSD", "D1") == pytest.approx(0.002)
    assert b.asked == [("EURUSD", "H1"), ("EURUSD", "D1")]


def test_atr_for_is_none_without_enough_candles():
    assert atr_for(Broker([]), "EURUSD") is None
    assert atr_for(Broker(candles(5)), "EURUSD") is None


# ---- end to end: a too-tight stop proposed during a scan never becomes an order --


class Agent:
    def __init__(self, decision, looked_at=("H1", "H4", "D1")):
        self._decision, self._looked_at = decision, list(looked_at)

    def decide(self, symbol, account, positions):
        return TradeResult(decision=self._decision, evidence=TradeEvidence(timeframes_used=self._looked_at))


class ScanBroker:
    """Candles differ by timeframe: a 20-pip H1 range and a 100-pip D1 range."""

    def __init__(self, by_timeframe):
        self.by_timeframe, self.orders = by_timeframe, []

    def ensure_connected(self):
        return True

    def ping_ms(self):
        return 50.0

    def get_account(self):
        return AccountState(BALANCE, BALANCE, 0.0, BALANCE, 0.0, 0.0)

    def get_symbol_info(self, symbol):
        return info()

    def get_candles(self, symbol, timeframe, count):
        return self.by_timeframe.get(timeframe, [])

    def calculate_lots(self, *a):
        return 0.1

    def place_order(self, *a):
        self.orders.append(a)
        return 99

    def get_bot_entries(self, since):
        return []

    def get_positions(self):
        return []


@pytest.fixture
def scan(tmp_path):
    s = replace(Settings.from_env(tmp_path / "none.env"), mode="live", state_db=tmp_path / "a.sqlite",
                paper_db=tmp_path / "p.sqlite", lockout_file=tmp_path / "L", kill_switch_file=tmp_path / "STOP",
                blackout_utc="", min_stop_atr=1.0, max_risk_pct=5.0)

    def run(sl_pips, timeframe="H1", by_timeframe=None, looked_at=("H1", "H4", "D1")):
        r = TradingRunner(s)
        r._broker = ScanBroker(by_timeframe if by_timeframe is not None
                               else {"H1": candles(60, 0.002), "D1": candles(60, 0.010)})
        r._daily_pnl = lambda: 0.0
        r._positions = lambda: []
        dec = TradeDecision(OPEN, "EURUSD", "buy", 2.0, sl_pips, sl_pips * 2, "r", 0.9, "trend_continuation",
                            stop_timeframe=timeframe)
        r._agent = Agent(dec, looked_at)
        r.run(["EURUSD"])
        return r
    return run


def notes(r) -> str:
    return r._audit.recent(1)[0]["guardrail_notes"]


def test_a_too_tight_stop_is_blocked_before_any_order_goes_out(scan):
    r = scan(sl_pips=4)                                    # 4 pips vs a 20-pip H1 range
    assert r._broker.orders == [] and "stop of 4 pips" in notes(r)


def test_a_stop_that_meets_the_floor_is_ordered(scan):
    assert len(scan(sl_pips=25)._broker.orders) == 1


def test_the_same_stop_passes_on_h1_and_fails_when_anchored_to_d1(scan):
    assert len(scan(sl_pips=25, timeframe="H1")._broker.orders) == 1             # 25 pips vs 20-pip H1 ATR
    r = scan(sl_pips=25, timeframe="D1")                                          # 25 pips vs 100-pip D1 ATR
    assert r._broker.orders == [] and "D1 ATR" in notes(r)
    assert len(scan(sl_pips=105, timeframe="D1")._broker.orders) == 1             # wide enough for D1


def test_a_stop_anchored_to_a_timeframe_never_analysed_is_blocked(scan):
    r = scan(sl_pips=500, timeframe="D1", looked_at=("H1", "H4"))
    assert r._broker.orders == [] and "no D1 data was looked at" in notes(r)


def test_a_symbol_with_no_candles_cannot_be_traded(scan):
    r = scan(sl_pips=500, by_timeframe={})
    assert r._broker.orders == [] and "no H1 ATR" in notes(r)


def test_the_declared_timeframe_is_stored_with_the_trade(scan):
    r = scan(sl_pips=105, timeframe="D1")
    assert r._audit.recent(1)[0]["stop_timeframe"] == "D1"


# ---- audit: results by stop timeframe --------------------------------------------


def test_results_can_be_grouped_by_stop_timeframe(tmp_path):
    a = TradeAudit(tmp_path / "a.sqlite")
    for ticket, (tf, pnl) in enumerate([("H1", -10.0), ("H1", -5.0), ("D1", 30.0), (None, 4.0)], start=1):
        i = a.record(run_id="r", mode="live", symbol="X", proposed_action="open", final_action="open",
                     position_ticket=ticket, stop_timeframe=tf or "")
        a.record_outcome(i, 1.0, pnl, datetime.now(timezone.utc))
    rows = {r["key"]: (r["closed"], r["pnl"]) for r in a.breakdown("stop_timeframe")}
    assert rows == {"D1": (1, 30.0), "H1": (2, -15.0), "(none)": (1, 4.0)}
