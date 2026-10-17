"""Masking for logs, with keyed hashes for identifiers.

An account, mobile, ID number or other long number in a log line becomes <NUMBER:hash>, <MOBILE:hash>, ... where hash
is a keyed hash (HMAC-SHA256 with LOG_HASH_KEY) of its canonical form. The same number always gives the same hash, so
a ticket's account can be searched for (hash it the same way) while no account number is stored or shown. Without the
key a hash cannot be reversed or recomputed; keep the key out of the store. Names, addresses, PINs, tokens and other
personal values in key=value, JSON or XML form are replaced outright (<MASKED>); secrets and JWTs are removed.

The hash is written in letters only (a-p), so the desk's own masking never mistakes it for a number."""
from __future__ import annotations

import hashlib
import hmac
import re

from agentcore.logsafe import AUTH_HEADER, SECRET
from agentcore.pii import PATTERNS, _canonical

TRACK2 = re.compile(r"\d{12,19}[=D]\d{4,}[\dF]*")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
HEXBLOB = re.compile(r"\b[0-9A-Fa-f]{24,}\b")
LONGRUN = re.compile(r"\d{20,}")
NAME_KEYS = (r"(?:first|last|middle|sur|full|customer|client|account|beneficiary|sender|receiver|recipient|holder|payee|"
             r"owner|father|mother|next_?of_?kin|employer|user)_?name|name")
OTHER_KEYS = (r"addr(?:ess)?\w*|street|city|town|suburb|dob|date_?of_?birth|birth\w*|national_?id\w*|id_?(?:no|num\w*|number)|"
              r"passport\w*|contact\w*|pin|otp|password|passwd|secret|"
              r"token|imei|signature|photo|image|selfie|document\w*")
KEYS = NAME_KEYS + "|" + OTHER_KEYS
_OPEN = r"((?:\"|')?\b(?:%s)\b(?:\"|')?\s*[:=]\s*(?:\"|')?)"
KV_NAME = re.compile("(?i)" + _OPEN % NAME_KEYS + r"([^\"',;&}\]\s]+(?: [A-Za-z][^\"',;&}\]\s=:]*(?=[\s\"',;&}\]]|$)){0,3})")  # a name: up to four words
KV_ONE = re.compile("(?i)" + _OPEN % OTHER_KEYS + r"([^\"',;&}\]\s]+)")
XML = re.compile(r"(?i)(<(" + KEYS + r")>)([^<]{1,80})(</)")
# (email, mobile and phone values are not blanked: they become keyed hashes below, so a ticket's mobile can be searched)
TOKEN = re.compile(r"<(?:NUMBER|MOBILE|EMAIL|NATIONAL_ID):([a-p]{12})>")
_ALPHABET = "abcdefghijklmnop"


def _letters(digest: bytes) -> str:
    return "".join(_ALPHABET[b >> 4] + _ALPHABET[b & 15] for b in digest[:6])  # 12 letters


class KeyedMasker:
    def __init__(self, key: str | bytes) -> None:
        key = key.encode() if isinstance(key, str) else key
        if len(key) < 16:
            raise ValueError("LOG_HASH_KEY must be at least 16 characters")
        self._key = key

    def hash_of(self, kind: str, raw: str) -> str:
        canon = _canonical(kind, raw)
        return _letters(hmac.new(self._key, f"{kind}:{canon}".encode(), hashlib.sha256).digest())

    def mask(self, text: str) -> str:
        text = TRACK2.sub("<TRACK2>", text)
        text = JWT.sub("<JWT>", text)
        text = AUTH_HEADER.sub(r"\1***", text)
        text = SECRET.sub(r"\1***", text)
        text = XML.sub(lambda m: f"{m[1]}<MASKED>{m[4]}", text)
        text = KV_NAME.sub(lambda m: m[1] + "<MASKED>", text)
        text = KV_ONE.sub(lambda m: m[1] + "<MASKED>", text)
        text = LONGRUN.sub("<LONGNUM>", text)
        text = HEXBLOB.sub(lambda m: m.group(0) if m.group(0).isdigit() else "<HEX>", text)
        for kind, pattern in PATTERNS:
            text = pattern.sub(lambda m, k=kind: f"<{k}:{self.hash_of(k, m.group(0))}>", text)
        return text

    @staticmethod
    def hashes(masked: str) -> list[str]:
        """The identifier hashes in text that has been masked."""
        return list(dict.fromkeys(TOKEN.findall(masked)))

    def hashes_for(self, *values: str) -> list[str]:
        """The hashes a ticket's value (in each form it may be logged in) is searched by."""
        return self.hashes(self.mask(" ".join(values)))
