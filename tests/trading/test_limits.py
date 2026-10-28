from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from trading.broker.metatrader import AccountState, Position, SymbolInfo
from trading.config import BASE_DIR, Settings
from trading.decision import OPEN, TradeDecision
from trading.guardrails import HARD_POSITION_CEILING, apply_guardrails
from trading.prompt import render_prompt, tokens

PROMPT = BASE_DIR / "trading" / "prompts" / "system_prompt.md"


def settings(tmp_path, **over) -> Settings:
    return replace(Settings.from_env(tmp_path / "none.env"), **over)


def info() -> SymbolInfo:
    return SymbolInfo("EURUSD", 5, 0.00001, 0.00001, 1.0, 100000, 0.01, 100.0, 0.01, 10, 1.0850, 1.0851)


def held(symbol: str, direction: str, ticket: int) -> Position:
    return Position(ticket, symbol, direction, 0.01, 1.0, 0.9, 1.1, 0.0, datetime.now(timezone.utc))


def guard(symbol="GBPUSD", direction="sell", positions=(), **kw):
    dec = TradeDecision(OPEN, symbol, direction, 2.0, 50, 100, "r", 0.9, "trend_continuation")
    base = dict(max_risk_pct=5.0, max_positions=12, daily_loss_limit_pct=60.0,
                spread_max_pips=5.0, confidence_threshold=0.72)
    return apply_guardrails(dec, AccountState(10000.0, 10000.0, 0.0, 10000.0, 0.0, 0.0),
                            list(positions), info(), **{**base, **kw})


# Synthetic indices are exempt from the forex-only correlation rule, so they make clean fillers.
def book(n, direction="sell"):
    return [held(f"Volatility {i} Index", direction, i) for i in range(n)]


# ---- position limits are settings, not constants ---------------------------------


def test_more_than_five_positions_are_allowed_when_the_setting_says_so():
    assert not guard(positions=book(5), max_positions=12).blocked
    assert not guard(positions=book(11), max_positions=12).blocked


def test_the_position_setting_is_the_limit_it_applies():
    assert guard(positions=book(12), max_positions=12).blocked
    assert guard(positions=book(5), max_positions=5).blocked


def test_a_sanity_ceiling_still_sits_above_the_setting():
    g = guard(positions=book(HARD_POSITION_CEILING), max_positions=HARD_POSITION_CEILING + 10)
    assert g.blocked and "ceiling" in g.notes[0]


def test_the_per_scan_limit_is_a_setting():
    assert guard(opens_this_scan=3).blocked                       # default 3
    assert not guard(opens_this_scan=3, max_new_opens=6).blocked
    assert guard(opens_this_scan=6, max_new_opens=6).blocked


# ---- same-direction cap ----------------------------------------------------------


def test_the_same_direction_cap_counts_across_all_symbols():
    sells = book(3, "sell")
    assert guard(direction="sell", positions=sells, max_same_direction=3).blocked
    assert not guard(direction="buy", positions=sells, max_same_direction=3).blocked    # the other way is open
    assert not guard(direction="sell", positions=sells, max_same_direction=4).blocked


def test_the_same_direction_cap_is_off_at_zero():
    assert not guard(direction="sell", positions=book(9, "sell"), max_same_direction=0).blocked


def test_a_blocked_trade_names_the_reason():
    g = guard(direction="sell", positions=book(3, "sell"), max_same_direction=3)
    assert "3 open sell positions" in g.notes[0]


# ---- config validation -----------------------------------------------------------


def test_bad_limits_are_reported(tmp_path):
    s = settings(tmp_path, max_positions=0, max_new_opens_per_scan=0, max_same_direction=-1)
    problems = " ".join(s.problems())
    assert "MAX_POSITIONS" in problems and "MAX_NEW_OPENS_PER_SCAN" in problems and "MAX_SAME_DIRECTION" in problems


def test_default_limits_match_the_original_behaviour(tmp_path):
    s = settings(tmp_path)
    assert (s.max_positions, s.max_new_opens_per_scan) == (5, 3)


# ---- the prompt follows the settings ---------------------------------------------


def test_every_token_in_the_prompt_file_is_known_and_filled(tmp_path):
    text = PROMPT.read_text(encoding="utf-8")
    assert render_prompt(text, settings(tmp_path)).count("{{") == 0
    assert set(re.findall(r"\{\{(\w+)\}\}", text)) <= set(tokens(settings(tmp_path)))


def test_the_prompt_states_the_limits_the_settings_enforce(tmp_path):
    s = settings(tmp_path, max_positions=12, max_new_opens_per_scan=6, max_same_direction=8,
                 max_synthetic_positions=7, max_risk_pct=2.0)
    text = render_prompt(PROMPT.read_text(encoding="utf-8"), s)
    for line in ("Maximum 12 simultaneous positions", "Maximum 6 new opens per scan",
                 "Maximum 8 open positions leaning the same way", "Maximum 2% of balance risked per trade",
                 "Maximum 7 synthetic index positions"):
        assert line in text
    assert "Maximum 5 simultaneous positions" not in text and "3 new opens" not in text


def test_the_prompt_renders_back_to_the_original_numbers_at_the_defaults(tmp_path):
    text = render_prompt(PROMPT.read_text(encoding="utf-8"), settings(tmp_path))
    assert "Maximum 5 simultaneous positions" in text and "Maximum 3 new opens per scan" in text


def test_an_unknown_token_is_an_error_not_a_silent_blank(tmp_path):
    with pytest.raises(ValueError, match="TYPO"):
        render_prompt("limit is {{TYPO}}", settings(tmp_path))
