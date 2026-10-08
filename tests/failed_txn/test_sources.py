"""Reading the systems: Postilion legs, cents and reversal flags; CONECT and biller statuses; IB; core postings.
All data invented."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from failed_txn.normalize import find_amounts, now_local, parse_amount, parse_day
from failed_txn.osticket import find_details
from failed_txn.sources import (
    DECLINED, FAILED, REVERSAL_FAILED, REVERSAL_PENDING, REVERSED, SUCCEEDED, UNCERTAIN,
    Connect, CoreBanking, InternetBanking, Postilion, Txn, Window, connect_reading, debit_check, postilion_transactions,
)
from tests.failed_txn.conftest import ACCOUNT, BILLERS_DB, CONECT_DB, IB_DB, POSTOFFICE_DB, RRN, STANDBY_DB


def window(days=30):
    return Window.around(None, 3, days)


# ---- Postilion -------------------------------------------------------------------------------------------
def test_switch_legs_merge_and_cents_become_dollars(mb):
    mb.add_switch(20, rsp="00")
    txns = Postilion(mb, POSTOFFICE_DB).by_account(ACCOUNT, window())
    assert len(txns) == 1 and txns[0].amount == Decimal("20.00") and txns[0].status == SUCCEEDED
    assert txns[0].currency == "USD" and txns[0].reference == RRN and txns[0].raw["legs"] == 2


def test_reversal_flag_two_as_text_means_fully_reversed(mb):
    mb.add_switch(50, rsp="91", reversed_flag="2")
    txn = Postilion(mb, POSTOFFICE_DB).by_account(ACCOUNT, window())[0]
    assert txn.status == REVERSED and "fully reversed" in txn.reversal


def test_reversal_advice_folds_into_the_original(mb):
    mb.add_switch(15, rsp="00", legs=1)
    mb.add_switch(15, rsp="00", legs=1, message_type="0420")
    txns = Postilion(mb, POSTOFFICE_DB).by_account(ACCOUNT, window())
    assert len(txns) == 1 and txns[0].status == REVERSED and "reversal advice" in txns[0].reversal


def test_timeouts_are_uncertain_and_declines_say_why(mb):
    mb.add_switch(30, rsp="91", rrn="611111111111")
    mb.add_switch(40, rsp="61", rrn="622222222222")
    mb.add_switch(45, rsp="00", completed=0, rrn="633333333333")
    by_amount = {t.amount: t for t in Postilion(mb, POSTOFFICE_DB).by_account(ACCOUNT, window())}
    assert by_amount[Decimal("30.00")].status == UNCERTAIN
    assert by_amount[Decimal("40.00")].status == DECLINED and "amount limit" in by_amount[Decimal("40.00")].detail
    assert by_amount[Decimal("45.00")].status == UNCERTAIN  # approved code but the exchange never completed


def test_amount_filter_is_in_the_sql_in_cents(mb):
    mb.add_switch(20, rrn="611111111111")
    mb.add_switch(0.20, rrn="622222222222")  # 20 cents must not match a $20 claim
    txns = Postilion(mb, POSTOFFICE_DB).by_account(ACCOUNT, window(), Decimal("20"))
    assert [t.amount for t in txns] == [Decimal("20.00")]
    assert "tran_amount_req BETWEEN 1999 AND 2001" in mb.sql_log[-1][1]


def test_balance_inquiries_are_left_out_and_rrn_lookup_works(mb):
    mb.add_switch(0, tran_type="31", rrn="699999999999")
    mb.add_switch(25)
    assert len(Postilion(mb, POSTOFFICE_DB).by_account(ACCOUNT, window())) == 1
    assert Postilion(mb, POSTOFFICE_DB).by_reference(RRN)[0].amount == Decimal("25.00")


def test_zero_padded_and_short_accounts_both_match(mb):
    mb.add_switch(10, account="232728329")
    assert len(Postilion(mb, POSTOFFICE_DB).by_account("00000232728329", window())) == 1


# ---- CONECT and billers -----------------------------------------------------------------------------------
def test_connect_statuses():
    assert connect_reading({"status": "SUCCESS"})[0] == SUCCEEDED
    assert connect_reading({"status": "REVERSED"})[0] == REVERSED
    assert connect_reading({"status": "FLAG_REVERSAL"})[0] == REVERSAL_PENDING
    assert connect_reading({"status": "FLAGED_FOR_RECON"})[0] == REVERSAL_PENDING
    assert connect_reading({"status": "REVERSAL_FAILED"})[0] == REVERSAL_FAILED
    assert connect_reading({"status": "PENDING"})[0] == UNCERTAIN
    assert connect_reading({"status": "FAILED", "error_code": "51"})[0] == DECLINED
    assert connect_reading({"status": "FAILED", "error_code": "907"})[0] == FAILED
    assert connect_reading({"status": "FAILED", "error_code": "Read timed out"})[0] == UNCERTAIN
    assert connect_reading({"status": "FAILED", "error_code": "REVERSAL_SUCCESSFUL"})[0] == REVERSED


def test_airtime_searches_conect_and_every_network(mb):
    day = (now_local() - timedelta(days=1)).strftime("%Y-%m-%d 10:00:00")
    mb.add_connect("transactions", {"account_number": ACCOUNT, "operation": "AIRTIME_PURCHASE", "transaction_type": "X",
                                    "amount": "5.00", "status": "FAILED", "created_date": day})
    mb.add_connect("transactions", {"account_number": ACCOUNT, "operation": "IFT", "transaction_type": "IFT",
                                    "amount": "5.00", "status": "SUCCESS", "created_date": day})
    mb.add_connect("econet_payment", {"source_account": ACCOUNT, "amount": "5.00", "status": "FLAG_REVERSAL",
                                      "transaction_date": day, "mobile_number": "0771234567"}, db="billers")
    txns, unread = Connect(mb, CONECT_DB, BILLERS_DB).find(ACCOUNT, "airtime", window(), Decimal("5"))
    assert sorted(t.status for t in txns) == [FAILED, REVERSAL_PENDING] and not unread
    assert any(t.counterparty == "0771234567" for t in txns)


def test_a_table_that_cannot_be_read_is_reported_not_hidden(mb):
    mb.fail_patterns.append("FROM zimswitch_payment")
    txns, unread = Connect(mb, CONECT_DB, None).find(ACCOUNT, "zipit", window())
    assert any("zimswitch_payment" in u for u in unread) and txns == []


# ---- IB and core banking ------------------------------------------------------------------------------------
def test_internet_banking_reading(mb):
    day = (now_local() - timedelta(days=1)).strftime("%Y-%m-%d 09:00:00")
    mb.ib["rtgstransaction"] = [{"transaction_status": "INCOMPLETE", "is_txn_reversed": 0, "transaction_value": "300.00",
                                  "transaction_reference": "IBREF1", "transaction_system_reference": "SYS1",
                                  "account_identification1": ACCOUNT, "account_identification2": None,
                                  "beneficiary_account_number": "1234567890", "date_created": day}]
    txn = InternetBanking(mb, IB_DB).find(ACCOUNT, window())[0]
    assert txn.status == UNCERTAIN and txn.amount == Decimal("300.00") and txn.counterparty == "1234567890"


def test_debit_check_reads_refunds_by_narration(mb):
    when = now_local() - timedelta(days=1)
    mb.add_posting(20, "D", "ZIPIT TRF 612345678901", when)
    postings = CoreBanking(mb, STANDBY_DB).postings(ACCOUNT, Window.around(when.date(), 1, 7))
    assert debit_check(postings, Decimal("20"), when.date())["refunded"] == "no"
    mb.add_posting(20, "C", "SALARY ADVANCE", when + timedelta(days=1))
    postings = CoreBanking(mb, STANDBY_DB).postings(ACCOUNT, Window.around(when.date(), 1, 7))
    assert debit_check(postings, Decimal("20"), when.date())["refunded"] == "possibly"
    mb.add_posting(20, "C", "REV ZIPIT TRF 612345678901", when + timedelta(days=1))
    postings = CoreBanking(mb, STANDBY_DB).postings(ACCOUNT, Window.around(when.date(), 1, 7))
    assert debit_check(postings, Decimal("20"), when.date())["refunded"] == "yes"
    assert debit_check(postings, Decimal("99"), when.date())["debited"] == "no"
    assert CoreBanking(mb, STANDBY_DB).account(ACCOUNT)["currency"] == "USD"
    assert CoreBanking(mb, STANDBY_DB).account("86000000000000")["exists"] is False


def test_postilion_rows_without_rrn_are_kept_apart():
    rows = [{"post_tran_id": i, "message_type": "0200", "tran_completed": 1, "rsp_code_rsp": "00", "tran_reversed": "0",
             "tran_amount_req": "100", "tran_currency_code": "840", "datetime_tran_local": "2026-10-07 10:00:00",
             "retrieval_reference_nr": None, "system_trace_audit_nr": None, "from_account_id": ACCOUNT} for i in (1, 2)]
    assert len(postilion_transactions(rows)) == 2


# ---- what people write ---------------------------------------------------------------------------------------
def test_amounts_dates_and_references_as_branches_write_them():
    assert parse_amount("USD 1,500.00") == Decimal("1500.00")
    assert parse_amount("1.500,50") == Decimal("1500.50")
    assert parse_amount("ZWG1100") == Decimal("1100.00")
    assert parse_amount("12,5") == Decimal("12.50")
    assert find_amounts("tried $20 then USD 15.50 and zig 1 100; amount: 7") == {Decimal("20.00"), Decimal("15.50"),
                                                                                Decimal("1100.00"), Decimal("7.00")}
    assert find_amounts("account 86000013101001 on 07/10/2026") == set()
    assert parse_day("07/10/2026").isoformat() == "2026-10-07" and parse_day("2026-10-07 14:03").day == 7
    found = find_details("Client 0771234567 acc 86000013101001 did ZIPIT of $20 on 07/10/2026, RRN 612345678901")
    assert "86000013101001" in found.accounts and "612345678901" in found.references
    assert Decimal("20.00") in found.amounts and parse_day("07/10/2026") in found.dates
    assert "771234567" in found.mobiles


def test_txn_describe_reads_plainly():
    txn = Txn(id="T1", source="postilion", channel="card/switch transfer", when=now_local(), amount=Decimal("20"),
              currency="USD", account=ACCOUNT, status=DECLINED, detail="response 51 (insufficient funds)")
    out = txn.describe()
    assert out["amount"] == "USD 20.00" and out["reading"].startswith("declined")
