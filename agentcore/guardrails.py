"""Rules both agents apply to what the model proposes, before anything is written to osTicket: the
text of a reply, and the result type. Each agent adds its own evidence rules (agent/guardrails.py)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .decision import Decision
from .pii import TOKEN_RE, Pseudonymizer

MIN_REPLY_CHARS = 20
MAX_REPLY_CHARS = 2000
SECRET_WORDS = re.compile(r"\b(PIN|OTP|CVV|CVC|password|passcode|one[- ]time (?:pin|password))\b", re.I)
URL_RE = re.compile(r"https?://|www\.", re.I)
DATES = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
DIGIT_RUN = re.compile(r"\d(?:[\s-]?\d)+")
TEMPLATE_LEFTOVERS = re.compile(r"\{\{|\}\}|\[(?:insert|name|customer|todo|account|number)[^\]]*\]", re.I)
LOOSE_TOKEN = re.compile(r"<[A-Z_]+_\d+>")


def has_raw_long_number(text: str) -> bool:
    """8+ digits (spaces/dashes allowed) that did not come from a masked token, ignoring dates.
    The model never sees real account or phone numbers, so a raw one in its reply is invented."""
    text = DATES.sub(" ", TOKEN_RE.sub(" ", text))
    return any(len(re.sub(r"\D", "", m.group())) >= 8 for m in DIGIT_RUN.finditer(text))


def reply_problems(text: str, pseudo: Optional[Pseudonymizer] = None) -> list[str]:
    problems: list[str] = []
    if len(text) < MIN_REPLY_CHARS:
        problems.append("reply is too short")
    if len(text) > MAX_REPLY_CHARS:
        problems.append("reply is too long")
    unknown = pseudo.unknown_tokens(text) if pseudo else TOKEN_RE.findall(text)
    stray = [t for t in LOOSE_TOKEN.findall(text) if not TOKEN_RE.fullmatch(t)]
    if unknown or stray:
        problems.append("reply contains a placeholder that does not exist in this ticket")
    if has_raw_long_number(text):
        problems.append("reply contains an account/phone number the agent was never shown")
    if SECRET_WORDS.search(text):
        problems.append("reply mentions a PIN, OTP, password or card code")
    if URL_RE.search(text):
        problems.append("reply contains a link")
    if TEMPLATE_LEFTOVERS.search(text):
        problems.append("reply contains template leftovers")
    return problems


@dataclass
class GuardrailResult:
    proposed: Decision
    final: Decision
    notes: list[str] = field(default_factory=list)
    deferred: bool = False  # retry next run (not a real decision)

    @property
    def changed(self) -> bool:
        return self.final.action != self.proposed.action
