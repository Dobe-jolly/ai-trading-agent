"""A CIF (customer number) instead of an account number: found on the ticket, turned into the client's accounts in
core banking, searched, and accepted as the owner's when the ticket is closed. All data invented."""
from __future__ import annotations

import json
from datetime import timedelta

from agentcore.pii import Pseudonymizer
from failed_txn.loop import render_ticket
from failed_txn.normalize import now_local
from failed_txn.osticket import Ticket, ThreadEntry, find_details, subject_cifs
from failed_txn.tools import standard_reply
from failed_txn.sources import DECLINED, Txn
from tests.failed_txn.conftest import ACCOUNT, OTHER_ACCOUNT
from tests.failed_txn.test_tools_and_guardrails import guarded, run, setup, toolbox

CIF = "191565"


def cif_ticket(subject="LCT client failing to process ZIPIT 00191565", body="Kindly assist, the client gets Do Not Honor."):
    t = Ticket(ticket_id=1, number="100001", help_topic="Failed ZIPIT Transaction", subject=subject,
               created=(now_local() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"), thread_id=50001)
    t.entries = [ThreadEntry(id=1001, type="M", staff_id=None, poster="Branch", body=body, created=t.created)]
    return t


def test_cifs_are_found_labelled_anywhere_or_bare_in_the_subject():
    assert find_details("Client CIF is 191565, failing ZIPIT").cifs == {CIF}
    assert find_details("customer number: 0191565").cifs == {CIF}
    assert find_details("paid 191565 dollars").cifs == set()  # a bare number in a message is not a CIF
    assert subject_cifs("ZIPIT failing 00191565") == {CIF}
    assert subject_cifs("ZIPIT failing 0772123456") == set() and subject_cifs("ZIPIT 772123456") == set()  # mobiles
    assert subject_cifs("ZIPIT 86000013101001 USD 20.00 on 12/06/2026") == set()  # account, amount, date
    t = cif_ticket()
    assert t.owner_details().cifs == {CIF} and "CIF / customer numbers: 191565" in render_ticket(t)


def test_a_cif_on_the_ticket_gives_the_clients_accounts_and_they_can_be_searched_and_closed_on(mb):
    mb.clients[CIF] = [ACCOUNT]
    mb.core_accounts[ACCOUNT] = {"currency": "USD", "flags": {"ACNTS_ATM_OPERN": "1", "ACNTS_DORMANT_ACNT": "0"}}
    mb.add_switch(20, rsp="51")
    t, pseudo = cif_ticket(), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    box = toolbox(mb)
    out = run(box, "find_client_accounts", {"cif": CIF}, pseudo, ev, t)
    assert len(out["accounts"]) == 1 and out["accounts"][0]["status"]["atm_enabled"] is True
    assert ACCOUNT not in json.dumps(out)  # account numbers reach the model masked
    token = out["accounts"][0]["account_number"]
    found = run(box, "find_switch_transactions", {"account_number": token}, pseudo, ev, t)
    assert found["found"] == 1 and found["transactions"][0]["status"] == "declined"
    run(box, "confirm_transaction", {"transaction_id": found["transactions"][0]["id"]}, pseudo, ev, t)
    assert guarded(mb, t, ev, pseudo).final.action == "reply_and_close"
    sql = [q for _, q in mb.sql_log if "ACNTS_CLIENT_NUM" in q][0]
    assert "A.ACNTS_CLIENT_NUM = 191565" in sql and "NAME" not in sql and "ADDR" not in sql


def test_a_cif_that_is_not_on_the_ticket_is_refused(mb):
    t, pseudo = cif_ticket(), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    refused = run(toolbox(mb), "find_client_accounts", {"cif": "999999"}, pseudo, ev, t)
    assert "not on this ticket" in refused and ev.refused == ["cif"]


def test_a_cif_from_a_staff_note_does_not_make_the_accounts_the_owners(mb):
    mb.clients[CIF] = [OTHER_ACCOUNT]
    t, pseudo = cif_ticket(subject="ZIPIT failing"), Pseudonymizer()
    t.entries.append(ThreadEntry(id=1002, type="N", staff_id=7, poster="Staff", body="CIF 191565", created=t.created))
    ev = setup(mb, pseudo, t)
    out = run(toolbox(mb), "find_client_accounts", {"cif": CIF}, pseudo, ev, t)
    assert len(out["accounts"]) == 1 and not ev.client_accounts


def test_a_number_that_is_no_cif_is_tried_as_an_account(mb):
    short = "231010019"  # a 9-digit account number where a CIF would go
    t, pseudo = cif_ticket(subject=f"ZIPIT failing {short}"), Pseudonymizer()
    mb.core_accounts[short] = {"currency": "USD"}
    ev = setup(mb, pseudo, t)
    out = run(toolbox(mb), "find_client_accounts", {"cif": short}, pseudo, ev, t)
    assert out["note"] == "This number is an account number, not a CIF." and len(out["accounts"]) == 1
    none = cif_ticket(subject="ZIPIT failing 1234567")
    ev2 = setup(mb, pseudo, none)
    assert "No client with this CIF" in run(toolbox(mb), "find_client_accounts", {"cif": "1234567"}, pseudo, ev2, none)["note"]


def test_a_specific_reason_gets_no_generic_advice_in_the_suggestion():
    """What to do about a limit of zero is a staff note now (knowledge_seed.json), not code: the suggestion gives
    core banking's reason and leaves the advice to the agent, instead of a generic 'try a smaller amount'."""
    txn = Txn(id="T1", source="postilion", channel="Bank to Wallet", when=now_local(), amount=None, currency="USD",
              account="1", status=DECLINED, detail="response 61 (x)", raw={"rsp": "61"},
              reason="Transaction Limit for Transfer debits exceeded for the day Limit of 0")
    _, reply = standard_reply(txn, None, DECLINED)
    assert "was declined (Transaction Limit for Transfer debits exceeded for the day Limit of 0)" in reply
    assert "smaller amount" not in reply and "special limit" not in reply
    txn.reason = None
    assert "exceeded a transaction limit" in standard_reply(txn, None, DECLINED)[1]
