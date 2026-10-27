"""Rules both agents apply to what the model proposes, before anything is written to osTicket: the
text of a reply, and the result type. Each agent adds its own evidence rules (agent/guardrails.py)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .cases.guard import placeholder_problem
from .decision import Decision
from .pii import TOKEN_RE, Pseudonymizer

MIN_REPLY_CHARS = 20
MAX_REPLY_CHARS = 2000
SECRET_WORDS = re.compile(r"\b(PIN|OTP|CVV|CVC|password|passcode|one[- ]time (?:pin|password))\b", re.I)
# Desks whose subject is OTPs and passwords (NMB Connect) may name them, but never ask for one or give one out.
SECRET_ASK = re.compile(r"(?i)\b(?:send|share|provide|give|tell|forward|reply with|confirm|disclose|let us have|"
                        r"read out|ask (?:the client |them )?for)\s+(?:us\s+|me\s+)?(?:the\s+|their\s+|his\s+|"
                        r"her\s+|your\s+|its\s+|that\s+|this\s+)?(?:client'?s?\s+|customer'?s?\s+)?(?:new\s+|"
                        r"current\s+|old\s+|\w+[- ]digit\s+)?(?:PIN|OTP|password|passcode|one[- ]time (?:pin|password)|"
                        r"CVV|CVC)s?\b")
SECRET_VALUE = re.compile(r"(?i)\b(?:PIN|OTP|password|passcode|username|user name)\b\s*(?:is|was|:|=|-)\s*"
                          r"(?:[\"'][^\"']+[\"']|(?=\S*\d)[^\s<]\S{2,})")
CARD_CODE = re.compile(r"\b(?:CVV|CVC)\b", re.I)
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


def reply_problems(text: str, pseudo: Optional[Pseudonymizer] = None, *, secret_topic: bool = False) -> list[str]:
    """What stops a reply from being posted. secret_topic: the desk's subject is OTPs and passwords, so the words
    themselves are allowed, but asking for a PIN, OTP or password, or writing one out, is not."""
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
    if not secret_topic and SECRET_WORDS.search(text):
        problems.append("reply mentions a PIN, OTP, password or card code")
    if secret_topic and (SECRET_ASK.search(text) or SECRET_VALUE.search(text) or CARD_CODE.search(text)):
        problems.append("reply asks for or gives out a PIN, OTP, password, username or card code")
    if URL_RE.search(text):
        problems.append("reply contains a link")
    if TEMPLATE_LEFTOVERS.search(text):
        problems.append("reply contains template leftovers")
    case_text = placeholder_problem(text)
    if case_text:
        problems.append(case_text)
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
