"""Pseudonymise customer identifiers before anything is sent to the model.

The model sees tokens like <MOBILE_1> and <NUMBER_2>. When it passes a token
to a lookup tool, the real value is swapped back in before Metabase is
queried, and the results are masked again on the way back. Real values in the
ticket text never leave the server. (Images and PDFs attached to a ticket are
the exception when ATTACHMENT_MODE=model: see agent/attachments.py.)

Names in free text are NOT masked (regexes can't find them reliably).
"""
from __future__ import annotations

import re
from typing import Any

# Order matters: more specific patterns run first.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    # Zimbabwe national ID, e.g. 63-123456A78 or 63123456 A 78, also after a label and a dash (ID-63-123456A78);
    # never inside a longer dashed number (0012-63-...)
    ("NATIONAL_ID", re.compile(r"(?<!\w)(?<!\d-)\d{2}-?\s?\d{6,7}\s?[A-Za-z]\s?-?\d{2}(?![\w])")),
    # ... and as branches often write it, without the check letter: 63-1234567, 63-123456-78 (the dash after the
    # district code is what tells it from other numbers)
    ("NATIONAL_ID", re.compile(r"(?<!\w)(?<!\d-)\d{2}-\d{6,7}(?:-\d{2})?(?![\w-])")),
    # Zimbabwe mobiles: 0772123456, 0772 123 456, +263 77 212 3456, 263772123456
    ("MOBILE", re.compile(r"(?<![\d+])(?:\+?263|0)[\s-]?7\d(?:[\s-]?\d){7}(?!\d)")),
    # Account, card and other long numbers (8+ digits)
    ("NUMBER", re.compile(r"(?<!\d)\d{8,19}(?!\d)")),
    # ... written in groups: cards as 4000 1234 5678 9010, accounts as 0012-3456-7890 or 0012-63-1234567 (three or
    # more groups, the first of 3+ digits, 9+ digits in all; or two groups of 4+). Not an amount written in thousands
    # (1 500 000, 125 000 000): the guardrails need amounts, and that is how amounts are written. Not a date
    # (2026-10-26, followed by a time or not).
    ("NUMBER", re.compile(r"(?<![\d-])(?!\d{1,3}(?: \d{3})+(?![\d-]|[ -]\d))(?!\d{4}-\d{2}-\d{2}(?!\d))"
                          r"(?:(?=(?:\d[ -]?){9})\d{3,7}(?:[ -]\d{2,7}){2,6}|\d{4,10}[ -]\d{4,10})(?![\d-])")),
]
TOKEN_RE = re.compile(r"<(?:EMAIL|NATIONAL_ID|MOBILE|NUMBER)_\d+>")


def _canonical(kind: str, value: str) -> str:
    if kind == "MOBILE":
        return re.sub(r"\D", "", value)[-9:]  # same number in any format -> one token
    if kind in ("EMAIL", "NATIONAL_ID"):
        return re.sub(r"[\s-]", "", value).upper()
    digits = re.sub(r"\D", "", value)  # 4000 1234 5678 9010 and 4000123456789010 are the same card
    return digits.lstrip("0") or "0"  # account 0001234567890 and 1234567890 are the same account


class Pseudonymizer:
    def __init__(self) -> None:
        self._token_for: dict[tuple[str, str], str] = {}
        self._value_for: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def _token(self, kind: str, raw: str) -> str:
        key = (kind, _canonical(kind, raw))
        if key not in self._token_for:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            token = f"<{kind}_{self._counts[kind]}>"
            self._token_for[key] = token
            self._value_for[token] = (raw if kind == "EMAIL" else re.sub(r"[\s-]", "", raw) if kind == "NUMBER"
                                      else re.sub(r"\s", "", raw))
        return self._token_for[key]

    def mask(self, text: str) -> str:
        for kind, pattern in PATTERNS:
            text = pattern.sub(lambda m, k=kind: self._token(k, m.group(0)), text)
        return text

    def unmask(self, text: str) -> str:
        return TOKEN_RE.sub(lambda m: self._value_for.get(m.group(0), m.group(0)), text)

    def unknown_tokens(self, text: str) -> list[str]:
        """Tokens in text that this ticket never issued (the model made them up)."""
        return [t for t in TOKEN_RE.findall(text) if t not in self._value_for]

    def mask_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.mask(obj)
        if isinstance(obj, bool) or obj is None or isinstance(obj, float):
            return obj
        if isinstance(obj, int):
            return self.mask(str(obj)) if len(str(abs(obj))) >= 8 else obj
        if isinstance(obj, dict):
            return {key: self.mask_obj(value) for key, value in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.mask_obj(value) for value in obj]
        return self.mask(str(obj))

    def unmask_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.unmask(obj)
        if isinstance(obj, dict):
            return {key: self.unmask_obj(value) for key, value in obj.items()}
        if isinstance(obj, list):
            return [self.unmask_obj(value) for value in obj]
        return obj

    @property
    def token_count(self) -> int:
        return len(self._value_for)

    def export(self) -> dict[str, str]:
        """token -> real value, for a reply that waits for approval (kept only until it is posted or dropped)."""
        return dict(self._value_for)

    @classmethod
    def from_export(cls, mapping: dict[str, str]) -> "Pseudonymizer":
        pseudo = cls()
        pseudo._value_for = {str(k): str(v) for k, v in (mapping or {}).items()}
        return pseudo
