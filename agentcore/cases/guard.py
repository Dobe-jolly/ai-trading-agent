"""What past cases may not do to a reply (docs/retrieval.md, section 4). The evidence gates need nothing new:
find_past_cases writes nothing into a desk's Evidence, so a close resting on a past case alone fails them as a close
with no lookups does (tests/core/test_case_guard.py proves it for each desk).

Two checks on the text:
- A case placeholder ([NUMBER], [MOBILE], [CUSTOMER], ...) in a reply means the model copied from another client's
  ticket. The reply can't be posted as written: agentcore.guardrails.reply_problems blocks it on every desk.
- A reply that repeats a long passage of a case it was shown (COPY_WORDS words in a row) may be accurate, but it
  may also be another client's resolution pasted onto this ticket. It waits for a person (agentcore.runner),
  whatever APPROVAL_MODE says. The desk's own standard reply for this ticket doesn't count: agent cases often
  carry the same wording because it is the standard outcome.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

CASE_PLACEHOLDER = re.compile(r"\[(?:NUMBER|MOBILE|NATIONAL_ID|EMAIL|CUSTOMER|STAFF)\]")
COPY_WORDS = 25
WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")


def placeholder_problem(text: str) -> Optional[str]:
    found = sorted(set(CASE_PLACEHOLDER.findall(text or "")))
    if not found:
        return None
    return (f"reply contains {', '.join(found)} from a past case (past cases are examples from other clients' "
            "tickets, not text for replies)")


def _shingles(text: str, n: int) -> set[tuple[str, ...]]:
    """Every run of n words, letters only: numbers, masked values and punctuation don't hide a copy."""
    words = WORD.findall((text or "").lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def copied_case(reply: str, cases: dict[int, str], allowed: Iterable[Optional[str]] = (),
                n: int = COPY_WORDS) -> Optional[int]:
    """The first case whose text shares n words in a row with the reply, not counting runs that are also in an
    allowed text (the desk's standard reply for this ticket). None when the reply is its own."""
    mine = _shingles(reply, n)
    if not mine:
        return None
    for text in allowed:
        mine -= _shingles(text or "", n)
    for case_id in sorted(cases):
        if mine & _shingles(cases[case_id], n):
            return case_id
    return None
