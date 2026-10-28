"""Code-enforced risk rules applied between the model's decision and any order execution.

The model proposes; this module decides what is allowed. Rules here cannot be
overridden by prompt engineering — they are structural checks on the numbers.

Rule hierarchy (checked in order):
  1. Daily loss circuit breaker — stops all trading when the day's loss exceeds the limit
  2. Max open positions — prevents over-exposure
  3. Action-specific rules for OPEN:
     0. One open position per symbol
     a. Spread too wide
     b. SL or TP missing / zero, or the stop closer than min_stop_atr x the ATR of the
        timeframe it is anchored to (which must be one the model analysed)
     c. Risk/reward ratio below minimum
     d. Risk percentage too high
     e. Confidence below threshold
     g. Total open risk across all positions above the global cap
  4. Action-specific rules for CLOSE / MODIFY:
     a. Position ticket must be provided and must be open
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable, Optional

from .broker.metatrader import MIN_LOT_RISK_TOLERANCE, AccountState, Position, SymbolInfo
from .decision import CLOSE, MODIFY, OPEN, WAIT, TradeDecision
from .indicators import atr

MIN_RR_RATIO = 1.0          # minimum risk:reward (TP/SL pips)
MIN_SL_PIPS = 3             # smallest acceptable stop-loss (global)
MIN_RISK_PCT = 0.1          # below this much room under the global cap, the trade is blocked
ATR_BARS = 60               # H1 candles fetched to measure ATR (14-period; the rest settles the smoothing)
ATR_FLOOR_SLACK = 0.98      # the model rounds its stop to whole pips, so a stop 2% under the floor still passes


@dataclass
class GuardrailResult:
    proposed: TradeDecision
    final: TradeDecision
    notes: list[str]
    blocked: bool = False    # True when a trade was stopped; final.action becomes WAIT

    @property
    def changed(self) -> bool:
        return self.proposed.action != self.final.action or bool(self.notes)


def atr_for(broker, symbol: str, timeframe: str = "H1") -> Optional[float]:
    """The symbol's ATR on `timeframe` in price units, or None when there are too few candles to say."""
    candles = broker.get_candles(symbol, timeframe, ATR_BARS)
    if not candles:
        return None
    return atr([c.high for c in candles], [c.low for c in candles], [c.close for c in candles]) or None


def open_risk_pct(
    positions: list[Position],
    balance: float,
    info_for: Callable[[str], Optional[SymbolInfo]],
    fallback_pct: float,
) -> float:
    """Total % of balance at risk across open positions, measured to each stop-loss.

    A position with no stop, or whose symbol info is unavailable, is counted at
    `fallback_pct` so unknown risk is never treated as zero.
    """
    if balance <= 0:
        return 0.0
    total = 0.0
    for p in positions:
        info = info_for(p.symbol)
        if p.sl <= 0 or info is None or info.pip_size <= 0 or info.sizing_pip_value <= 0:
            total += fallback_pct
            continue
        pips_to_stop = abs(p.open_price - p.sl) / info.pip_size
        total += p.lots * pips_to_stop * info.sizing_pip_value / balance * 100.0
    return total


def _block(decision: TradeDecision, notes: list[str], note: str) -> GuardrailResult:
    notes.append(note)
    final = replace(decision, action=WAIT, direction="", sl_pips=0, tp_pips=0)
    return GuardrailResult(decision, final, notes, blocked=True)


HARD_POSITION_CEILING = 30       # sanity bound above whatever max_positions is set to
SYNTHETIC_MIN_CONFIDENCE = 0.78  # vol/boom/crash indices need higher conviction
MAX_SAME_DIRECTION = 2           # max correlated forex positions in same direction
MAX_NEW_OPENS_PER_SCAN = 3       # default for new positions approved in one scan across all symbols

_SYNTHETIC_SYMBOLS = frozenset({
    "Volatility 10 Index", "Volatility 25 Index", "Volatility 50 Index",
    "Volatility 75 Index", "Volatility 100 Index",
    "Volatility 10 (1s) Index", "Volatility 25 (1s) Index", "Volatility 50 (1s) Index",
    "Volatility 75 (1s) Index", "Volatility 100 (1s) Index",
    "Step Index",
    "Boom 300 Index", "Boom 500 Index", "Boom 1000 Index",
    "Crash 300 Index", "Crash 500 Index", "Crash 1000 Index",
})

_FOREX_SYMBOLS = frozenset({
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD",
    "NZDUSD", "USDCAD", "EURJPY", "EURGBP",
})


def apply_guardrails(
    decision: TradeDecision,
    account: Optional[AccountState],
    positions: list[Position],
    symbol_info: Optional[SymbolInfo],
    *,
    max_risk_pct: float,
    max_positions: int,
    daily_loss_limit_pct: float,
    spread_max_pips: float,
    synthetic_spread_max_pips: float = 10000.0,
    confidence_threshold: float,
    daily_pnl: float = 0.0,
    open_risk_pct: float = 0.0,
    max_total_risk_pct: float = 0.0,
    opens_this_scan: int = 0,
    halted: Optional[str] = None,
    max_new_opens: int = MAX_NEW_OPENS_PER_SCAN,
    max_same_direction: int = 0,
    atr_price: Optional[float] = None,
    min_stop_atr: float = 0.0,
    analysed_timeframes: Optional[frozenset] = None,
) -> GuardrailResult:
    notes: list[str] = []

    # WAIT / CLOSE / MODIFY pass through most checks unchanged
    if decision.action == WAIT:
        return GuardrailResult(decision, decision, notes)

    # 1. Daily loss circuit breaker (applies to any new open)
    if account and daily_loss_limit_pct > 0:
        limit = account.balance * (daily_loss_limit_pct / 100.0)
        if daily_pnl <= -limit:
            return _block(
                decision, notes,
                f"daily loss circuit breaker: today's P&L is {daily_pnl:.2f}, "
                f"limit is -{limit:.2f} ({daily_loss_limit_pct}% of balance). No new trades today.",
            )

    if decision.action == CLOSE:
        if not decision.position_ticket:
            return _block(decision, notes, "close requires position_ticket")
        open_tickets = {p.ticket for p in positions}
        if decision.position_ticket not in open_tickets:
            return _block(decision, notes,
                          f"position {decision.position_ticket} is not in the open positions list")
        return GuardrailResult(decision, decision, notes)

    if decision.action == MODIFY:
        if not decision.position_ticket:
            return _block(decision, notes, "modify requires position_ticket")
        open_tickets = {p.ticket for p in positions}
        if decision.position_ticket not in open_tickets:
            return _block(decision, notes,
                          f"position {decision.position_ticket} is not in the open positions list")
        if decision.sl_pips < MIN_SL_PIPS and decision.sl_pips != 0:
            return _block(decision, notes,
                          f"modified SL of {decision.sl_pips} pips is below the minimum {MIN_SL_PIPS} pips")
        return GuardrailResult(decision, decision, notes)

    # ---- OPEN-specific checks ---------------------------------------------------

    # 1b. Circuit breaker (blackout hours, broker latency) — new opens only
    if halted:
        return _block(decision, notes, f"trading halted: {halted}")

    # 2. Max open positions (hard cap + config cap)
    if len(positions) >= HARD_POSITION_CEILING:
        return _block(
            decision, notes,
            f"total open positions ({len(positions)}) at the hard ceiling ({HARD_POSITION_CEILING}); "
            "wait for a position to close before opening a new trade",
        )
    if len(positions) >= max_positions:
        return _block(
            decision, notes,
            f"max open positions ({max_positions}) already reached; "
            "wait for one to close before opening a new trade",
        )

    # 2-dup. One position per symbol — a second trade on the same symbol is the same bet again.
    # `positions` includes opens approved earlier in this scan, so same-scan repeats are caught too.
    if any(p.symbol == decision.symbol for p in positions):
        return _block(
            decision, notes,
            f"{decision.symbol} already has an open position; manage it with close or modify "
            "instead of opening another",
        )

    # 2-scan. Cap on new opens per scan — one thesis across many symbols is one bet repeated
    if opens_this_scan >= max_new_opens:
        return _block(
            decision, notes,
            f"{opens_this_scan} positions already approved this scan (max {max_new_opens}); "
            "wait for the next scan",
        )

    # 2-dir. Cap on how many positions across ALL symbols may lean the same way. Many trades on
    # different symbols can still be one directional bet (a dollar-up move hits most forex, metals
    # and indices at once), so this is what keeps a larger book from being a single trade.
    if max_same_direction > 0:
        leaning = sum(1 for p in positions if p.direction == decision.direction)
        if leaning >= max_same_direction:
            return _block(
                decision, notes,
                f"already {leaning} open {decision.direction} positions (max {max_same_direction} "
                "in one direction); wait for one to close or open the other way",
            )

    # 2a. Correlated forex exposure — no more than MAX_SAME_DIRECTION in same direction
    if decision.symbol in _FOREX_SYMBOLS:
        same_dir_forex = sum(
            1 for p in positions
            if p.symbol in _FOREX_SYMBOLS and p.direction == decision.direction
        )
        if same_dir_forex >= MAX_SAME_DIRECTION:
            return _block(
                decision, notes,
                f"already have {same_dir_forex} {decision.direction} forex positions "
                f"(max {MAX_SAME_DIRECTION}); correlated exposure too high",
            )

    # 2b. Synthetic index minimum confidence
    if decision.symbol in _SYNTHETIC_SYMBOLS and decision.confidence < SYNTHETIC_MIN_CONFIDENCE:
        return _block(
            decision, notes,
            f"confidence {decision.confidence:.0%} is below the {SYNTHETIC_MIN_CONFIDENCE:.0%} "
            f"minimum required for synthetic index {decision.symbol}; "
            "these instruments have unpredictable spike behaviour — wait for clearer confluence",
        )

    # 3a. Spread check — synthetics (Deriv "Index" instruments) have naturally wide spreads
    if symbol_info is not None:
        spread = symbol_info.spread_pips()
        is_synthetic = "Index" in decision.symbol
        limit = synthetic_spread_max_pips if is_synthetic else spread_max_pips
        if spread > limit:
            return _block(
                decision, notes,
                f"spread is {spread:.1f} pips, above the maximum {limit:.1f} pips; "
                "wait for the spread to tighten",
            )

    # 3b. SL / TP must be set
    if decision.sl_pips < MIN_SL_PIPS:
        return _block(decision, notes,
                      f"sl_pips={decision.sl_pips} is below the minimum {MIN_SL_PIPS} pips; "
                      "set a proper stop-loss")

    # 3b-atr. The stop must be at least `min_stop_atr` × the ATR of the timeframe it is anchored to
    # (`stop_timeframe`, declared by the model), or ordinary volatility sweeps it. The ATR is measured
    # live for every symbol, so there is no table to go stale. A D1 level needs a D1-sized stop; an
    # H1 floor alone would let a D1-based trade through with a stop a fifth of the right size.
    if min_stop_atr > 0 and symbol_info is not None and symbol_info.pip_size > 0:
        tf = decision.stop_timeframe or "H1"
        if analysed_timeframes is not None and tf not in analysed_timeframes:
            return _block(
                decision, notes,
                f"the stop is anchored to {tf} but no {tf} data was looked at in this session "
                f"(looked at: {', '.join(sorted(analysed_timeframes)) or 'nothing'}); analyse {tf} first "
                "or anchor the stop to a timeframe you did analyse",
            )
        if not atr_price or atr_price <= 0:
            return _block(decision, notes,
                          f"no {tf} ATR is available for this symbol, so the stop distance can't be checked")
        floor = min_stop_atr * atr_price * ATR_FLOOR_SLACK
        if decision.sl_pips * symbol_info.pip_size < floor:
            have = decision.sl_pips * symbol_info.pip_size / atr_price
            return _block(
                decision, notes,
                f"stop of {decision.sl_pips:,} pips is {have:.2f}× the {tf} ATR ({atr_price:.6g}); it must be at "
                f"least {min_stop_atr:g}× ({math.ceil(floor / symbol_info.pip_size):,} pips); widen the stop-loss "
                "or anchor it to a faster timeframe if that is where the level really is",
            )

    if decision.tp_pips <= 0:
        return _block(decision, notes, "tp_pips must be greater than 0; set a take-profit")

    # 3c. Risk/reward ratio
    rr = decision.tp_pips / decision.sl_pips
    if rr < MIN_RR_RATIO:
        return _block(
            decision, notes,
            f"risk/reward ratio is {rr:.2f} (TP={decision.tp_pips} / SL={decision.sl_pips}); "
            f"minimum is {MIN_RR_RATIO:.1f}:1",
        )

    # 3d. Risk percentage — hard ceiling from config
    if decision.risk_pct > max_risk_pct:
        notes.append(
            f"risk_pct {decision.risk_pct}% exceeds max {max_risk_pct}%; capped to {max_risk_pct}%"
        )
        decision = replace(decision, risk_pct=max_risk_pct)

    # 3e. Confidence — block below threshold; scale risk down for borderline confidence
    if decision.confidence < confidence_threshold:
        return _block(
            decision, notes,
            f"confidence {decision.confidence:.0%} is below threshold {confidence_threshold:.0%}; "
            "wait for a higher-confidence setup",
        )

    # 3f. Confidence-based risk scaling
    #   65–74%  → cap at 5%   (borderline setup, small size)
    #   75–84%  → cap at 10%  (clear setup, normal size)
    #   85%+    → cap at 20%  (strong multi-tf confluence, size up)
    if decision.confidence < 0.75:
        risk_cap = 5.0
    elif decision.confidence < 0.85:
        risk_cap = 10.0
    else:
        risk_cap = 20.0
    risk_cap = min(risk_cap, max_risk_pct)
    if decision.risk_pct > risk_cap:
        notes.append(
            f"risk_pct {decision.risk_pct}% capped to {risk_cap}% for confidence {decision.confidence:.0%}"
        )
        decision = replace(decision, risk_pct=risk_cap)

    # 3g. Global open-risk cap — total risk across all open trades, not just this one.
    #   Trims the new trade to the room left; blocks when there is almost none.
    if max_total_risk_pct > 0:
        room = max_total_risk_pct - open_risk_pct
        if room < MIN_RISK_PCT:
            return _block(
                decision, notes,
                f"open risk is {open_risk_pct:.1f}% of balance, at the {max_total_risk_pct:.1f}% "
                "global cap; wait for a position to close or its stop to move",
            )
        if decision.risk_pct > room:
            notes.append(
                f"risk_pct {decision.risk_pct}% trimmed to {room:.1f}% "
                f"(open risk {open_risk_pct:.1f}% of {max_total_risk_pct:.1f}% global cap)"
            )
            decision = replace(decision, risk_pct=round(room, 2))

    # 3h. Minimum-lot affordability, judged on the FINAL risk (after every cap above). If even the
    # broker's smallest lot would risk well over what that allows, the symbol is too big for the
    # account and a "2% trade" would really be a much larger one.
    if symbol_info is not None and account and decision.sl_pips > 0 and symbol_info.sizing_pip_value > 0:
        intended_risk = account.balance * (decision.risk_pct / 100.0)
        min_lot_risk = symbol_info.volume_min * decision.sl_pips * symbol_info.sizing_pip_value
        if min_lot_risk > intended_risk * MIN_LOT_RISK_TOLERANCE:
            return _block(
                decision, notes,
                f"minimum lot ({symbol_info.volume_min}) would risk ${min_lot_risk:.2f} against the "
                f"${intended_risk:.2f} this trade is allowed ({decision.risk_pct}%); "
                "symbol too large for the account at this risk",
            )

    return GuardrailResult(decision, decision, notes)
