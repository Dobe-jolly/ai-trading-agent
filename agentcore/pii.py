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
    # Zimbabwe national ID, e.g. 63-123456A78 or 63123456 A 78
    ("NATIONAL_ID", re.compile(r"(?<![\w-])\d{2}-?\s?\d{6,7}\s?[A-Za-z]\s?-?\d{2}(?![\w])")),
    # Zimbabwe mobiles: 0772123456, 0772 123 456, +263 77 212 3456, 263772123456
    ("MOBILE", re.compile(r"(?<![\d+])(?:\+?263|0)[\s-]?7\d(?:[\s-]?\d){7}(?!\d)")),
    # Account, card and other long numbers (8+ digits)
    ("NUMBER", re.compile(r"(?<!\d)\d{8,19}(?!\d)")),
]
TOKEN_RE = re.compile(r"<(?:EMAIL|NATIONAL_ID|MOBILE|NUMBER)_\d+>")


def _canonical(kind: str, value: str) -> str:
    if kind == "MOBILE":
        return re.sub(r"\D", "", value)[-9:]  # same number in any format -> one token
    if kind in ("EMAIL", "NATIONAL_ID"):
        return re.sub(r"[\s-]", "", value).upper()
    return value.lstrip("0") or "0"  # account 0001234567890 and 1234567890 are the same account


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
            self._value_for[token] = re.sub(r"\s", "", raw) if kind != "EMAIL" else raw
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
