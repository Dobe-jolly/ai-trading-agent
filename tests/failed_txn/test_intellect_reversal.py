"""A switch transaction Postilion reports reversed is checked against core banking (Intellect) before the desk tells
the branch the money is back: Postilion's flag only says it sent a reversal. Intellect may have refused it (for
example when the reversal reached it before the original, after a Postilion stall), so the client stays debited.
All data invented."""
from __future__ import annotations

from datetime import datetime

from failed_txn.sources import REVERSAL_FAILED, REVERSED, CoreBanking, intellect_reversal
from tests.failed_txn.conftest import RRN, STANDBY_DB
from tests.failed_txn.test_tools_and_guardrails import confirmed, guarded, run, toolbox


def row(mti, serial, rc, error=None, branch="10"):
    return {"ASIIN_MTI": mti, "ASIIN_BRN_CODE": branch, "ASIIN_SERIAL": serial, "ASIOUT_MTI": None,
            "ASIOUT_FIELD_039": rc, "ERR_DESC": error}


def answer(mti, serial, rc, error=None, branch="10"):
    return {"mti": mti, "branch": branch, "serial": serial, "rc": rc, "error": error}


# ---- the reading ------------------------------------------------------------------------------------------
def test_a_transaction_intellect_never_saw_or_did_not_approve_needs_no_reversal_there():
    assert intellect_reversal([])["reversed_at_intellect"] == "not needed"
    out = intellect_reversal([answer("0200", 5, "51"), answer("0420", 6, "25")])
    assert out["reversed_at_intellect"] == "not needed" and "did not approve it (51)" in out["why"]


def test_an_approved_transaction_whose_reversal_intellect_accepted_is_reversed():
    out = intellect_reversal([answer("0200", 5, "00"), answer("0420", 9, "00")])
    assert out["reversed_at_intellect"] == "yes"


def test_an_approved_transaction_without_a_reversal_at_intellect_leaves_the_debit():
    out = intellect_reversal([answer("0200", 5, "00")])
    assert out["reversed_at_intellect"] == "no" and "never received a reversal" in out["why"]


def test_a_reversal_that_reached_intellect_before_the_original_is_explained():
    out = intellect_reversal([answer("0420", 12344, "25"), answer("0200", 12358, "00")])
    assert out["reversed_at_intellect"] == "no"
    assert "refused the reversal (response 25)" in out["why"] and "before the original" in out["why"]


def test_a_reversal_intellect_failed_to_match_carries_its_error_and_no_ordering_claim():
    error = 'FN_GET_ORG_TRN ORA-00904: "X1": invalid identifier'
    out = intellect_reversal([answer("0200", 469, "00", branch="4"), answer("0420", 472, "25", error, branch="4")])
    assert out["reversed_at_intellect"] == "no" and error in out["why"] and "before the original" not in out["why"]
    # serials of different branch codes are different sequences: no ordering is claimed across them
    out = intellect_reversal([answer("0420", 3, "25", branch="4"), answer("0200", 900, "00", branch="10")])
    assert "before the original" not in out["why"]


# ---- the query --------------------------------------------------------------------------------------------
def test_the_answers_query_takes_only_an_rrn_and_never_reads_the_raw_message():
    class Mb:
        def query(self, db, sql):
            self.sql = sql
            return [row("200", "425", " 00 "), row("0420", "426", "25", "  FN_GET_ORG_TRN   ORA-00933 ")]
    mb = Mb()
    out = CoreBanking(mb, STANDBY_DB).switch_answers(RRN, datetime(2026, 1, 16).date())
    assert out == [answer("0200", 425, "00"), answer("0420", 426, "25", "FN_GET_ORG_TRN ORA-00933")]
    assert f"A.ASIIN_FIELD_037 = '{RRN}'" in mb.sql and "ASIOUTNP" in mb.sql
    assert "ASIIN_MESSAGE" not in mb.sql and "ASIOUT_MESSAGE" not in mb.sql
    try:
        CoreBanking(mb, STANDBY_DB).switch_answers("1' OR '1'='1", datetime(2026, 1, 16).date())
    except ValueError:
        pass
    else:
        raise AssertionError("an RRN with quotes must be refused")


# ---- confirm_transaction and the guardrails ---------------------------------------------------------------
def confirm_again(mb, t, ev, pseudo):
    return run(toolbox(mb), "confirm_transaction", {"transaction_id": ev.subject_id}, pseudo, ev, t)


def test_a_reversal_intellect_refused_is_not_closed_as_money_back(mb):
    mb.asi_answers[RRN] = [row("0420", 12344, "25"), row("0200", 12358, "00")]
    t, ev, pseudo = confirmed(mb, rsp="91", reversed_flag="2")
    out = confirm_again(mb, t, ev, pseudo)
    assert out["where_this_leaves_the_client"] == REVERSAL_FAILED and out["needs_a_person"] is True
    assert out["core_banking_reversal"]["reversed_at_intellect"] == "no"
    assert "before the original" in out["confirmed"]["detail"]
    assert out["suggested_outcome"]["action"] == "escalate"
    g = guarded(mb, t, ev, pseudo, reply="The ZIPIT did not go through and was reversed, so the money is back.")
    assert g.final.action == "escalate"
    assert len([q for db, q in mb.sql_log if "ASIOUTNP" in q]) == 1  # read once, even when confirmed twice


def test_a_reversal_intellect_accepted_still_closes(mb):
    mb.asi_answers[RRN] = [row("0200", 5, "00"), row("0420", 9, "00")]
    t, ev, pseudo = confirmed(mb, rsp="91", reversed_flag="2")
    out = confirm_again(mb, t, ev, pseudo)
    assert out["where_this_leaves_the_client"] == REVERSED and "needs_a_person" not in out
    assert out["core_banking_reversal"]["reversed_at_intellect"] == "yes"
    assert out["suggested_outcome"]["action"] == "reply_and_close"


def test_a_reversed_transaction_that_never_reached_intellect_still_closes(mb):
    t, ev, pseudo = confirmed(mb, rsp="91", reversed_flag="2")  # no ASI rows for this RRN
    out = confirm_again(mb, t, ev, pseudo)
    assert out["where_this_leaves_the_client"] == REVERSED
    assert out["core_banking_reversal"]["reversed_at_intellect"] == "not needed"


def test_unreadable_answers_do_not_block_the_ticket(mb):
    mb.fail_patterns.append("ASIOUTNP")
    t, ev, pseudo = confirmed(mb, rsp="91", reversed_flag="2")
    out = confirm_again(mb, t, ev, pseudo)
    assert out["core_banking_reversal"] == "could not be read" and out["where_this_leaves_the_client"] == REVERSED


def test_core_banking_showing_the_debit_and_nothing_back_overrides_a_reversed_flag(mb):
    mb.fail_patterns.append("ASIOUTNP")  # even without Intellect's answers
    mb.add_posting(20, "D", "ZIPIT SEND 612345678901")
    t, ev, pseudo = confirmed(mb, rsp="91", reversed_flag="2")
    out = confirm_again(mb, t, ev, pseudo)
    assert out["core_banking"]["debited"] == "yes" and out["core_banking"]["refunded"] == "no"
    assert out["where_this_leaves_the_client"] == REVERSAL_FAILED and out["needs_a_person"] is True


def test_a_reversed_flag_with_the_refund_in_core_banking_closes(mb):
    mb.add_posting(20, "D", "ZIPIT SEND 612345678901")
    mb.add_posting(20, "C", "REV ZIPIT SEND 612345678901")
    t, ev, pseudo = confirmed(mb, rsp="91", reversed_flag="2")
    out = confirm_again(mb, t, ev, pseudo)
    assert out["where_this_leaves_the_client"] == REVERSED and "needs_a_person" not in out
