"""The router's lookups: how similar closed tickets were filed, and what is usually filed under a topic. Both read
osTicket only; results are masked like everything else the model sees. There are no customer lookups."""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from agentcore.metabase import MetabaseError
from agentcore.pii import Pseudonymizer
from .osticket import OsTicket, Ticket
from .topics import Catalogue, Topic

log = logging.getLogger(__name__)
MAX_RESULT_CHARS = 7000
WORD = re.compile(r"[A-Za-z][A-Za-z'&-]{2,29}")
STOP_WORDS = {"the", "and", "for", "client", "customer", "please", "assist", "help", "issue", "problem", "kindly",
              "with", "not", "has", "have", "was", "his", "her", "their", "account", "number", "bank", "branch"}

SIMILAR_TOOL = {
    "name": "find_similar_closed_tickets",
    "description": (
        "Closed tickets (last year) whose subject or first message contains every one of the given words, and the "
        "help topic each was filed under. Shows how the service desk files tickets like this one. Use 1-3 "
        "distinctive words (a product or a symptom: 'zipit', 'otp', 'statement'), not numbers or names."),
    "input_schema": {"type": "object", "properties": {
        "words": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3}},
        "required": ["words"]},
}
EXAMPLES_TOOL = {
    "name": "get_topic_examples",
    "description": ("Recent tickets filed under one help topic: subject and the start of the first message. Use it "
                    "to check what a topic is really used for before choosing it."),
    "input_schema": {"type": "object", "properties": {
        "topic": {"type": "string", "description": "A topic name exactly as in the list."}}, "required": ["topic"]},
}


@dataclass
class Evidence:
    """What the lookups showed, for the guardrails, the notes and the log."""
    searches: list[dict[str, Any]] = field(default_factory=list)
    topics_checked: list[str] = field(default_factory=list)
    similar_topics: Counter = field(default_factory=Counter)  # topic name -> similar closed tickets filed there
    check_errors: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"searches": len(self.searches), "topics_checked": self.topics_checked[:6],
                "similar": dict(self.similar_topics.most_common(5)), "errors": self.check_errors[:3]}


class Toolbox:
    def __init__(self, store: Optional[OsTicket], catalogue: Optional[Catalogue], examples_days: int = 365) -> None:
        self.store = store
        self.catalogue = catalogue
        self.examples_days = examples_days
        self.as_of = None  # closed-ticket replays: nothing to rewind (the topics are as they are now)

    def schemas(self) -> list[dict[str, Any]]:
        return [SIMILAR_TOOL, EXAMPLES_TOOL] if self.store is not None else []

    def run(self, name: str, args: dict[str, Any], pseudo: Pseudonymizer, evidence: Evidence,
            ticket: Optional[Ticket]) -> tuple[str, bool]:
        """Returns (masked result text, is_error)."""
        evidence.tools_used.append(name)
        try:
            if name == "find_similar_closed_tickets":
                data = self._similar(args, evidence)
            elif name == "get_topic_examples":
                data = self._examples(args, evidence)
            else:
                return f"Unknown tool {name!r}.", True
        except ValueError as exc:
            evidence.refused.append(name)
            return str(exc), True
        except MetabaseError as exc:
            evidence.check_errors.append(f"{name}: {str(exc)[:200]}")
            return f"The lookup failed: {str(exc)[:300]}", True
        text = json.dumps(data, ensure_ascii=False, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + " ...(truncated)"
        return pseudo.mask(text), False

    # ---- lookups ---------------------------------------------------------------------------------
    @staticmethod
    def words(raw: Any) -> list[str]:
        """Plain words only: no numbers (so no account, mobile or reference can be searched) and no names list."""
        items = raw if isinstance(raw, list) else [raw]
        words = []
        for item in items[:3]:
            text = str(item or "").strip().lower()
            if not WORD.fullmatch(text):
                raise ValueError(f"{item!r} is not a plain word (letters only, 3-30 characters; no numbers).")
            if text not in STOP_WORDS:
                words.append(text)
        if not words:
            raise ValueError("Give at least one distinctive word (a product or a symptom).")
        return words

    def _similar(self, args: dict[str, Any], evidence: Evidence) -> dict[str, Any]:
        assert self.store is not None
        words = self.words(args.get("words"))
        rows = self.store.similar(words, self.examples_days)
        by_topic: Counter = Counter()
        for r in rows:
            topic = self.catalogue.find(r["help_topic"]) if self.catalogue else None
            by_topic[topic.name if topic else (r["help_topic"].strip() or "(no topic)")] += 1
        evidence.similar_topics.update(by_topic)
        evidence.searches.append({"tool": "find_similar_closed_tickets", "args": {"words": words}, "found": len(rows)})
        return {
            "words": words, "closed_tickets_found": len(rows),
            "filed_under": [{"topic": t, "tickets": n, **self._topic_note(t)} for t, n in by_topic.most_common(8)],
            "examples": [{"subject": r["subject"][:120], "filed_under": r["help_topic"].strip()} for r in rows[:6]],
        }

    def _examples(self, args: dict[str, Any], evidence: Evidence) -> dict[str, Any]:
        assert self.store is not None
        topic = self._topic(args.get("topic"))
        rows = self.store.examples(topic.topic_id, self.examples_days)
        evidence.topics_checked.append(topic.name)
        evidence.searches.append({"tool": "get_topic_examples", "args": {"topic": topic.name}, "found": len(rows)})
        return {"topic": topic.name, "description": topic.description, "worked_by_agent": topic.desk,
                "tickets_last_90_days": topic.recent,
                "examples": [{"subject": r["subject"][:120], "message": r["message"][:240]} for r in rows]}

    def _topic(self, name: Any) -> Topic:
        topic = self.catalogue.find(str(name or "")) if self.catalogue else None
        if topic is None:
            raise ValueError(f"{name!r} is not an active help topic. Use a name exactly as in the list.")
        return topic

    def _topic_note(self, name: str) -> dict[str, Any]:
        topic = self.catalogue.find(name) if self.catalogue else None
        if topic is None:
            return {"active": False}
        note: dict[str, Any] = {"active": True}
        if self.catalogue.is_catch_all(topic.path):
            note["catch_all"] = True
        if topic.desk:
            note["worked_by_agent"] = topic.desk
        return note
