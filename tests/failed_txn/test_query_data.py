"""query_data on a failed-transaction ticket: only the ticket's own identifiers, masked results, recorded under a
query_id; counts never widen what may be searched; a failed exploratory query does not hold up the ticket. A close on
a query finding needs QUERY_CLOSE, a cited query matched on the ticket owner's own number, core banking's postings
checked, and a reply that agrees with them about the money. All data invented."""
from __future__ import annotations

from datetime import datetime

from agentcore.dataquery import DataQuery, Source
from agentcore.pii import Pseudonymizer
from failed_txn.decision import Decision
from failed_txn.guardrails import apply_guardrails
from failed_txn.tools import Toolbox
from failed_txn.normalize import now_local
from failed_txn.sources import CoreBanking
from tests.failed_txn.conftest import ACCOUNT, OTHER_ACCOUNT, STANDBY_DB
from tests.failed_txn.test_tools_and_guardrails import run, setup, state, ticket


class LogMb:
    """A Metabase that answers query_data from canned rows (and fails on request)."""

    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.sql = rows or [], fail, []

    def query(self, db, sql):
        from agentcore.metabase import MetabaseError
        self.sql.append(sql)
        if self.fail:
            raise MetabaseError("column \"x\" does not exist")
        return self.rows


def box(mb):
    zipit = Source(key="zipit_middleware", database="Postilion Billers", table="public.zipit", about="ZIPIT log.",
                   columns=["date_created", "amount", "status", "debit_account_error", "source_account",
                            "destination_account", "rrn"],
                   identifiers={"source_account": "account", "destination_account": "account", "rrn": "reference"},
                   time_column="date_created", max_days=31, order_by="date_created", db_id=77)
    return Toolbox(data=DataQuery(mb, {"zipit_middleware": zipit}))


def ask(mb, args, text=None):
    t, pseudo = ticket(text) if text else ticket(), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    b = box(mb)
    return t, ev, pseudo, b, run(b, "query_data", args, pseudo, ev, t)


def test_a_query_on_the_tickets_account_is_masked_and_recorded():
    mb = LogMb([{"date_created": datetime(2026, 7, 1, 9, 0), "amount": "20", "status": "FAILED",
                 "debit_account_error": "Insufficient funds", "source_account": ACCOUNT,
                 "destination_account": "12345678901", "rrn": "601234567890"}])
    t, ev, pseudo, b, out = ask(mb, {"source": "zipit_middleware", "match_column": ["source_account"],
                                     "match_value": "<NUMBER_1>"})
    assert out["query_id"] == "Q1" and out["rows"][0]["debit_account_error"] == "Insufficient funds"
    assert ACCOUNT not in str(out) and out["rows"][0]["source_account"] == "<NUMBER_1>"
    assert f"'{ACCOUNT}'" in mb.sql[0] and f"'{ACCOUNT.zfill(14)}'" in mb.sql[0]
    rec = ev.queries["Q1"]
    assert rec["owner_given"] is True and rec["rows"] == 1 and rec["kind"] == "account"
    # the destination account came back in a matched result, so it may be looked at next (as with the other lookups)
    dest = pseudo.mask("12345678901")
    again = run(b, "query_data", {"source": "zipit_middleware", "match_column": ["destination_account"],
                                  "match_value": dest}, pseudo, ev, t)
    assert again["query_id"] == "Q2" and ev.queries["Q2"]["owner_given"] is False


def test_another_customers_account_is_refused():
    mb = LogMb()
    t, pseudo = ticket(), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    other = pseudo.mask(OTHER_ACCOUNT)
    out = run(box(mb), "query_data", {"source": "zipit_middleware", "match_column": ["source_account"],
                                      "match_value": other}, pseudo, ev, t)
    assert isinstance(out, str) and "match_value (account) is not on this ticket" in out
    assert ev.refused == ["query_data account"] and mb.sql == []


def test_counts_over_everyone_never_widen_what_may_be_searched():
    mb = LogMb([{"status": "FAILED", "debit_account_error": f"Account {OTHER_ACCOUNT} not found", "n": 40}])
    t, ev, pseudo, b, out = ask(mb, {"source": "zipit_middleware", "mode": "count",
                                     "count_by": ["status", "debit_account_error"]})
    assert out["rows"][0]["n"] == 40 and OTHER_ACCOUNT not in str(out)
    assert OTHER_ACCOUNT.lstrip("0") not in ev.known.accounts
    assert ev.queries["Q1"]["mode"] == "count" and not ev.queries["Q1"]["match_columns"]


def test_a_failed_exploratory_query_does_not_hold_up_the_ticket():
    t, ev, pseudo, b, out = ask(LogMb(fail=True), {"source": "zipit_middleware", "match_column": ["rrn"],
                                                   "match_value": "<NUMBER_1>"},
                                text=f"ZIPIT failed, acc {ACCOUNT} RRN 601234567890")
    assert isinstance(out, str) and out.startswith("Query failed")
    assert ev.check_errors == [] and ev.unread and "zipit_middleware" in ev.unread[0]


def test_a_close_still_needs_a_confirmed_transaction_or_link():
    mb = LogMb([{"date_created": datetime(2026, 7, 1, 9, 0), "amount": "20", "status": "FAILED",
                 "debit_account_error": "Insufficient funds", "source_account": ACCOUNT}])
    t, ev, pseudo, b, out = ask(mb, {"source": "zipit_middleware", "match_column": ["source_account"],
                                     "match_value": "<NUMBER_1>"})
    d = Decision("reply_and_close", 0.95, "declined", "reason", ["Q1: the ZIPIT failed: insufficient funds"],
                 "The ZIPIT of 20.00 failed because of insufficient funds; no money left the account.")
    g = apply_guardrails(d, t, state(t), ev, pseudo, confidence_threshold=0.85, closes_so_far=0, max_closes=20,
                         agent_staff_id=525, topics=["Failed ZIPIT Transaction"])
    assert g.final.action == "escalate" and "no transaction or EcoCash link was confirmed" in g.notes[0]


def test_without_a_catalogue_there_is_no_query_tool():
    assert all(s["name"] != "query_data" for s in Toolbox(data=DataQuery(LogMb(), {})).schemas())
    assert any(s["name"] == "query_data" for s in box(LogMb()).schemas())


# ---- closing on a query finding (QUERY_CLOSE) -------------------------------------------------------------
FAILED_ROW = {"date_created": datetime(2026, 7, 1, 9, 0), "amount": "20", "status": "FAILED",
              "debit_account_error": "Insufficient funds", "source_account": ACCOUNT, "destination_account": "12345678901"}
NO_MONEY = ("The ZIPIT of 20.00 failed because the account had insufficient funds. No money left the account. Kindly "
            "advise the client to fund the account and try again.")


def close_on(mb, reply=NO_MONEY, cite=("Q1: the ZIPIT failed: insufficient funds",), postings=True, allow=True,
             match=("source_account", "<NUMBER_1>")):
    mb.core_accounts.setdefault(ACCOUNT, {"currency": "USD"})
    t, pseudo = ticket(), Pseudonymizer()
    ev = setup(mb, pseudo, t)
    b = Toolbox(core=CoreBanking(mb, STANDBY_DB), data=box(LogMb([FAILED_ROW])).data)
    if match[0] != "source_account":  # a number that came back in an earlier result, not one the branch gave
        run(b, "query_data", {"source": "zipit_middleware", "match_column": ["source_account"],
                              "match_value": "<NUMBER_1>"}, pseudo, ev, t)
        match = (match[0], pseudo.mask(match[1]))
    run(b, "query_data", {"source": "zipit_middleware", "match_column": [match[0]], "match_value": match[1]},
        pseudo, ev, t)
    if postings:
        run(b, "check_account_postings", {"account_number": "<NUMBER_1>"}, pseudo, ev, t)
    d = Decision("reply_and_close", 0.95, "insufficient_funds", "reason", list(cite), reply)
    g = apply_guardrails(d, t, state(t), ev, pseudo, confidence_threshold=0.85, closes_so_far=0, max_closes=20,
                         agent_staff_id=525, topics=["Failed ZIPIT Transaction"], allow_query_close=allow)
    return ev, g


def test_with_query_close_a_cited_finding_and_clean_postings_close_the_ticket(mb):
    ev, g = close_on(mb)
    assert g.final.action == "reply_and_close" and ev.query_close == ["Q1"]
    assert "closed on what query_data found (Q1 zipit_middleware), core banking postings checked" in g.notes


def test_query_close_is_off_unless_switched_on(mb):
    ev, g = close_on(mb, allow=False)
    assert g.final.action == "escalate" and "no transaction or EcoCash link was confirmed" in g.notes[0]


def test_a_finding_without_core_banking_postings_checked_does_not_close(mb):
    ev, g = close_on(mb, postings=False)
    assert g.final.action == "escalate" and "postings were not checked" in g.notes[0]


def test_no_money_left_is_refused_when_core_banking_shows_the_debit(mb):
    mb.add_posting(20, "D", "ZIPIT TO OTHER BANK")
    ev, g = close_on(mb)
    assert g.final.action == "escalate" and "shows a debit of 20.00" in g.notes[0]


def test_a_reversal_is_only_claimed_when_core_banking_shows_it(mb):
    reversed_reply = ("The ZIPIT of 20.00 failed at the receiving bank and the money was reversed to the client's "
                      "account. Kindly advise the client to try again.")
    ev, g = close_on(mb, reply=reversed_reply)
    assert g.final.action == "escalate" and "does not show a debit of 20.00 and its reversal" in g.notes[0]
    mb2 = type(mb)()
    mb2.add_posting(20, "D", "ZIPIT TO OTHER BANK")
    mb2.add_posting(20, "C", "REVERSAL ZIPIT TO OTHER BANK", when=now_local())  # the next day (same-day rows with one batch would be one row)
    ev, g = close_on(mb2, reply=reversed_reply)
    assert g.final.action == "reply_and_close"


def test_the_finding_must_be_cited_and_matched_on_the_owners_own_number(mb):
    ev, g = close_on(mb, cite=("the middleware says insufficient funds",))
    assert g.final.action == "escalate" and "no cited query" in g.notes[0]
    ev, g = close_on(type(mb)(), match=("destination_account", "12345678901"),
                     cite=("Q2: the ZIPIT to that account failed",))
    assert g.final.action == "escalate" and "no cited query" in g.notes[0]
