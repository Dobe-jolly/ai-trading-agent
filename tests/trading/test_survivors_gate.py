"""Fix 1: free-form opens are hard-blocked until a registered setup is on the survivors allowlist."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading.broker.metatrader import AccountState, Position, SymbolInfo
from trading.decision import CLOSE, MODIFY, OPEN, WAIT, TradeDecision
from trading.guardrails import apply_guardrails
from trading import survivors


BALANCE = 10_000.0


def account() -> AccountState:
    return AccountState(BALANCE, BALANCE, 0.0, BALANCE, 0.0, 0.0)


def eurusd() -> SymbolInfo:
    return SymbolInfo("EURUSD", 5, 0.00001, 0.00001, 1.0, 100000, 0.01, 100.0, 0.01, 10, 1.0850, 1.0851)


def open_dec(playbook: str = "", symbol: str = "EURUSD") -> TradeDecision:
    return TradeDecision(
        OPEN, symbol, "buy", 1.0, 50, 100, "r", 0.9, "trend_continuation",
        stop_timeframe="H1", playbook=playbook,
    )


def guard(dec: TradeDecision, *, require: bool = True, positions=None):
    return apply_guardrails(
        dec, account(), positions or [], eurusd(),
        max_risk_pct=5.0, max_positions=5, daily_loss_limit_pct=60.0,
        spread_max_pips=5.0, confidence_threshold=0.72,
        require_approved_setup=require,
        atr_price=0.0010, min_stop_atr=1.0,
        analysed_timeframes=frozenset({"H1"}),
    )


def test_allowlist_is_empty_until_a_setup_passes():
    assert survivors.APPROVED == ()
    assert survivors.summarize_approved().startswith("(none")


def test_freeform_open_is_hard_blocked_when_gate_on_and_no_survivors():
    g = guard(open_dec(""))
    assert g.blocked and g.final.action == WAIT
    assert any("no approved setups" in n for n in g.notes)


def test_named_but_unapproved_playbook_is_still_blocked_when_list_empty():
    g = guard(open_dec("alpha"))
    assert g.blocked and g.final.action == WAIT
    assert any("no approved setups" in n for n in g.notes)


def test_gate_off_allows_open_for_other_rule_tests():
    g = guard(open_dec(""), require=False)
    assert not g.blocked and g.final.action == OPEN


def test_close_and_modify_still_work_while_opens_are_halted():
    held = Position(7, "EURUSD", "buy", 0.01, 1.0850, 1.0800, 1.0950, 0.0, datetime.now(timezone.utc))
    for action in (CLOSE, MODIFY):
        dec = TradeDecision(action, "EURUSD", "", 1.0, 40, 80, "r", 0.9, "x", position_ticket=7)
        g = guard(dec, require=True, positions=[held])
        assert not g.blocked, action


def test_wait_is_unchanged_by_the_gate():
    dec = TradeDecision(WAIT, "EURUSD", "", 0.0, 0, 0, "no edge", 0.5, "wait")
    g = guard(dec, require=True)
    assert not g.blocked and g.final.action == WAIT


def test_approved_playbook_for_matching_class_is_allowed(monkeypatch):
    monkeypatch.setattr(
        survivors, "APPROVED",
        (survivors.ApprovedSetup("alpha", "forex"),),
    )
    g = guard(open_dec("alpha"))
    assert not g.blocked and g.final.action == OPEN


def test_approved_playbook_wrong_asset_class_is_blocked(monkeypatch):
    monkeypatch.setattr(
        survivors, "APPROVED",
        (survivors.ApprovedSetup("alpha", "metals"),),
    )
    g = guard(open_dec("alpha", symbol="EURUSD"))
    assert g.blocked and any("not approved for forex" in n for n in g.notes)


def test_empty_playbook_blocked_even_when_survivors_exist(monkeypatch):
    monkeypatch.setattr(
        survivors, "APPROVED",
        (survivors.ApprovedSetup("alpha", "forex"),),
    )
    g = guard(open_dec(""))
    assert g.blocked and any("requires playbook" in n for n in g.notes)


def test_open_allowed_helper_matches_guardrail_messages():
    ok, reason = survivors.open_allowed("", "EURUSD")
    assert not ok and "no approved setups" in reason
