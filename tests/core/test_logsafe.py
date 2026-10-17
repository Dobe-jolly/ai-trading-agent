"""The service log never carries customer numbers or secrets. Invented values only."""
from __future__ import annotations

import io
import logging

from agentcore.logsafe import FORMAT, RedactingFormatter, redact


def test_long_numbers_keep_only_their_last_four_digits():
    line = redact("#700512: account 86000013101001, mobile 263771234567, card 4000123412341234, ref 612345678901")
    assert "86000013101001" not in line and "263771234567" not in line and "4000123412341234" not in line
    assert "…1001" in line and "…4567" in line and "…1234" in line and "…8901" in line
    assert "#700512" in line  # ticket numbers stay


def test_amounts_dates_and_short_ids_stay():
    line = redact("USD 1500.00 on 2026-10-12 10:00:00, decision 4123, 0.95 confidence, port 8085")
    assert line == "USD 1500.00 on 2026-10-12 10:00:00, decision 4123, 0.95 confidence, port 8085"


def test_secrets_are_replaced():
    line = redact('{"accessToken": "abc-123-secret", "uuid": "u1"} api_key=k_live_999 password: hunter22 '
                  "Authorization: Basic dXNlcjpwYXNz sk-ant-api03-AbCdEf123456")
    for secret in ("abc-123-secret", "k_live_999", "hunter22", "dXNlcjpwYXNz", "AbCdEf123456"):
        assert secret not in line
    assert '"uuid": "u1"' in line


def test_the_formatter_redacts_messages_arguments_and_tracebacks():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter(FORMAT))
    logger = logging.getLogger("test.logsafe")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("lookup for %s failed", "86000013101001")
        try:
            raise ValueError("bad account 86000099999999")
        except ValueError:
            logger.exception("boom")
    finally:
        logger.removeHandler(handler)
    out = stream.getvalue()
    assert "86000013101001" not in out and "86000099999999" not in out and "…1001" in out and "…9999" in out
