"""Core banking's own error for a switch transaction it declined (Intellect's ASI interface log, by RRN), the
account's restrictions on a response 57, Postilion's limits on a 61, and the switch's reason in the reply. What the
helpdesk does about each is in the desk's staff notes, not here. All data invented."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from agentcore.metabase import MetabaseError
from failed_txn.sources import DECLINED, CoreBanking, Txn
from failed_txn.tools import Evidence, Toolbox, standard_reply
from tests.failed_txn.conftest import RRN, STANDBY_DB
from tests.failed_txn.test_tools_and_guardrails import confirmed, guarded, run, toolbox

LIMIT = "Transaction Limit for Cash Withdrawals exceeded for the month Limit of 300"


def test_a_core_banking_decline_gets_core_bankings_own_reason(mb):
    mb.asi_errors[RRN] = [LIMIT, LIMIT]
    t, ev, pseudo = confirmed(mb, rsp="61")
    out = run(toolbox(mb), "confirm_transaction", {"transaction_id": ev.subject_id}, pseudo, ev, t)
    assert out["core_banking_error"] == [LIMIT] and "needs_a_person" not in out
    assert f'core banking says "{LIMIT}"' in out["confirmed"]["detail"]
    reply = out["suggested_outcome"]["reply"]
    assert f"was declined ({LIMIT})" in reply and "smaller amount" not in reply
    asi = [q for db, q in mb.sql_log if "ASIERRLOG" in q]
    assert len(asi) == 1  # read once, even when confirmed twice
    assert f"A.ASIIN_FIELD_037 = '{RRN}'" in asi[0] and "A.ASIIN_ENTITY_NUM = '1'" in asi[0]
    assert "H.ERR_IN_SERIAL = A.ASIIN_SERIAL" in asi[0] and asi[0].count("TO_DATE(") == 2
    assert guarded(mb, t, ev, pseudo).final.action == "reply_and_close"


def test_response_57_without_a_core_banking_reason_shows_the_accounts_restrictions(mb):
    t, ev, pseudo = confirmed(mb, rsp="57")
    out = run(toolbox(mb), "confirm_transaction", {"transaction_id": ev.subject_id}, pseudo, ev, t)
    assert out["account_restrictions"] == "could not be read"  # this account's flags are not in the fixture
    assert "core_banking_error" not in out and "needs_a_person" not in out and "check" not in out


def test_response_57_with_a_core_banking_reason_needs_no_extra_check(mb):
    mb.asi_errors[RRN] = ["Account is dormant"]
    t, ev, pseudo = confirmed(mb, rsp="57")
    out = run(toolbox(mb), "confirm_transaction", {"transaction_id": ev.subject_id}, pseudo, ev, t)
    assert out["core_banking_error"] == ["Account is dormant"] and "check" not in out
    assert "was declined (Account is dormant)" in out["suggested_outcome"]["reply"]


def test_an_unreadable_error_log_does_not_block_the_ticket(mb):
    mb.fail_patterns.append("ASIERRLOG")
    t, ev, pseudo = confirmed(mb, rsp="61")
    out = run(toolbox(mb), "confirm_transaction", {"transaction_id": ev.subject_id}, pseudo, ev, t)
    assert out["core_banking_error"] == "could not be read" and not ev.check_errors
    assert out["postilion_limit_hit"] == "Postilion gave no limit details for it"


def test_a_limit_decline_core_banking_did_not_log_says_no_limit_was_clearly_hit(mb):
    t, ev, pseudo = confirmed(mb, rsp="61")  # no error logged in core banking for this RRN
    out = run(toolbox(mb), "confirm_transaction", {"transaction_id": ev.subject_id}, pseudo, ev, t)
    assert out["postilion_limit_hit"] == "Postilion gave no limit details for it" and "core_banking_error" not in out
    assert "needs_a_person" not in out


class Core:
    def __init__(self, fail=False):
        self.asked, self.fail = [], fail

    def switch_errors(self, rrn, day):
        self.asked.append((rrn, day))
        if self.fail:
            raise MetabaseError("no table")
        return []


def txn(sink, status=DECLINED, rsp="05"):
    return Txn(id="T1", source="postilion", channel="Zipit Send", when=datetime(2026, 1, 16, 10, 28),
               amount=Decimal("20"), currency="USD", account="1", status=status, detail=f"response {rsp} (x)",
               reference=RRN, raw={"rsp": rsp, "sink": sink})


def test_only_transactions_that_reached_core_banking_are_looked_up():
    core = Core()
    box = Toolbox(core=core)
    assert box._core_error(txn("NmbZswAcq")) is None and not core.asked  # declined at ZimSwitch
    assert box._core_error(txn("NmbIntellect")) is None and core.asked == [(RRN, datetime(2026, 1, 16).date())]
    assert box._core_error(txn("NmbIntellect", status="succeeded")) is None and len(core.asked) == 1
    assert Toolbox(core=Core(fail=True))._core_error(txn(None)) == "could not be read"


def test_the_error_log_query_takes_only_an_rrn():
    class Mb:
        def query(self, db, sql):
            self.sql = sql
            return [{"ERR_DESC": "  Limit   exceeded "}, {"ERR_DESC": None}]
    mb = Mb()
    assert CoreBanking(mb, STANDBY_DB).switch_errors(RRN, datetime(2026, 1, 16).date()) == ["Limit exceeded"]
    assert "BETWEEN TO_DATE('15-01-2026','DD-MM-YYYY') AND TO_DATE('17-01-2026','DD-MM-YYYY')" in mb.sql
    try:
        CoreBanking(mb, STANDBY_DB).switch_errors("1' OR '1'='1", datetime(2026, 1, 16).date())
    except ValueError:
        pass
    else:
        raise AssertionError("an RRN with quotes must be refused")


def test_the_switchs_reason_is_the_reason_in_the_reply():
    t = txn("NmbZswAcq", rsp="30")
    t.reason = "Sender's card not found"
    action, reply = standard_reply(t, None, DECLINED)
    assert action == "reply_and_close" and "was declined (Sender's card not found)" in reply
    assert reply.endswith("no money left the client's account. ")  # what to do about it: a staff note, not code
    assert "The client can try again" not in reply
    assert Evidence().status == "none"


CLEAR = {"ACNTS_ATM_OPERN": "1", "ACNTS_MBLBNK_OPERN": "1", "ACNTS_INET_OPERN": "0", "ACNTS_DORMANT_ACNT": "0",
         "ACNTS_INOP_ACNT": "0", "ACNTS_DB_FREEZED": "0", "ACNTS_CR_FREEZED": "0", "ACNTS_CLOSURE_DATE": None}


def with_flags(mb, **changes):
    from tests.failed_txn.conftest import ACCOUNT
    mb.core_accounts[ACCOUNT] = {"currency": "USD", "flags": dict(CLEAR, **changes)}


def test_response_57_on_a_dormant_account_says_so_and_needs_no_person(mb):
    with_flags(mb, ACNTS_DORMANT_ACNT="1")
    t, ev, pseudo = confirmed(mb, rsp="57")
    out = run(toolbox(mb), "confirm_transaction", {"transaction_id": ev.subject_id}, pseudo, ev, t)
    assert out["account_restrictions"] == ["the account is dormant"] and "needs_a_person" not in out
    assert "was declined (the account is dormant)" in out["suggested_outcome"]["reply"]
    acnts = [q for db, q in mb.sql_log if "ACNTS_ATM_OPERN" in q][0]
    assert "ACNTS_AC_NAME" not in acnts and "ADDR" not in acnts  # no name or address is read


def test_response_57_with_atm_use_off_or_a_closed_account(mb):
    with_flags(mb, ACNTS_ATM_OPERN="N", ACNTS_CLOSURE_DATE="2026-03-01T00:00:00")
    t, ev, pseudo = confirmed(mb, rsp="57")
    out = run(toolbox(mb), "confirm_transaction", {"transaction_id": ev.subject_id}, pseudo, ev, t)
    assert out["account_restrictions"] == ["the account was closed on 01-03-2026", "ATM use is not enabled on the account"]


def test_response_57_on_a_clear_account_says_what_was_checked(mb):
    with_flags(mb)
    t, ev, pseudo = confirmed(mb, rsp="57")
    out = run(toolbox(mb), "confirm_transaction", {"transaction_id": ev.subject_id}, pseudo, ev, t)
    assert out["account_restrictions"] == ("none found: ATM use enabled, not dormant, inoperative, frozen or closed")
    assert "check" not in out


def test_the_account_lookup_shows_its_status_and_permissions(mb):
    with_flags(mb, ACNTS_DB_FREEZED="1")
    t, ev, pseudo = confirmed(mb, rsp="51")
    out = run(toolbox(mb), "check_account_postings", {"account_number": "<NUMBER_1>"}, pseudo, ev, t)
    status = out["account"]["status"]
    assert status["atm_enabled"] is True and status["debits_frozen"] is True and status["closed_on"] is None
    assert "mobile_banking_enabled" not in status and "internet_banking_enabled" not in status
    assert out["account"]["restrictions"] == ["debits are frozen on the account"]


def test_a_limit_decline_core_banking_did_not_log_lists_postilions_limits():
    from failed_txn.tools import Evidence
    from tests.failed_txn.test_postilion_legs import LIMITS, leg, structured
    from failed_txn.sources import postilion_transactions
    t = postilion_transactions([dict(leg("61", sink="NmbIntellect"), structured_data_rsp=structured(
        RemainingAdvancedLimits=LIMITS))])[0]
    t.id = "T1"
    ev = Evidence()
    ev.txns["T1"] = t
    out = Toolbox(core=Core())._confirm("T1", ev, None)
    assert out["postilion_limit_hit"].startswith("Postilion's limit B2W_DAILY") and "needs_a_person" not in out
    assert out["postilion_limits"][0].startswith("B2W_DAILY (Daily, Account): per transaction USD 100.00")
    assert "was declined (Postilion's limit B2W_DAILY: USD 500.00 per Daily, USD 0.00 left)" in out["suggested_outcome"]["reply"]
    roomy = LIMITS.replace("<RemOnlineTotalLimit>0<", "<RemOnlineTotalLimit>50000<")  # nothing used up
    t2 = postilion_transactions([dict(leg("61", sink="NmbIntellect"), structured_data_rsp=structured(
        RemainingAdvancedLimits=roomy))])[0]
    t2.id = "T1"
    ev2 = Evidence()
    ev2.txns["T1"] = t2
    out2 = Toolbox(core=Core())._confirm("T1", ev2, None)
    assert out2["postilion_limit_hit"] == "none of Postilion's limits on the card clearly stopped it"
    assert "needs_a_person" not in out2
