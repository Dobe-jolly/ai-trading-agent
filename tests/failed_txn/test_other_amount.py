"""The ticket's amount matches no attempt, but an attempt of another amount was declined: the reply says so openly
("We could not find an attempt of 250.00 ... We can see the ... of USD 50.00 ...") and may close. All data invented."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from failed_txn.normalize import now_local
from failed_txn.tools import mentions_amount
from tests.failed_txn.test_tools_and_guardrails import confirmed, guarded, run, toolbox

ASKED = "Client acc 86000013101001 did a ZIPIT of $250 yesterday and it failed"


def test_mentions_amount_accepts_the_usual_ways_of_writing_it():
    assert mentions_amount("an attempt of 250.00 on", Decimal("250")) and mentions_amount("USD 250 failed", Decimal("250"))
    assert mentions_amount("ZWG 1,500.00", Decimal("1500")) and mentions_amount("ZWG 1500", Decimal("1500"))
    assert mentions_amount("USD 20.50", Decimal("20.50")) and not mentions_amount("USD 20.00", Decimal("20.50"))
    assert not mentions_amount("USD 2500.00", Decimal("250")) and not mentions_amount("1250", Decimal("250"))


def test_the_standard_reply_says_the_tickets_amount_was_not_found_and_what_was(mb):
    mb.add_switch(40, rsp="51", rrn="612345678902", when=now_local() - timedelta(days=1, hours=3))
    t, ev, pseudo = confirmed(mb, rsp="51", text=ASKED)  # the USD 20.00 attempt
    out = run(toolbox(mb), "confirm_transaction", {"transaction_id": ev.subject_id}, pseudo, ev, t)
    reply = out["suggested_outcome"]["reply"]
    assert out["amount_matches_ticket"] == "no" and out["suggested_outcome"]["action"] == "reply_and_close"
    assert reply.startswith("We could not find an attempt of 250.00 on the client's account. We can see the ")
    assert "which was declined (insufficient funds), and no money left the client's account." in reply
    assert "1 other attempt on the account was declined with the same response code." in reply


def test_a_close_on_another_amount_rests_on_core_banking_not_on_the_wording(mb):
    t, ev, pseudo = confirmed(mb, rsp="51", text=ASKED)
    open_reply = ("We could not find an attempt of 250.00 on the client's account. We can see the ZIPIT of USD 20.00, "
                  "which was declined (insufficient funds), and no money left the client's account.")
    assert guarded(mb, t, ev, pseudo, reply=open_reply).final.action == "reply_and_close"
    shorter = "The ZIPIT was declined for insufficient funds and no money left the client's account."
    assert guarded(mb, t, ev, pseudo, reply=shorter).final.action == "reply_and_close"


def test_another_amount_is_not_closed_on_when_core_banking_shows_a_debit_of_the_tickets_amount(mb):
    mb.add_posting(250, "D", "ZIPIT TO OTHER BANK")
    t, ev, pseudo = confirmed(mb, rsp="51", text=ASKED)
    open_reply = ("We could not find an attempt of 250.00 on the client's account. We can see the ZIPIT of USD 20.00, "
                  "which was declined (insufficient funds).")
    g = guarded(mb, t, ev, pseudo, reply=open_reply)
    assert g.final.action == "escalate" and "core banking shows a debit of 250.00" in g.notes[0]


def test_only_a_decline_may_close_on_another_amount(mb):
    t, ev, pseudo = confirmed(mb, rsp="00", text=ASKED)  # succeeded, but USD 20.00, not 250
    reply = "We could not find an attempt of 250.00. The ZIPIT of USD 20.00 went through successfully."
    g = guarded(mb, t, ev, pseudo, reply=reply)
    assert g.final.action == "escalate" and "not the amount on the ticket" in g.notes[0]
