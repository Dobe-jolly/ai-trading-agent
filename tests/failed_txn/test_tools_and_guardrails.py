"""The lookups as the model calls them (masking, who may be looked up, confirming) and the rules in code that
decide whether a reply may close the ticket. All data invented."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from agentcore.pii import Pseudonymizer
from failed_txn.decision import Decision
from failed_txn.guardrails import apply_guardrails
from failed_txn.normalize import now_local
from failed_txn.osticket import Ticket, ThreadEntry, TicketState
from failed_txn.sources import Connect, CoreBanking, InternetBanking, Postilion
from failed_txn.tools import Evidence, Toolbox, standard_reply
from tests.failed_txn.conftest import (
    ACCOUNT, BILLERS_DB, CONECT_DB, IB_DB, OTHER_ACCOUNT, POSTOFFICE_DB, RRN, STANDBY_DB,
)


def toolbox(mb, core=True):
    return Toolbox(Postilion(mb, POSTOFFICE_DB), Connect(mb, CONECT_DB, BILLERS_DB), InternetBanking(mb, IB_DB),
                   CoreBanking(mb, STANDBY_DB) if core else None)


def ticket(text="Client acc 86000013101001 did a ZIPIT of $20 yesterday and it failed", topic="Failed ZIPIT Transaction"):
    t = Ticket(ticket_id=1, number="100001", help_topic=topic, created=(now_local() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
               thread_id=50001)
    t.entries = [ThreadEntry(id=1001, type="M", staff_id=None, poster="Branch", body=text, created=t.created)]
    return t


def state(t):
    return TicketState(ticket_id=t.ticket_id, status_state="open", status_name="Open",
                       help_topic=t.help_topic, last_entry_id=t.last_entry_id)


def run(box, name, args, pseudo, ev, t):
    text, error = box.run(name, args, pseudo, ev, t)
    return json.loads(text) if not error else text


def setup(mb, pseudo, t):
    pseudo.mask(t.all_text())
    return Evidence()


# ---- lookups -------------------------------------------------------------------------------------------
def test_results_are_masked_and_only_the_tickets_accounts_may_be_searched(mb):
    mb.add_switch(20, rsp="51")
    t, pseudo = ticket(), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    box = toolbox(mb)
    out = run(box, "find_switch_transactions", {"account_number": "<NUMBER_1>", "amount": "20"}, pseudo, ev, t)
    assert out["found"] == 1 and out["transactions"][0]["status"] == "declined"
    assert "86000013101001" not in json.dumps(out) and RRN not in json.dumps(out)
    refused = run(box, "find_switch_transactions", {"account_number": OTHER_ACCOUNT}, pseudo, ev, t)
    assert "not on this ticket" in refused and ev.refused == ["account_number"]
    assert "not a value from this ticket" in run(box, "find_switch_transactions", {"account_number": "<NUMBER_9>"},
                                                 pseudo, ev, t)


def test_confirming_a_failed_transaction_checks_core_banking(mb):
    day = now_local() - timedelta(days=1)
    mb.add_connect("transactions", {"account_number": ACCOUNT, "transaction_type": "ZIPIT", "operation": "ZIPIT",
                                    "amount": "20.00", "status": "FAILED", "error_code": "907",
                                    "created_date": day.strftime("%Y-%m-%d 10:00:00")})
    t, pseudo = ticket(), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    box = toolbox(mb)
    found = run(box, "find_nmb_connect_transactions", {"account_number": "<NUMBER_1>", "kind": "zipit"}, pseudo, ev, t)
    tid = found["transactions"][0]["id"]
    out = run(box, "confirm_transaction", {"transaction_id": tid}, pseudo, ev, t)
    assert out["where_this_leaves_the_client"] == "failed_no_debit" and out["core_banking"]["debited"] == "no"
    assert out["suggested_outcome"]["action"] == "reply_and_close" and out["amount_matches_ticket"] == "yes"
    mb.add_posting(20, "D", "NMBCONNECT ZIPIT", day)
    out = run(box, "confirm_transaction", {"transaction_id": tid}, pseudo, ev, t)
    assert out["where_this_leaves_the_client"] == "failed" and out["suggested_outcome"]["action"] == "escalate"
    mb.add_posting(20, "C", "REVERSAL NMBCONNECT ZIPIT", day + timedelta(hours=5))
    out = run(box, "confirm_transaction", {"transaction_id": tid}, pseudo, ev, t)
    assert out["where_this_leaves_the_client"] == "failed_refunded"


def test_same_amount_rivals_are_flagged(mb):
    noon = (now_local() - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)  # same day at any hour
    mb.add_switch(20, rsp="00", rrn="611111111111", when=noon)
    mb.add_switch(20, rsp="91", rrn="622222222222", when=noon + timedelta(hours=1))
    t, pseudo = ticket(), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    box = toolbox(mb)
    found = run(box, "find_switch_transactions", {"account_number": "<NUMBER_1>", "amount": "20"}, pseudo, ev, t)
    ok = next(x["id"] for x in found["transactions"] if x["status"] == "succeeded")
    out = run(box, "confirm_transaction", {"transaction_id": ok}, pseudo, ev, t)
    assert "caution" in out and ev.same_amount_rivals()


def test_unknown_transaction_ids_are_refused(mb):
    t, pseudo = ticket(), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    assert "not a transaction id" in run(toolbox(mb), "confirm_transaction", {"transaction_id": "T7"}, pseudo, ev, t)


def test_standard_replies():
    assert standard_reply(None, None, "not_found")[0] == "request_info"
    assert standard_reply(None, None, "none")[0] == "request_info"


# ---- guardrails -----------------------------------------------------------------------------------------
def guarded(mb, t, ev, pseudo, action="reply_and_close", reply="The ZIPIT was declined and no money left the "
                                                                 "client's account. The client can try again.", conf=0.95):
    d = Decision(action, conf, "x", "reason", [], reply)
    return apply_guardrails(d, t, state(t), ev, pseudo, confidence_threshold=0.85, closes_so_far=0, max_closes=20,
                            agent_staff_id=525, topics=["Failed ZIPIT Transaction"])


def confirmed(mb, rsp="51", reversed_flag="0", text=None):
    mb.add_switch(20, rsp=rsp, reversed_flag=reversed_flag)
    t, pseudo = ticket(text) if text else ticket(), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    box = toolbox(mb)
    found = run(box, "find_switch_transactions", {"account_number": "<NUMBER_1>"}, pseudo, ev, t)
    run(box, "confirm_transaction", {"transaction_id": found["transactions"][0]["id"]}, pseudo, ev, t)
    return t, ev, pseudo


def test_a_close_needs_a_confirmed_transaction(mb):
    t, pseudo = ticket(), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    g = guarded(mb, t, ev, pseudo)
    assert g.final.action == "escalate" and "no transaction or EcoCash link was confirmed" in g.notes[0]


def test_a_declined_transaction_may_close(mb):
    t, ev, pseudo = confirmed(mb, rsp="51")
    assert guarded(mb, t, ev, pseudo).final.action == "reply_and_close"


@pytest.mark.parametrize("rsp,reversed_flag,expect", [("91", "0", "uncertain"), ("00", "1", "uncertain")])
def test_uncertain_outcomes_never_close(mb, rsp, reversed_flag, expect):
    t, ev, pseudo = confirmed(mb, rsp=rsp, reversed_flag=reversed_flag)
    g = guarded(mb, t, ev, pseudo)
    assert g.final.action == "escalate" and expect in g.notes[0]


def test_the_reply_must_say_what_the_evidence_says(mb):
    t, ev, pseudo = confirmed(mb, rsp="51")
    g = guarded(mb, t, ev, pseudo, reply="The ZIPIT failed and was reversed, so the money is back in the account.")
    assert g.final.action == "escalate" and "came back" in g.notes[0]
    t, ev, pseudo = confirmed(mb, rsp="91", reversed_flag="2")
    assert guarded(mb, t, ev, pseudo, reply="The ZIPIT did not go through and was reversed successfully, so the money "
                                            "is back in the client's account.").final.action == "reply_and_close"
    g = guarded(mb, t, ev, pseudo, reply="The ZIPIT went through and the recipient should have it. Kindly advise.")
    assert g.final.action == "escalate" and "went through" in g.notes[0]


def test_another_amount_than_the_tickets_needs_core_banking_to_show_no_debit_of_it(mb):
    mb.add_posting(35, "D", "ZIPIT TO OTHER BANK")
    t, ev, pseudo = confirmed(mb, rsp="51", text="Client acc 86000013101001 ZIPIT of USD 35 failed")
    g = guarded(mb, t, ev, pseudo)
    assert g.final.action == "escalate" and "not the amount on the ticket" in g.notes[0] and "35.00" in g.notes[0]


def test_declined_but_debited_needs_a_person(mb):
    t, ev, pseudo = confirmed(mb, rsp="51")
    mb.add_posting(20, "D", "ZIPIT", now_local() - timedelta(days=1, hours=2))
    toolbox(mb).run("confirm_transaction", {"transaction_id": ev.subject_id}, pseudo, ev, t)
    g = guarded(mb, t, ev, pseudo)
    assert g.final.action == "escalate" and "debited" in g.notes[0]


def test_a_failed_lookup_defers_the_close(mb):
    t, ev, pseudo = confirmed(mb, rsp="51")
    ev.check_errors.append("find_nmb_connect_transactions: timeout")
    g = guarded(mb, t, ev, pseudo)
    assert g.final.action == "no_action" and g.deferred
