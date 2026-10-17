"""EcoCash wallet links from the bank's registration log (Ecocash Reg): the link in force per mobile and currency,
links to another or closed account, and what is never read. Also: zero-padded corporate account numbers on tickets.
All data invented."""
from __future__ import annotations

import json

from agentcore.pii import Pseudonymizer
from failed_txn.osticket import find_details
from failed_txn.sources import CoreBanking, EcoCashLinks
from failed_txn.tools import Toolbox
from tests.failed_txn.conftest import ACCOUNT, ECOCASH_REG_DB, OTHER_ACCOUNT, STANDBY_DB
from tests.failed_txn.test_tools_and_guardrails import run, setup, ticket

MOBILE = "263772000111"


def link(account, kind, when, msisdn=MOBILE, currency="840"):
    return {"account_id": account, "msisdn": msisdn, "service_type": kind, "currency_code": currency,
            "date_added": when, "first_name": "NEVER", "id_number": "NEVER"}


def box(mb):
    return Toolbox(core=CoreBanking(mb, STANDBY_DB), ecocash=EcoCashLinks(mb, ECOCASH_REG_DB))


def test_a_wallet_linked_to_another_account_is_shown_with_that_accounts_status(mb):
    mb.ecocash_links += [link(ACCOUNT, "BANKREG", "2025-03-01 10:00:00.123"),
                         link(OTHER_ACCOUNT, "REGISTRATION", "2026-05-02 09:30:00.12")]
    mb.core_accounts[OTHER_ACCOUNT] = {"currency": "USD", "flags": {"ACNTS_CLOSURE_DATE": "2026-06-01T00:00:00"}}
    t, pseudo = ticket(f"Client acc {ACCOUNT} failing B2W on EcoCash 0772000111"), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    out = run(box(mb), "find_ecocash_links", {"account_number": "<NUMBER_1>"}, pseudo, ev, t)
    wallet = out["wallets"][0]
    assert wallet["linked_now"] is True and wallet["since"] == "02-05-2026" and wallet["currency"] == "USD"
    assert [h["event"] for h in wallet["history"]] == ["linked", "linked (earlier method)"]
    assert wallet["account_status"] == ["the account was closed on 01-06-2026"]
    assert out["account"]["ever_linked"] is True and out["account"]["linked_now_to"] == []
    text = json.dumps(out)
    assert "NEVER" not in text and MOBILE not in text and OTHER_ACCOUNT not in text  # masked; names never read
    sql = [q for db, q in mb.sql_log if db == ECOCASH_REG_DB][0]
    assert "first_name" not in sql and "id_number" not in sql and "TRIM(account_id) IN (" in sql


def test_a_delinked_wallet_and_an_account_with_no_link(mb):
    mb.ecocash_links += [link(ACCOUNT, "REGISTRATION", "2026-01-05 08:00:00.1"),
                         link(ACCOUNT, "DEREGISTRATION", "2026-04-07 12:00:00.1")]
    t, pseudo = ticket(f"Client acc {ACCOUNT} on 0772000111 failing W2B"), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    out = run(box(mb), "find_ecocash_links", {"mobile": "<MOBILE_1>"}, pseudo, ev, t)
    assert out["wallets"][0]["linked_now"] is False and out["wallets"][0]["delinked_on"] == "07-04-2026"
    t2 = ticket(f"Client acc {OTHER_ACCOUNT} failing B2W")
    ev2 = setup(mb, pseudo, t2)
    none = run(box(mb), "find_ecocash_links", {"account_number": "<NUMBER_2>"}, pseudo, ev2, t2)
    assert none["wallets"] == [] and none["note"] == "No EcoCash link found for this account."


def test_a_mobile_that_is_not_on_the_ticket_is_refused(mb):
    t, pseudo = ticket(), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    refused = run(box(mb), "find_ecocash_links", {"mobile": "0779999999"}, pseudo, ev, t)
    assert "not on this ticket" in refused and ev.refused == ["mobile"]


def test_zero_padded_corporate_account_numbers_count_as_accounts():
    assert find_details("SME ACCOUNT FAILING TO PURCHASE AIRTIME - 00000021269522").accounts == {"21269522"}
    assert find_details("call 0772123456").accounts == set() and find_details("ref 0012345").accounts == set()


# ---- confirming the link ------------------------------------------------------------------------------------
from datetime import date  # noqa: E402

from tests.failed_txn.test_tools_and_guardrails import guarded  # noqa: E402


def confirm(mb, text, args):
    t, pseudo = ticket(text), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    b = box(mb)
    out = run(b, "confirm_ecocash_link", args, pseudo, ev, t)
    return t, ev, pseudo, out


def test_an_account_with_no_link_closes_and_the_wording_is_the_agents(mb):
    t, ev, pseudo, out = confirm(mb, f"Client acc {ACCOUNT} failing B2W", {"account_number": "<NUMBER_1>"})
    assert out["where_this_leaves_the_client"] == "not_linked"
    assert out["suggested_outcome"]["reply"] == "Kindly advise the client to link the account for B2W or W2B services."
    assert ev.statuses == {"ecocash_link": "not_linked"}  # a link lookup is not a transaction search
    reply = out["suggested_outcome"]["reply"]
    assert guarded(mb, t, ev, pseudo, reply=reply).final.action == "reply_and_close"
    own = ("The client's EcoCash wallet is not linked to account <NUMBER_1>. Kindly assist the client to register the "
           "account on EcoCash, then try the transfer again.")
    assert guarded(mb, t, ev, pseudo, reply=own).final.action == "reply_and_close"  # no wording rule in code


def test_a_wallet_linked_to_another_account_names_it_in_the_reply(mb):
    mb.ecocash_links += [link(OTHER_ACCOUNT, "REGISTRATION", "2026-02-02 09:30:00.12")]
    t, ev, pseudo, out = confirm(mb, f"Client acc {ACCOUNT} on 0772000111 failing B2W",
                                 {"account_number": "<NUMBER_1>", "mobile": "<MOBILE_1>"})
    assert out["where_this_leaves_the_client"] == "linked_elsewhere"
    assert OTHER_ACCOUNT not in json.dumps(out)  # the model sees it masked; the branch gets the real number
    assert pseudo.unmask(out["suggested_outcome"]["reply"]).startswith(
        f"Kindly note that the linked account is {OTHER_ACCOUNT}. Kindly delink that account")
    assert guarded(mb, t, ev, pseudo, reply="Kindly delink the other account and link the new one.").final.action \
        == "reply_and_close"


def test_a_closed_linked_account_and_the_link_as_it_stood_on_the_tickets_day(mb):
    mb.ecocash_links += [link(OTHER_ACCOUNT, "REGISTRATION", "2025-02-02 09:30:00.12")]
    mb.core_accounts[OTHER_ACCOUNT] = {"currency": "USD", "flags": {"ACNTS_CLOSURE_DATE": "2026-03-01T00:00:00"}}
    _, _, _, out = confirm(mb, f"Client acc {ACCOUNT} on 0772000111 failing B2W", {"mobile": "<MOBILE_1>"})
    assert out["where_this_leaves_the_client"] == "linked_account_closed"
    replay_box = box(mb)
    replay_box.as_of = date(2026, 2, 1)  # before the account closed: it was simply linked elsewhere then
    t, pseudo = ticket(f"Client acc {ACCOUNT} on 0772000111 failing B2W"), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    then = run(replay_box, "confirm_ecocash_link", {"mobile": "<MOBILE_1>"}, pseudo, ev, t)
    assert then["where_this_leaves_the_client"] == "linked_elsewhere"
    replay_box.as_of = date(2025, 1, 1)  # before the link existed
    assert run(replay_box, "confirm_ecocash_link", {"mobile": "<MOBILE_1>"}, pseudo, ev, t)["where_this_leaves_the_client"] \
        == "not_linked"


def test_a_good_link_is_not_closed_on(mb):
    mb.ecocash_links += [link(ACCOUNT, "REGISTRATION", "2026-02-02 09:30:00.12")]
    t, ev, pseudo, out = confirm(mb, f"Client acc {ACCOUNT} failing B2W", {"account_number": "<NUMBER_1>"})
    assert out["where_this_leaves_the_client"] == "linked_ok" and "look for the transaction" in out["note"]
    g = guarded(mb, t, ev, pseudo, reply="Kindly link the account.")
    assert g.final.action == "escalate" and "the link is linked_ok" in g.notes[0]


def test_a_link_close_needs_the_owners_account_or_mobile(mb):
    t, pseudo = ticket(f"Client acc {ACCOUNT} failing B2W"), Pseudonymizer()
    from failed_txn.osticket import ThreadEntry
    t.entries.append(ThreadEntry(id=1002, type="N", staff_id=7, poster="Staff", body="mobile 0772000111", created=t.created))
    ev = setup(mb, pseudo, t)
    run(box(mb), "confirm_ecocash_link", {"mobile": "<MOBILE_1>"}, pseudo, ev, t)  # from a staff note only
    g = guarded(mb, t, ev, pseudo, reply="Kindly advise the client to link the account for B2W or W2B services.")
    assert g.final.action == "escalate" and "not for an account or mobile the ticket owner gave" in g.notes[0]


def test_wallets_that_differ_are_described_and_closed_for_the_branch_to_decide(mb):
    mb.core_accounts[OTHER_ACCOUNT] = {"currency": "USD", "flags": {"ACNTS_DORMANT_ACNT": "1"}}
    mb.ecocash_links += [link(ACCOUNT, "REGISTRATION", "2026-05-29 09:00:00.1", currency="924"),
                         link(OTHER_ACCOUNT, "BANKREG", "2025-01-10 09:00:00.1", currency="840")]
    t, ev, pseudo, out = confirm(mb, f"Client acc {ACCOUNT} on 0772000111 failing B2W",
                                 {"account_number": "<NUMBER_1>", "mobile": "<MOBILE_1>"})
    assert out["where_this_leaves_the_client"] == "links_differ"
    reply = pseudo.unmask(out["suggested_outcome"]["reply"])
    assert reply == ("Kindly note the EcoCash links we can see: "
                     f"0772000111 (USD) is linked to account {OTHER_ACCOUNT} (the account is dormant) since 10-01-2025; "
                     f"0772000111 (ZWG) is linked to account {ACCOUNT} (the account on this ticket) since 29-05-2026. "
                     "Kindly delink the wrong link and advise the client to register with the correct account.")
    assert len(out["what_we_see"]) == 2
    g = guarded(mb, t, ev, pseudo, reply=out["suggested_outcome"]["reply"])
    assert g.final.action == "reply_and_close"


def test_one_link_and_no_account_on_the_ticket_goes_to_a_person(mb):
    mb.ecocash_links += [link(OTHER_ACCOUNT, "REGISTRATION", "2026-02-02 09:30:00.12")]
    t, ev, pseudo, out = confirm(mb, "Client on 0772000111 failing B2W", {"mobile": "<MOBILE_1>"})
    assert out["where_this_leaves_the_client"] == "linked_no_account_given" and out["needs_a_person"] is True
    assert out["what_we_see"] and out["suggested_outcome"]["action"] == "escalate"


def test_two_accounts_linked_and_no_account_on_the_ticket_are_described(mb):
    third = "01120000333333"
    mb.core_accounts[OTHER_ACCOUNT] = {"currency": "USD"}
    mb.core_accounts[third] = {"currency": "ZWG"}
    mb.ecocash_links += [link(OTHER_ACCOUNT, "REGISTRATION", "2026-02-02 09:30:00.12"),
                         link(third, "REGISTRATION", "2026-03-03 09:30:00.12", currency="924")]
    t, ev, pseudo, out = confirm(mb, "Client on 0772000111 failing W2B on EcoCash", {"mobile": "<MOBILE_1>"})
    assert out["where_this_leaves_the_client"] == "links_differ"
    reply = pseudo.unmask(out["suggested_outcome"]["reply"])
    assert OTHER_ACCOUNT in reply and third in reply and "the account on this ticket" not in reply


def test_an_account_core_banking_does_not_know_is_not_told_to_link(mb):
    unknown = "86000077777777"
    t, ev, pseudo, out = confirm(mb, f"Client acc {unknown} gets no universal account on W2B", {"account_number": "<NUMBER_1>"})
    assert out["where_this_leaves_the_client"] == "account_not_found"
    assert pseudo.unmask(out["suggested_outcome"]["reply"]) == ("Kindly advise the client to confirm the account number. It "
                                                               f"is failing to validate: account {unknown} does not exist in "
                                                               "core banking.")
    assert guarded(mb, t, ev, pseudo, reply=out["suggested_outcome"]["reply"]).final.action == "reply_and_close"


def test_core_banking_finds_an_account_whatever_padding_the_link_log_kept(mb):
    short = ACCOUNT.lstrip("0")[1:]  # a 13-digit form, as about 27,000 rows of the link log hold accounts
    mb.core_accounts["0" + short] = {"currency": "USD"}
    found = CoreBanking(mb, STANDBY_DB).account(short)
    assert found["exists"] is True
    sql = [q for _, q in mb.sql_log if "IACLINK_ACTUAL_ACNUM IN (" in q][-1]
    assert f"'{short}'" in sql and f"'{short.zfill(14)}'" in sql


def test_a_zipit_ticket_is_never_closed_on_the_ecocash_link(mb):
    t, ev, pseudo, out = confirm(mb, f"Client acc {ACCOUNT} gets no universal account on ZIPIT", {"account_number": "<NUMBER_1>"})
    assert out["where_this_leaves_the_client"] == "not_linked"
    g = guarded(mb, t, ev, pseudo, reply=out["suggested_outcome"]["reply"])
    assert g.final.action == "escalate" and "this ticket is not about EcoCash" in g.notes[0]
