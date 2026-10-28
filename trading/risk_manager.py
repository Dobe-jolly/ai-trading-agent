"""Portfolio Risk Manager — second-pass review of the Analyst's proposed trade basket.

Called once per scan after all symbols have been analysed. The RM receives the full
list of approved OPEN proposals and may veto or resize any of them before execution.
It is a single-turn Claude call, not a multi-turn loop.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from .broker.metatrader import AccountState, Position
from .decision import WAIT, TradeDecision

log = logging.getLogger(__name__)


@dataclass
class RMVerdict:
    symbol: str
    verdict: str        # "approve" | "veto" | "resize"
    new_risk_pct: float
    reason: str

    def apply(self, decision: TradeDecision) -> TradeDecision:
        if self.verdict == "veto":
            return replace(decision, action=WAIT, direction="", sl_pips=0, tp_pips=0)
        if self.verdict == "resize":
            return replace(decision, risk_pct=max(0.1, self.new_risk_pct))
        return decision


class RiskManager:
    """Single-turn Claude review of the full proposed trade basket."""

    def __init__(self, client: Any, model: str, max_tokens: int = 1024) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        prompt_path = Path(__file__).parent / "prompts" / "risk_manager_prompt.md"
        self._system = prompt_path.read_text(encoding="utf-8")

    def review(
        self,
        proposals: list[TradeDecision],
        account: Optional[AccountState],
        existing_positions: list[Position],
    ) -> dict[str, RMVerdict]:
        """Return verdicts keyed by symbol. Approves all if the API call fails."""
        if not proposals:
            return {}

        basket = [
            {
                "symbol": d.symbol,
                "direction": d.direction,
                "risk_pct": d.risk_pct,
                "sl_pips": d.sl_pips,
                "tp_pips": d.tp_pips,
                "confidence": round(d.confidence, 2),
                "setup": d.setup,
                "reason": d.reason,
            }
            for d in proposals
        ]
        acct = account.as_dict() if account else {}
        pos = [p.as_dict() for p in existing_positions]

        user_msg = (
            f"## Proposed trade basket ({len(proposals)} trades)\n\n"
            f"{json.dumps(basket, indent=2)}\n\n"
            f"## Account state\n{json.dumps(acct, indent=2)}\n\n"
            f"## Existing open positions\n"
            f"{json.dumps(pos, indent=2) if pos else 'None'}\n\n"
            "Review the basket and call review_basket with your verdicts."
        )

        response = None
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=self._system,
                    tools=[_REVIEW_BASKET_TOOL],
                    tool_choice={"type": "any"},
                    messages=[{"role": "user", "content": user_msg}],
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                is_rate_limit = "429" in str(exc) or "rate_limit" in str(exc).lower()
                wait = 90 if is_rate_limit else 15
                log.warning("RM attempt %d failed (%s); retrying in %ds", attempt + 1, exc, wait)
                if attempt < 2:
                    time.sleep(wait)
        if response is None:
            log.warning("Risk manager unavailable after 3 attempts (%s); vetoing all proposals", last_exc)
            return _veto_all(proposals, f"RM unavailable: {last_exc}")

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "review_basket":
                return _parse_verdicts(block.input, proposals)

        log.warning("Risk manager did not call review_basket; vetoing all")
        return _veto_all(proposals, "RM did not respond")


# ---- helpers -------------------------------------------------------------------------

def _approve_all(proposals: list[TradeDecision]) -> dict[str, RMVerdict]:
    return {d.symbol: RMVerdict(d.symbol, "approve", d.risk_pct, "no review") for d in proposals}


def _veto_all(proposals: list[TradeDecision], reason: str) -> dict[str, RMVerdict]:
    return {d.symbol: RMVerdict(d.symbol, "veto", 0.0, reason) for d in proposals}


def _parse_verdicts(inp: dict, proposals: list[TradeDecision]) -> dict[str, RMVerdict]:
    known = {d.symbol: d for d in proposals}
    result: dict[str, RMVerdict] = {}

    for item in inp.get("verdicts", []):
        sym = str(item.get("symbol", ""))
        verdict = str(item.get("verdict", "approve")).lower()
        if verdict not in ("approve", "veto", "resize"):
            verdict = "approve"
        orig = known.get(sym)
        new_risk = float(item.get("new_risk_pct") or (orig.risk_pct if orig else 1.0))
        reason = str(item.get("reason", ""))
        result[sym] = RMVerdict(sym, verdict, new_risk, reason)
        if verdict != "approve":
            log.info("RM %-28s %s%s  — %s", sym, verdict,
                     f" → {new_risk:.1f}%" if verdict == "resize" else "",
                     reason[:80])

    # Ensure every proposal has a verdict (fill missing with approve)
    for d in proposals:
        if d.symbol not in result:
            result[d.symbol] = RMVerdict(d.symbol, "approve", d.risk_pct, "not reviewed")

    return result


# ---- tool schema ---------------------------------------------------------------------

_REVIEW_BASKET_TOOL: dict = {
    "name": "review_basket",
    "description": "Submit your verdicts for the proposed trade basket. Include a verdict for every symbol.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "description": "One entry per proposed trade.",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "verdict": {
                            "type": "string",
                            "enum": ["approve", "veto", "resize"],
                            "description": "approve: trade stands. veto: block it. resize: approve with reduced risk_pct.",
                        },
                        "new_risk_pct": {
                            "type": "number",
                            "description": "Required when verdict is 'resize'. New risk percentage (e.g. 0.75).",
                        },
                        "reason": {
                            "type": "string",
                            "description": "One sentence explaining the verdict.",
                        },
                    },
                    "required": ["symbol", "verdict", "reason"],
                },
            },
            "summary": {
                "type": "string",
                "description": "One sentence overall assessment of the basket.",
            },
        },
        "required": ["verdicts"],
    },
}
