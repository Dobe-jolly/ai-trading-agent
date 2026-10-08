"""The structured decision the model must return, via the submit_decision tool (shared by both agents)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

REPLY_AND_CLOSE = "reply_and_close"
REQUEST_INFO = "request_info"
ESCALATE = "escalate"
NO_ACTION = "no_action"
ACTIONS = (REPLY_AND_CLOSE, REQUEST_INFO, ESCALATE, NO_ACTION)
REPLY_ACTIONS = (REPLY_AND_CLOSE, REQUEST_INFO)

def submit_decision_tool(*, close_help: str, ask_help: str, category_help: str, standard_help: str) -> dict[str, Any]:
    """The submit_decision tool, worded for one agent (what its lookups are, what a category looks like)."""
    return {
        "name": "submit_decision",
        "description": (
            "Submit your final decision for this ticket. Call it exactly once, after you have "
            "gathered the evidence you need. Calling it ends your work on the ticket."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(ACTIONS),
                    "description": (
                        f"reply_and_close: {close_help}; post the reply and close. "
                        f"request_info: {ask_help}; post a reply asking for it and keep the ticket open. "
                        "escalate: a person must handle it; an internal note with your findings is posted. "
                        "no_action: nothing to do yet."
                    ),
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1,
                               "description": "How sure you are that this action is correct, 0 to 1."},
                "category": {"type": "string", "description": category_help},
                "reason": {"type": "string", "description": (
                    "Internal only (goes in the audit log and escalation notes). 2-5 sentences: what you checked and "
                    "why this action.")},
                "evidence": {"type": "array", "items": {"type": "string"},
                             "description": "Facts from tool results that support the decision, one per item."},
                "reply_text": {"type": "string", "description": (
                    "The reply posted on the ticket (read by the branch/service-desk staff who raised it). Required for "
                    "reply_and_close and request_info, empty otherwise. Masked values like <NUMBER_1> may be used; they "
                    "are filled in before posting.")},
                "follows_standard_outcome": {"type": "boolean", "description": (
                    f"{standard_help} Replies marked false wait for a person to approve them.")},
            },
            "required": ["action", "confidence", "category", "reason", "evidence", "follows_standard_outcome"],
        },
    }


SUBMIT_DECISION_TOOL = submit_decision_tool(
    close_help="the lookups explain the issue and the reply tells the branch what to do",
    ask_help="a specific detail is missing or contradictory",
    category_help="The main finding behind the decision.",
    standard_help="true when the action and reply follow the standard outcome; false when you reasoned beyond it.",
)


@dataclass
class Decision:
    action: str
    confidence: float
    category: str
    reason: str
    evidence: list[str] = field(default_factory=list)
    reply_text: str = ""
    standard: Optional[bool] = None  # the model's own view: does this follow the standard outcome?

    @classmethod
    def from_tool_input(cls, data: dict[str, Any]) -> "Decision":
        """Parse the model's submit_decision input. Raises ValueError if it is unusable."""
        if not isinstance(data, dict):
            raise ValueError("Decision must be an object")
        action = str(data.get("action", "")).strip()
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
        try:
            confidence = float(data.get("confidence"))
        except (TypeError, ValueError):
            raise ValueError("confidence must be a number between 0 and 1") from None
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        reason = str(data.get("reason", "")).strip()
        if not reason:
            raise ValueError("reason is required")
        evidence = data.get("evidence") or []
        if not isinstance(evidence, list):
            evidence = [str(evidence)]
        reply_text = str(data.get("reply_text") or "").strip()
        if action in REPLY_ACTIONS and not reply_text:
            raise ValueError(f"reply_text is required for {action}")
        return cls(
            action=action,
            confidence=confidence,
            category=str(data.get("category") or "uncategorised").strip()[:60],
            reason=reason,
            evidence=[str(item) for item in evidence][:20],
            reply_text=reply_text if action in REPLY_ACTIONS else "",
            standard=data["follows_standard_outcome"] if isinstance(data.get("follows_standard_outcome"), bool) else None,
        )

    @classmethod
    def fallback(cls, reason: str, action: str = ESCALATE) -> "Decision":
        return cls(action=action, confidence=0.0, category="agent_fallback", reason=reason)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
