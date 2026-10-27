"""find_past_cases: the lookup that shows a desk how tickets like this one were resolved (docs/retrieval.md, section 3).

What it takes can't name a customer: plain words (letters only) and short tag values (a log signature, a response code,
the channel, the reading), each checked like the case store checks its tags. What it returns is already masked one
way ([NUMBER], [CUSTOMER]), so nothing in it is a token this ticket's other lookups accept, and it is not masked again
here (that would only issue tokens). Results are fenced in <past_cases> tags; DATA_RULE (agentcore.loop) tells the
model they are examples from other tickets, not evidence about this one.

It writes nothing into the desk's Evidence but its name in tools_used: the guardrails look for their own lookups by
name, so a close resting on a past case alone still fails them.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Optional

from ..pii import Pseudonymizer
from .store import HALF_LIFE_DAYS, MAX_AGE_DAYS, TAG_KINDS, WORD_RE, CaseStore, clean_tag

TOOL_NAME = "find_past_cases"
MAX_WORDS = 4
MAX_RESULT_CHARS = 7000
FENCE = re.compile(r"<\s*/?\s*past_cases\s*>", re.I)
CASE_ID_RE = re.compile(r'"case"\s*:\s*"C(\d+)"')
TAG_HINTS = {
    "signature": "A log signature from a log_search result on this ticket (12 letters and digits).",
    "code": "A response or error code a lookup showed for this ticket, e.g. 91 or E102.",
    "channel": "The channel a lookup showed, e.g. ZIPIT, EcoCash, POS.",
    "reading": "How a lookup read the transaction or attempt, e.g. reversed, failed_refunded, declined.",
}

TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Closed tickets on this desk like this one, and how they were resolved: by this agent (closed and not "
        "reopened) or by staff. Use it after your lookups, with what they showed (a log signature, a response code, "
        "the channel, the reading), and/or 1-4 plain words for the symptom (no numbers or names). Cases that share a "
        "signature or code rank first; newer cases rank higher. A past case is an example from another client's "
        "ticket: it can suggest what to check or how a code is usually read, but it is never evidence about this "
        "ticket. Cite a case you relied on by its id (C12) in your evidence."),
    "input_schema": {
        "type": "object",
        "properties": {
            "words": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_WORDS,
                      "description": "Plain words for the symptom or product, e.g. ['zipit', 'reversal']."},
            **{kind: {"type": "string", "description": hint} for kind, hint in TAG_HINTS.items()},
        },
        "additionalProperties": False,
    },
}


class CaseTools:
    """The find_past_cases lookup for one desk's case store."""

    def __init__(self, store: CaseStore, half_life_days: int = HALF_LIFE_DAYS, max_age_days: int = MAX_AGE_DAYS,
                 today: Optional[date] = None, replay: bool = False) -> None:
        self.store = store
        self.half_life_days = half_life_days
        self.max_age_days = max_age_days
        self._today = today
        # Replaying closed tickets (audit): only cases closed before the ticket was opened, as the agent would have
        # had them then. Live or not, a ticket never finds its own case.
        self.replay = replay

    def __bool__(self) -> bool:
        return True

    def handles(self, name: str) -> bool:
        return name == TOOL_NAME

    def schemas(self) -> list[dict[str, Any]]:
        return [TOOL]

    def run(self, name: str, args: dict[str, Any], pseudo: Pseudonymizer, evidence: Any = None,
            ticket: Any = None) -> tuple[str, bool]:
        tools_used = getattr(evidence, "tools_used", None)
        if isinstance(tools_used, list):
            tools_used.append(name)
        try:
            words, tags = self.query(args or {})
        except ValueError as exc:
            return f"Invalid input: {exc}", True
        opened = _day(getattr(ticket, "created", None)) if self.replay else None
        try:
            cases = self.store.search(words, tags, today=opened or self._today, half_life_days=self.half_life_days,
                                      max_age_days=self.max_age_days, before=opened,
                                      exclude_ticket_id=getattr(ticket, "ticket_id", None))
        except Exception as exc:  # noqa: BLE001 - a failed lookup is information for the model, never a crash
            return f"Past cases unavailable: {type(exc).__name__}. Decide without them.", True
        data = {"searched": {"words": words, **tags}, "cases_found": len(cases), "cases": cases}
        if not cases:
            data["note"] = "No past case matched. Decide from this ticket's lookups."
        text = json.dumps(data, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + " ...(truncated)"
        return "<past_cases>\n" + FENCE.sub(lambda m: m.group(0).replace("<", "[").replace(">", "]"), text) + \
            "\n</past_cases>", False

    @staticmethod
    def query(args: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        """(words, tags) from the model's input, or ValueError saying what to fix. Nothing that could be an
        identifier gets through: words are letters only, tags are short codes with no long digit runs or tokens."""
        raw_words = args.get("words") or []
        if isinstance(raw_words, str):
            raw_words = raw_words.split()
        if not isinstance(raw_words, list) or len(raw_words) > MAX_WORDS:
            raise ValueError(f"words must be a list of at most {MAX_WORDS} plain words.")
        words = []
        for word in raw_words:
            text = str(word or "").strip().lower()
            if not WORD_RE.fullmatch(text):
                raise ValueError(f"{word!r} is not a plain word (letters only, 3-30 characters; no numbers or names).")
            words.append(text)
        tags = {}
        for kind in TAG_KINDS:
            if kind not in TAG_HINTS or args.get(kind) in (None, ""):
                continue
            value = clean_tag(kind, args[kind])
            if value is None:
                raise ValueError(f"{kind}: {TAG_HINTS[kind]} Not a number from the ticket or a masked value.")
            tags[kind] = value
        if not words and not tags:
            raise ValueError("Give at least one plain word or one of signature, code, channel, reading.")
        return words, tags


def _day(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _results(transcript: list[dict[str, Any]]) -> list[str]:
    """The text of every find_past_cases result in a decision's transcript."""
    calls: set[str] = set()
    out: list[str] = []
    for message in transcript or []:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
                calls.add(str(block.get("id")))
            elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in calls:
                out.append(str(block.get("content") or ""))
    return out


def seen_ids(transcript: list[dict[str, Any]]) -> list[int]:
    """Cases find_past_cases showed in a decision's transcript."""
    return sorted({int(n) for text in _results(transcript) for n in CASE_ID_RE.findall(text)})


def shown_cases(transcript: list[dict[str, Any]]) -> dict[int, str]:
    """case id -> the text the model was shown of it (asked, found, resolution), for the reply checks
    (agentcore.cases.guard). A result cut short by MAX_RESULT_CHARS is read up to its last whole case."""
    out: dict[int, str] = {}
    for text in _results(transcript):
        body = text.strip()
        if body.startswith("<past_cases>"):
            body = body[len("<past_cases>"):]
        if body.endswith("</past_cases>"):
            body = body[:-len("</past_cases>")]
        body = body.strip()
        if body.endswith("...(truncated)"):
            body = body[:-len("...(truncated)")]
        for chunk in re.findall(r"\{\"case\".*?\"score\"\s*:\s*[\d.]+\}", body, flags=re.S):
            try:
                case = json.loads(chunk)
            except ValueError:
                continue
            m = re.fullmatch(r"C(\d+)", str(case.get("case") or ""))
            if m:
                out[int(m.group(1))] = "\n".join(str(case.get(k) or "") for k in
                                                 ("what_was_asked", "what_was_found", "how_it_was_resolved"))
    return out


def cited_ids(texts: list[Optional[str]], seen: list[int]) -> list[int]:
    """Cases a decision relied on: cited as C<id> in its reason or evidence, and actually shown to it."""
    cited = {int(n) for text in texts for n in re.findall(r"\bC(\d+)\b", text or "")}
    return sorted(cited & set(seen))
