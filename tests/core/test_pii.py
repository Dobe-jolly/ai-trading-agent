"""Masking before anything reaches the model (agentcore.pii): numbers written in groups and IDs without the check
letter, as branches write them. Invented numbers only."""
from __future__ import annotations

import pytest

from agentcore.pii import TOKEN_RE, Pseudonymizer


@pytest.mark.parametrize("text,kind", [
    ("card 4000 1234 5678 9010 blocked", "NUMBER"),
    ("card 4000-1234-5678-9010 blocked", "NUMBER"),
    ("account 0012-3456-7890", "NUMBER"),
    ("RRN 123456 789012", "NUMBER"),
    ("acc 1234 5678", "NUMBER"),
    ("ID 63-1234567", "NATIONAL_ID"),
    ("ID 63-123456-78", "NATIONAL_ID"),
    ("ID 63-123456A78", "NATIONAL_ID"),
    ("ID-63-1234567B47 given", "NATIONAL_ID"),
    ("NID-63-1234567 given", "NATIONAL_ID"),
    ("account 1234567890123", "NUMBER"),
    ("mobile 0772 123 456", "MOBILE"),
])
def test_identifiers_as_branches_write_them_are_masked(text, kind):
    out = Pseudonymizer().mask(text)
    assert f"<{kind}_1>" in out and not any(c.isdigit() for c in TOKEN_RE.sub("", out))


@pytest.mark.parametrize("text", [
    "USD 1 500 000 was debited", "ZWG 125 000 000", "on 2026-10-26 at 16:37:18", "ref 2026-10", "code 0420",
    "amount 1,500.00", "between 10 and 20", "2 Oct 2026", "at 2026-10-26 16 37", "on 26 10 2026", "10 20 30 40 50",
    "USD 12 500 000",
])
def test_amounts_dates_and_codes_are_left_alone(text):
    assert Pseudonymizer().mask(text) == text


def test_an_id_shape_inside_a_longer_dashed_number_is_part_of_that_number():
    assert Pseudonymizer().mask("ref 0012-63-1234567") == "ref <NUMBER_1>"


def test_a_grouped_number_is_the_same_token_as_the_unbroken_one_and_unmasks_to_digits():
    pseudo = Pseudonymizer()
    out = pseudo.mask("card 4000 1234 5678 9010, again 4000123456789010, account 0012-3456-7890")
    assert out == "card <NUMBER_1>, again <NUMBER_1>, account <NUMBER_2>"
    assert pseudo.unmask("<NUMBER_1> <NUMBER_2>") == "4000123456789010 001234567890"


def test_unbroken_numbers_keep_their_canonical_form():
    """agentcore.logs hashes values by this form: an unbroken number must hash as before (no re-ingest)."""
    from agentcore.pii import _canonical

    assert _canonical("NUMBER", "0001234567890") == "1234567890"
    assert _canonical("NUMBER", "4000 1234 5678 9010") == _canonical("NUMBER", "4000123456789010")
