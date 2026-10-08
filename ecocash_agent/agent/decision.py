"""The structured decision the model must return, via the submit_decision tool."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

REPLY_AND_CLOSE = "reply_and_close"
REQUEST_INFO = "request_info"
ESCALATE = "escalate"
NO_ACTION = "no_action"
ACTIONS = (REPLY_AND_CLOSE, REQUEST_INFO, ESCALATE, NO_ACTION)
REPLY_ACTIONS = (REPLY_AND_CLOSE, REQUEST_INFO)

SUBMIT_DECISION_TOOL: dict[str, Any] = {
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
                    "reply_and_close: the lookups explain the issue and the reply tells the branch what to do; "
                    "post the reply and close. "
                    "request_info: a specific detail is missing or the branch must act and report back (e.g. no mobile number, or delink first); "
                    "post a reply asking for it and keep the ticket open. "
                    "escalate: a person must handle it; an internal note with your findings is posted. "
                    "no_action: nothing to do yet."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "How sure you are that this action is correct, 0 to 1.",
            },
            "category": {
                "type": "string",
                "description": "The main finding behind the decision: the error_type or status from the lookup (e.g. already_linked, kyc_mismatch, wallet_closed, linked), or e.g. missing_mobile_number.",
            },
            "reason": {
                "type": "string",
                "description": "Internal only (goes in the audit log and escalation notes). 2-5 sentences: what you checked and why this action.",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Facts from tool results that support the decision, one per item.",
            },
            "reply_text": {
                "type": "string",
                "description": "The reply posted on the ticket (read by the branch/service-desk staff who raised it). Required for reply_and_close and request_info, empty otherwise. Masked values like <NUMBER_1> may be used; they are filled in before posting.",
            },
            "follows_standard_outcome": {
                "type": "boolean",
                "description": (
                    "true when the action and reply follow the standard reply for the latest attempt; false when you "
                    "reasoned beyond it (other lookups, the thread, attachments) to an outcome the standard reply would "
                    "not give. Replies marked false wait for a person to approve them."
                ),
            },
        },
        "required": ["action", "confidence", "category", "reason", "evidence", "follows_standard_outcome"],
    },
}


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
