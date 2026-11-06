"""TradeDecision dataclass and the submit_trade tool definition Claude calls to end each session."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .chart_reads import CHART_READ_SCHEMA, clean_read

OPEN = "open"
CLOSE = "close"
MODIFY = "modify"
WAIT = "wait"
VALID_ACTIONS = (OPEN, CLOSE, MODIFY, WAIT)
VALID_DIRECTIONS = ("buy", "sell", "")
# Timeframes a stop can be anchored to; the stop must be at least one ATR of that timeframe.
STOP_TIMEFRAMES = ("H1", "H4", "D1")


@dataclass
class TradeDecision:
    action: str                          # open | close | modify | wait
    symbol: str
    direction: str                       # buy | sell | "" (for close/wait)
    risk_pct: float                      # % of account balance to risk (used only for open)
    sl_pips: int                         # stop-loss distance in pips
    tp_pips: int                         # take-profit distance in pips
    reason: str                          # internal reasoning, 2–5 sentences
    confidence: float                    # 0.0–1.0
    setup: str                           # e.g. trend_continuation, breakout, reversal, range, wait
    evidence: list[str] = field(default_factory=list)
    position_ticket: Optional[int] = None  # required for close / modify
    stop_timeframe: str = ""             # H1 | H4 | D1: whose structure the stop is anchored to (required for open)
    chart_read: Optional[dict] = None    # the model's structured map of the chart (see chart_reads.py); never blocks a decision

    @classmethod
    def fallback(cls, reason: str) -> "TradeDecision":
        return cls(
            action=WAIT, symbol="", direction="", risk_pct=0.0,
            sl_pips=0, tp_pips=0, reason=reason, confidence=0.0, setup="fallback",
        )

    @classmethod
    def from_tool_input(cls, inp: dict) -> "TradeDecision":
        action = str(inp.get("action", WAIT)).lower()
        if action not in VALID_ACTIONS:
            raise ValueError(f"action must be one of {VALID_ACTIONS}, got {action!r}")
        direction = str(inp.get("direction", "")).lower()
        if direction not in VALID_DIRECTIONS:
            raise ValueError(f"direction must be 'buy', 'sell' or '' for close/wait")
        if action == OPEN and not direction:
            raise ValueError("direction is required when action is 'open'")
        confidence = float(inp.get("confidence", 0.0))
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        risk_pct = float(inp.get("risk_pct", 1.0))
        if risk_pct <= 0 or risk_pct > 10:
            raise ValueError("risk_pct must be between 0.1 and 10")
        stop_timeframe = str(inp.get("stop_timeframe") or "").upper()
        if action == OPEN and stop_timeframe not in STOP_TIMEFRAMES:
            raise ValueError(
                f"stop_timeframe is required when action is 'open' and must be one of {STOP_TIMEFRAMES}: "
                "the timeframe whose structure your stop-loss is anchored to"
            )
        if stop_timeframe and stop_timeframe not in STOP_TIMEFRAMES:
            raise ValueError(f"stop_timeframe must be one of {STOP_TIMEFRAMES}, got {stop_timeframe!r}")
        evidence = inp.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [e.strip() for e in evidence.split("\n") if e.strip()]
        return cls(
            action=action,
            symbol=str(inp.get("symbol", "")),
            direction=direction,
            risk_pct=risk_pct,
            sl_pips=int(inp.get("sl_pips", 0)),
            tp_pips=int(inp.get("tp_pips", 0)),
            reason=str(inp.get("reason", "")),
            confidence=confidence,
            setup=str(inp.get("setup", "unknown")),
            evidence=list(evidence),
            position_ticket=int(inp["position_ticket"]) if inp.get("position_ticket") else None,
            stop_timeframe=stop_timeframe,
            chart_read=clean_read(inp.get("chart_read")),
        )


SUBMIT_TRADE_TOOL: dict = {
    "name": "submit_trade",
    "description": (
        "Submit your trading decision for this symbol. Call this once you have gathered "
        "enough market data and are ready to act. If conditions are not right, use action='wait'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["open", "close", "modify", "wait"],
                "description": (
                    "open: enter a new position. "
                    "close: exit an existing position (set position_ticket). "
                    "modify: move SL/TP on an existing position (set position_ticket). "
                    "wait: no action this scan."
                ),
            },
            "symbol": {
                "type": "string",
                "description": "Trading symbol exactly as shown, e.g. EURUSD, XAUUSD, Volatility 75 Index.",
            },
            "direction": {
                "type": "string",
                "enum": ["buy", "sell", ""],
                "description": "buy or sell for 'open'; leave empty for close/modify/wait.",
            },
            "risk_pct": {
                "type": "number",
                "description": "Percentage of account balance to risk on this trade (0.1–2.0 recommended). Only used for 'open'.",
            },
            "sl_pips": {
                "type": "integer",
                "description": "Stop-loss distance from entry in pips. Required for open; used as new SL for modify.",
            },
            "tp_pips": {
                "type": "integer",
                "description": "Take-profit distance from entry in pips. Required for open; used as new TP for modify.",
            },
            "stop_timeframe": {
                "type": "string",
                "enum": list(STOP_TIMEFRAMES),
                "description": (
                    "Required for 'open'. The timeframe whose structure your stop-loss is anchored to "
                    "(e.g. H4 if the stop sits beyond an H4 swing, D1 if beyond a D1 level). The code "
                    "requires the stop to be at least 1x that timeframe's ATR, and you must have looked "
                    "at that timeframe in this session. A slower timeframe means a wider stop."
                ),
            },
            "position_ticket": {
                "type": "integer",
                "description": "Ticket number of the position to close or modify. Required for close/modify.",
            },
            "reason": {
                "type": "string",
                "description": "2–5 sentences explaining your reasoning. Mention the key signals and why this setup is valid now.",
            },
            "confidence": {
                "type": "number",
                "description": "Your confidence in this decision, 0.0 (very uncertain) to 1.0 (very confident).",
            },
            "setup": {
                "type": "string",
                "description": (
                    "Short label for the trade setup: trend_continuation, breakout, reversal, "
                    "range_bounce, scalp, news_play, take_profit, stop_loss_hit, wait."
                ),
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key facts from your lookups that support this decision (3–6 bullet points).",
            },
            "chart_read": CHART_READ_SCHEMA,
        },
        "required": ["action", "symbol", "direction", "risk_pct", "sl_pips", "tp_pips", "reason", "confidence", "setup"],
    },
}
