"""The router's decision: the shared one (agentcore.decision) with the help topic the ticket belongs under."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from agentcore.decision import *  # noqa: F401,F403
from agentcore.decision import ESCALATE, MOVE, NO_ACTION, REQUEST_INFO, Decision

ROUTE_ACTIONS = (MOVE, REQUEST_INFO, ESCALATE, NO_ACTION)


@dataclass
class RouteDecision(Decision):
    actions: ClassVar[tuple] = ROUTE_ACTIONS
    target_topic: str = ""                                  # the help topic it belongs under, as listed
    alternatives: list[str] = field(default_factory=list)   # other topics that could fit, best first

    @classmethod
    def extra_fields(cls, action: str, data: dict[str, Any]) -> dict[str, Any]:
        target = str(data.get("target_topic") or "").strip()
        if action == MOVE and not target:
            raise ValueError("target_topic is required for move")
        alternatives = data.get("alternatives") or []
        if not isinstance(alternatives, list):
            alternatives = [str(alternatives)]
        return {"target_topic": target[:200], "alternatives": [str(a).strip()[:200] for a in alternatives if str(a).strip()][:4]}


SUBMIT_DECISION_TOOL: dict[str, Any] = {
    "name": "submit_decision",
    "description": ("Submit where this ticket belongs. Call it exactly once, after you have read the ticket and "
                    "looked at how similar tickets were filed. Calling it ends your work on the ticket."),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ROUTE_ACTIONS), "description": (
                "move: the ticket clearly belongs under target_topic; it is filed there (or suggested to staff when "
                "the rules say a person must move it). "
                "request_info: the ticket does not say what the problem is; post a reply asking the branch to "
                "describe it, and keep the ticket open. "
                "escalate: you cannot tell which topic fits, or it fits two; an internal note with your best "
                "candidates is posted for a person. "
                "no_action: it belongs where it is (a general request with no more specific topic).")},
            "target_topic": {"type": "string", "description": (
                "The help topic it belongs under, written exactly as in the list of topics. Required for move; for "
                "escalate, your best candidate if you have one.")},
            "alternatives": {"type": "array", "items": {"type": "string"},
                             "description": "Other topics from the list that could fit, best first (at most 3)."},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1,
                           "description": "How sure you are that the ticket belongs under target_topic, 0 to 1."},
            "category": {"type": "string", "description": "A short label for what the ticket is about, e.g. "
                                                          "'failed ZIPIT', 'NMB Connect login'."},
            "reason": {"type": "string", "description": (
                "Internal only (the decision log and the note to staff). 2-4 sentences: what the ticket is about and "
                "why it belongs under that topic.")},
            "evidence": {"type": "array", "items": {"type": "string"}, "description": (
                "What points to the topic: words from the ticket, how similar closed tickets were filed.")},
            "reply_text": {"type": "string", "description": (
                "Only for request_info: the reply to the branch asking what the problem is. Masked values like "
                "<NUMBER_1> may be used; they are filled in before posting.")},
            "follows_standard_outcome": {"type": "boolean", "description": (
                "For request_info: true when the reply is the standard request for the problem and the client's "
                "details; false when you asked for something else. Replies marked false wait for a person.")},
        },
        "required": ["action", "confidence", "category", "reason", "evidence"],
    },
}
