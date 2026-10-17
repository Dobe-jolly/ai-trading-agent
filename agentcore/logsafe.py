"""Keep customer numbers and secrets out of the service log (journald keeps it for weeks, and it is read by people
who have no reason to see them).

Every line the agents write goes through RedactingFormatter: runs of 9 to 19 digits (account, card, mobile and ID
numbers, references) keep only their last four digits, and the values of tokens, keys, passwords and Authorization
headers are replaced. Ticket numbers (6 digits), amounts and dates are left alone. Applied to tracebacks too.
"""
from __future__ import annotations

import logging
import re

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DIGITS = re.compile(r"(?<![\d.])\d{9,19}(?![\d.]\d)")
SECRET = re.compile(
    r"(?i)(\"?\b(?:access_?token|accesstoken|api[_-]?key|x-api-key|password|passwd|secret|client_secret|token)\b\"?"
    r"\s*[:=]\s*\"?)([^\s\"',;&}]{3,})")
AUTH_HEADER = re.compile(r"(?i)\b(authorization\"?\s*[:=]\s*\"?(?:basic|bearer)\s+)[A-Za-z0-9+/=._~-]+")
ANTHROPIC_KEY = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")


def redact(text: str) -> str:
    text = ANTHROPIC_KEY.sub("sk-ant-***", text)
    text = AUTH_HEADER.sub(r"\1***", text)
    text = SECRET.sub(r"\1***", text)
    return DIGITS.sub(lambda m: "…" + m.group(0)[-4:], text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup(verbose: bool = False, quiet: tuple[str, ...] = ("httpx", "httpcore", "anthropic")) -> None:
    """logging.basicConfig with every handler on the root logger redacting."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=FORMAT)
    for handler in logging.getLogger().handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(FORMAT))
    for noisy in quiet:
        logging.getLogger(noisy).setLevel(logging.WARNING)
