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
# A name nobody listed, written where branches write names: "Client Tendai Moyo", "Mr. MOYO", "account holder:
# Rudo Banda", "name is Farai". The capitalised words after the cue are masked, up to the first ordinary word.
CUE = (r"\b(?i:client|customer|beneficiary|account\s+holder|holder|name|mr|mrs|ms|miss|dr|madam)\b\.?"
       r"(?:\s*[:,-]\s*|\s+)(?:(?i:is|named|called)\s+)?")
NAME_WORD = r"(?:[A-Z][a-z'-]+|[A-Z]{3,}(?:'[A-Z]+)?)"
CUE_NAME = re.compile(CUE + rf"({NAME_WORD}(?:\s+{NAME_WORD}){{0,3}})")
NOT_A_NAME = set("""account accounts acc number no services service care support details detail information info mobile
phone card cards branch bank has had was is are the and of for on in at to from with complains complained says said
reports reported wants requested request requests transferred tried did does cannot can could would will called who
zipit ecocash rtgs pos atm usd zwg zig otp pin visa mastercard nmb connect internet banking app online name names status
statement complaint query transaction transactions transfer payment reversal refund wallet registration login
please kindly team dear sir madam""".split())
SIGN_OFF = re.compile(r"^\s*(?:(?:kind|best|warm)?\s*regards|thanks(?:\s+and\s+regards)?|thank\s+you|many\s+thanks|"
                      r"yours\s+(?:faithfully|sincerely)|sincerely|cheers)\s*[,.!]?\s*$", re.I)


class CaseMasker:
    """Masks a case's text for storage. customers: the ticket owner and anyone else named as a client;
    staff: the people who wrote on the thread. Names may be None or blank (osTicket rows often are)."""

    def __init__(self, customers: Iterable[Optional[str]] = (), staff: Iterable[Optional[str]] = ()) -> None:
        self._names = _name_patterns(customers, CUSTOMER) + _name_patterns(staff, STAFF)
        self._learned: set[str] = set()

    def mask(self, text: Optional[str]) -> str:
        if not text:
            return ""
        text = TOKEN_RE.sub(lambda m: PLACEHOLDERS[m.group(0)[1:].rsplit("_", 1)[0]], text)
        for kind, pattern in PATTERNS:
            text = pattern.sub(PLACEHOLDERS[kind], text)
        text = _drop_greeting_and_signature(text)
        # A name found after a cue is masked wherever else it appears, here and in the case's later fields
        # ("Client Tendai Moyo ... Tendai can retry").
        learned = {n for n in (_cue_split(m)[0] for m in CUE_NAME.finditer(text)) if n} - self._learned
        if learned:
            self._learned |= learned
            self._names += _name_patterns(learned, CUSTOMER)
        for pattern, placeholder in self._names:
            text = pattern.sub(placeholder, text)
        return CUE_NAME.sub(_cue_name, text).strip()


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


def _cue_split(m: re.Match[str]) -> tuple[Optional[str], int]:
    """(the name after a cue: its capitalised words up to the first ordinary one, or None; where it ends in group 1)."""
    end = None
    for word in re.finditer(r"\S+", m.group(1)):
        if re.sub(r"'s$", "", word.group().lower()) in NOT_A_NAME:
            break
        end = word.end()
    if end is None:
        return None, 0
    return re.sub(r"'s$", "", m.group(1)[:end]), end


def _cue_name(m: re.Match[str]) -> str:
    """The cue kept, the name after it masked."""
    name, end = _cue_split(m)
    if name is None:
        return m.group(0)
    cue = m.group(0)[:m.start(1) - m.start(0)]
    return cue + CUSTOMER + m.group(1)[end:]


def _drop_greeting_and_signature(text: str) -> str:
    """Greeting lines go; at the first line that is only a sign-off, it and everything after it go."""
    kept = []
    for line in text.splitlines():
        if SIGN_OFF.match(line):
            break
        if not GREETING.match(line):
            kept.append(line)
    return "\n".join(kept)
