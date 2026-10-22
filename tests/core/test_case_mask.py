"""One-way masking of past cases (agentcore.cases.mask). Invented tickets, names and numbers only."""
from __future__ import annotations

import pytest

from agentcore.cases.mask import CaseMasker
from agentcore.pii import TOKEN_RE, Pseudonymizer

FIRST_MESSAGE = """Dear Team,

Client Tendai Moyo, account 1234567890123, mobile 0772 123 456, ID 63-123456A78,
email t.moyo@example.com reports a ZIPIT of 150.00 debited on 2026-08-14 but not received.
Card 4000123412341234 was also blocked.

Kind regards
Rudo Chikore
Branch Services, Avondale
+263 77 765 4321"""

STAFF_REPLY = """Good morning Rudo,

The ZIPIT failed at the switch and a 0420 reversal was posted the same day. TENDAI should see the funds back.

Thanks
Farai"""


def masker():
    return CaseMasker(customers=["Tendai Moyo"], staff=["Rudo Chikore", "Farai Ndlovu", None, "  "])


# ---- nothing that can be looked up survives -------------------------------------------------------------
def test_numbers_become_placeholders_not_tokens():
    out = masker().mask(FIRST_MESSAGE)
    for raw in ("1234567890123", "0772 123 456", "63-123456A78", "t.moyo@example.com", "4000123412341234"):
        assert raw not in out
    assert "[NUMBER]" in out and "[MOBILE]" in out and "[NATIONAL_ID]" in out and "[EMAIL]" in out
    assert not TOKEN_RE.search(out)


def test_a_masked_case_gives_the_ticket_pseudonymizer_nothing_to_issue():
    """The point of one-way masking: shown to a desk, a case adds no token its lookups would accept."""
    pseudo = Pseudonymizer()
    case = masker().mask(FIRST_MESSAGE) + "\n" + masker().mask(STAFF_REPLY)
    assert pseudo.mask(case) == case
    assert pseudo.token_count == 0
    assert pseudo.unmask("[NUMBER] [MOBILE]") == "[NUMBER] [MOBILE]"


def test_tokens_from_the_decisions_log_become_placeholders():
    logged = "Checked <NUMBER_1> for <MOBILE_2>; owner <EMAIL_1>, ID <NATIONAL_ID_1>. Reversal found."
    out = CaseMasker().mask(logged)
    assert out == "Checked [NUMBER] for [MOBILE]; owner [EMAIL], ID [NATIONAL_ID]. Reversal found."
    assert not TOKEN_RE.search(out)


def test_amounts_dates_and_short_codes_stay():
    out = masker().mask(FIRST_MESSAGE) + "\n" + masker().mask(STAFF_REPLY)  # each field on its own, as stored
    for kept in ("150.00", "2026-08-14", "0420", "ZIPIT"):
        assert kept in out


# ---- names ----------------------------------------------------------------------------------------------
def test_known_names_go_in_any_form():
    out = masker().mask("Client Tendai Moyo called. tendai   moyo again. TENDAI said Moyo-Ncube is his sister. "
                        "Rudo escalated.")
    assert "Tendai" not in out and "tendai" not in out and "TENDAI" not in out
    assert "Rudo" not in out
    assert out.startswith("Client [CUSTOMER] called. [CUSTOMER] again. [CUSTOMER] said Moyo-Ncube")
    assert "[STAFF] escalated." in out


def test_a_name_part_in_lower_case_is_an_ordinary_word():
    out = CaseMasker(customers=["Gift Banda"]).mask("Gift asked why the gift voucher failed.")
    assert out == "[CUSTOMER] asked why the gift voucher failed."


def test_short_name_parts_and_initials_are_not_masked_alone():
    out = CaseMasker(customers=["T Jo Moyo"]).mask("T Jo Moyo: Jo is a word here, T too.")
    assert out == "[CUSTOMER]: Jo is a word here, T too."


def test_a_name_never_matches_inside_a_placeholder():
    out = CaseMasker(customers=["Number Mobile"], staff=["Staff Customer"]).mask("Account 1234567890, 0772123456.")
    assert out == "Account [NUMBER], [MOBILE]."


# ---- greetings and signatures ---------------------------------------------------------------------------
def test_greeting_and_signature_lines_are_dropped():
    out = masker().mask(FIRST_MESSAGE)
    assert not out.startswith("Dear")
    assert "Avondale" not in out and "Branch Services" not in out and "regards" not in out.lower()
    reply = masker().mask(STAFF_REPLY)
    assert reply == ("The ZIPIT failed at the switch and a 0420 reversal was posted the same day. "
                     "[CUSTOMER] should see the funds back.")


@pytest.mark.parametrize("line", ["Dear Sir, my account was debited twice.", "Hi team, please check the OTP.",
                                  "Thanks to the branch the card was replaced."])
def test_a_greeting_or_thanks_that_carries_the_request_is_kept(line):
    assert CaseMasker().mask(line) == line


def test_empty_text():
    assert CaseMasker().mask(None) == ""
    assert CaseMasker().mask("") == ""
