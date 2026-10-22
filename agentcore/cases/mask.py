"""One-way masking for past cases, applied once when a case is stored (docs/retrieval.md, section 2).

A past ticket is about another customer. Masking it with this ticket's Pseudonymizer would turn that customer's
account into a token the desk's lookups accept (<NUMBER_3>), and the model could look them up. So a case gets
placeholders that are not tokens and map back to nothing: [NUMBER], [MOBILE], [NATIONAL_ID], [EMAIL] for numbers,
[CUSTOMER] and [STAFF] for names. Text that is already masked (the decisions log) has its tokens turned into the same
placeholders, so no output ever matches TOKEN_RE.

Names: agentcore.pii can't find them in free text, but the builder knows who was on the ticket (the owner from
ost_user, the staff on the thread). Those are replaced exactly: the full name in any case, and each part of it
written with a capital (so "Dear Tendai" goes, the word "gift" in a sentence stays). Greeting lines and the
signature after a sign-off are dropped. A name nobody listed can still get through; that is what case suppression
in the console is for.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from ..pii import PATTERNS, TOKEN_RE

PLACEHOLDERS = {"EMAIL": "[EMAIL]", "NATIONAL_ID": "[NATIONAL_ID]", "MOBILE": "[MOBILE]", "NUMBER": "[NUMBER]"}
CUSTOMER = "[CUSTOMER]"
STAFF = "[STAFF]"
MIN_NAME_PART = 3  # shorter parts ("Jo", initials) would wipe out ordinary words
# A line that is only a greeting ("Dear Tendai,", "Good morning team"); "Dear Sir, my account was ..." is kept.
GREETING = re.compile(r"^\s*(?:dear|hi|hello|hie|hey|greetings|good\s+(?:morning|afternoon|evening|day))\b"
                      r"[^\n.?,!:]{0,40}[,!.:]?\s*$", re.I)
SIGN_OFF = re.compile(r"^\s*(?:(?:kind|best|warm)?\s*regards|thanks(?:\s+and\s+regards)?|thank\s+you|many\s+thanks|"
                      r"yours\s+(?:faithfully|sincerely)|sincerely|cheers)\s*[,.!]?\s*$", re.I)


class CaseMasker:
    """Masks a case's text for storage. customers: the ticket owner and anyone else named as a client;
    staff: the people who wrote on the thread. Names may be None or blank (osTicket rows often are)."""

    def __init__(self, customers: Iterable[Optional[str]] = (), staff: Iterable[Optional[str]] = ()) -> None:
        self._names = _name_patterns(customers, CUSTOMER) + _name_patterns(staff, STAFF)

    def mask(self, text: Optional[str]) -> str:
        if not text:
            return ""
        text = TOKEN_RE.sub(lambda m: PLACEHOLDERS[m.group(0)[1:].rsplit("_", 1)[0]], text)
        for kind, pattern in PATTERNS:
            text = pattern.sub(PLACEHOLDERS[kind], text)
        text = _drop_greeting_and_signature(text)
        for pattern, placeholder in self._names:
            text = pattern.sub(placeholder, text)
        return text.strip()


def _name_patterns(names: Iterable[Optional[str]], placeholder: str) -> list[tuple[re.Pattern[str], str]]:
    """Full names first (any case), then each part with a capital, longest first so "Moyo-Ncube" goes before "Moyo"."""
    full: set[str] = set()
    parts: set[str] = set()
    for name in names:
        words = re.findall(r"[A-Za-z][A-Za-z'-]*", name or "")
        if not words:
            continue
        full.add(" ".join(words))
        parts.update(w for w in words if len(w) >= MIN_NAME_PART)
    out = []
    # Brackets count as letters at the edges, so a name never matches inside a placeholder ("[NUMBER]").
    for name in sorted(full, key=len, reverse=True):
        spaced = r"\s+".join(re.escape(w) for w in name.split())
        out.append((re.compile(rf"(?<![A-Za-z\[]){spaced}(?![A-Za-z\]])", re.I), placeholder))
    for part in sorted(parts, key=len, reverse=True):
        variants = {part[0].upper() + part[1:].lower(), part.upper()} | ({part} if part[0].isupper() else set())
        cased = "|".join(re.escape(v) for v in sorted(variants))
        out.append((re.compile(rf"(?<![A-Za-z'\[-])(?:{cased})(?![A-Za-z'\]-])"), placeholder))
    return out


def _drop_greeting_and_signature(text: str) -> str:
    """Greeting lines go; at the first line that is only a sign-off, it and everything after it go."""
    kept = []
    for line in text.splitlines():
        if SIGN_OFF.match(line):
            break
        if not GREETING.match(line):
            kept.append(line)
    return "\n".join(kept)
