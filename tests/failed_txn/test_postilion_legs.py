"""Postilion read through the bank's own report query (post_tran_leg): transaction names from the node map, the
switch-processed codes, reversals kept, and the SQL it sends. All data invented."""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from failed_txn.sources import (DECLINED, FAILED, REVERSED, SUCCEEDED, UNCERTAIN, Postilion, Window,
                                parse_structured, postilion_transactions, switch_says, txn_name)
from tests.failed_txn.conftest import ACCOUNT, RRN


def leg(rsp="00", *, tran_type="50", message_type="0200", source="NmbZswIss", sink="NMBIntellect", cents="2000",
        reversed_flag="0", rrn=RRN, merchant=None, tran_nr=1):
    return {"datetime_req": "2026-10-12 10:15:00", "tran_nr": tran_nr, "tran_type": tran_type,
            "message_type": message_type, "rsp_code_rsp": rsp, "source_node_name": source, "sink_node_name": sink,
            "system_trace_audit_nr": "123456", "retrieval_reference_nr": rrn, "tran_currency_code": "840",
            "settle_amount_req": cents, "from_account_id": ACCOUNT, "to_account_id": None,
            "card_acceptor_name_loc": merchant, "tran_reversed": reversed_flag}


def test_the_bank_map_names_transactions_whatever_the_case_or_padding():
    assert txn_name("50", "0200", "NmbZswIss", "NMBIntellect") == "Zipit Send"
    assert txn_name("27", 200, "nmbecocashsc", "NmbIntellect") == "Wallet to Bank"
    assert txn_name("00", "0220", "EFTHostedSrc", "NmbIntellect") == "POS Purchase - Them on Us"
    assert txn_name("39", "0100", "NmbAtmSrc", "VisaAcq") == "ATM Visa Acquiring"
    assert txn_name("40", "0200", "SomeOtherSrc", "NmbIntellect") is None


@pytest.mark.parametrize("rsp, status, words", [
    ("00", SUCCEEDED, None),
    ("51", DECLINED, "business decline"),          # insufficient funds: the switch processed it
    ("55", DECLINED, "business decline"),          # wrong PIN
    ("06", FAILED, "technical failure"),           # not in the bank's processed list: a node or link failed
    ("92", FAILED, "technical failure"),
    ("91", UNCERTAIN, None),                       # no answer: the client may have been debited
])
def test_switch_processed_codes_separate_business_declines_from_technical_failures(rsp, status, words):
    txn = postilion_transactions([leg(rsp)])[0]
    assert txn.status == status and txn.channel == "Zipit Send"
    assert words is None or words in txn.detail
    assert "via NmbZswIss to NMBIntellect" in txn.detail and txn.amount == Decimal("20.00")


def test_a_reversal_advice_as_a_number_still_folds_into_the_original():
    rows = [leg("00", tran_nr=1), leg("00", message_type=420, tran_nr=2)]
    txns = postilion_transactions(rows)
    assert len(txns) == 1 and txns[0].status == REVERSED and txns[0].channel == "Zipit Send"
    assert postilion_transactions([leg("91", reversed_flag="2")])[0].status == REVERSED


def test_pos_rows_carry_the_merchant():
    txn = postilion_transactions([leg("00", tran_type="00", source="EFTHostedSrc", sink="NmbZswAcq",
                                      merchant="OK MART      HARARE       ZW")])[0]
    assert txn.channel == "POS Purchase - Them on Us" and txn.detail.endswith("at OK MART HARARE ZW")


class Recorder:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def query(self, db, sql):
        self.sql.append(sql)
        return self.rows


def test_the_leg_query_reads_only_the_report_columns_and_keeps_reversals():
    mb = Recorder([leg("51")])
    source = Postilion(mb, 31, table="post_tran_leg")
    txns = source.by_account(ACCOUNT, Window.around(None, 3, 30), Decimal("20"))
    sql = mb.sql[0]
    assert "FROM post_tran_leg WITH (NOLOCK)" in sql and "tran_postilion_originated = '0'" in sql
    assert "tran_type NOT LIKE '3%'" in sql and "settle_amount_req BETWEEN 1999 AND 2001" in sql
    assert "datetime_req >=" in sql and "ORDER BY datetime_req DESC" in sql
    assert "tran_reversed <> '2'" not in sql and not re.search(r"\bpan\b", sql)  # reversals kept; no card numbers
    assert txns[0].status == DECLINED
    source.by_reference(RRN)
    assert f"retrieval_reference_nr = '{RRN}'" in mb.sql[1]
    with pytest.raises(ValueError):
        Postilion(mb, 31, table="post_tran; drop")


def item(text):
    size = str(len(text))
    return f"{len(size)}{size}{text}"


def structured(**pairs):
    return "".join(item(k.replace("__", ":")) + item(v) for k, v in pairs.items())


def test_the_switch_reason_is_read_and_the_personal_fields_beside_it_are_dropped():
    data = structured(Reason="Sender's card not found", SenderFirstName="TESTNAME", SenderLastName="TESTSURNAME",
                      SenderNationalId="99-000000X99", SenderMobileNumber="263770000001", ReceiverBank="ECOCASH",
                      Postilion__MetaData=structured(Reason="1"))
    txn = postilion_transactions([dict(leg("30", sink="NmbZswAcq"), structured_data_rsp=data, extended_rsp_code=None)])[0]
    assert "response 30 (format error); the switch says \"Sender's card not found\"; receiving bank ECOCASH" in txn.detail
    assert txn.status == DECLINED
    text = str(txn.describe())
    assert not any(p in text for p in ("TESTNAME", "TESTSURNAME", "99-000000X99", "263770000001", "MetaData"))


def test_extended_codes_and_ecocash_errors_come_from_any_leg_and_bad_data_is_ignored():
    first = dict(leg("91", sink="NmbIntellect", tran_nr=1), structured_data_rsp="not structured", extended_rsp_code=" ")
    second = dict(leg("91", sink="NmbIntellect", tran_nr=2),
                  structured_data_rsp=structured(Postilion__ExtendedResponseCode="P050", EcocashErrorCode="20503",
                                                 EcocashErrorDesc="Customer   already linked"),
                  extended_rsp_code="P050")
    txn = postilion_transactions([first, second])[0]
    assert 'EcoCash error 20503: "Customer already linked"' in txn.detail
    assert "Postilion extended response code P050" in txn.detail and txn.status == UNCERTAIN
    assert parse_structured("15ab") is None and parse_structured(None) is None
    assert switch_says([leg("00")]) == []  # rows without the columns (older reads) say nothing extra


def test_the_queries_read_the_switch_reason_columns_on_both_tables():
    for table in ("post_tran_leg", "post_tran"):
        mb = Recorder([])
        Postilion(mb, 31, table=table).by_reference(RRN)
        assert "extended_rsp_code, structured_data_rsp" in mb.sql[0] and not re.search(r"\bpan\b", mb.sql[0])


LIMITS = ("<Limits><Limit><LimitName>B2W_DAILY</LimitName><Period>Daily</Period><Association>Account</Association>"
          "<OnlinePerTransactionAmountLimit>10000</OnlinePerTransactionAmountLimit><OnlineTotalLimit>50000</OnlineTotalLimit>"
          "<RemOnlineTotalLimit>0</RemOnlineTotalLimit></Limit>"
          "<Limit><LimitName>B2W_MONTHLY</LimitName><Period>Monthly</Period></Limit></Limits>")


def test_postilions_own_limits_are_read_on_a_limit_decline_only():
    from failed_txn.sources import advanced_limits, describe_limit
    limits = advanced_limits(LIMITS)
    assert [x["LimitName"] for x in limits] == ["B2W_DAILY", "B2W_MONTHLY"]
    assert describe_limit(limits[0], "USD") == ("B2W_DAILY (Daily, Account): per transaction USD 100.00, total USD 500.00, "
                                                "remaining USD 0.00")  # Postilion holds amounts in cents
    data = structured(RemainingAdvancedLimits=LIMITS, SenderFirstName="TESTNAME")
    txn = postilion_transactions([dict(leg("61"), structured_data_rsp=data, extended_rsp_code="P173")])[0]
    assert "Postilion's limits on the card: B2W_DAILY (Daily, Account): per transaction USD 100.00" in txn.detail
    assert txn.reason == "Postilion's limit B2W_DAILY: USD 500.00 per Daily, USD 0.00 left"  # USD 20.00 > 0.00 left
    assert txn.raw["limits"][0]["LimitName"] == "B2W_DAILY" and "TESTNAME" not in str(txn.describe())
    declined = postilion_transactions([dict(leg("51"), structured_data_rsp=data)])[0]
    assert "limits" not in declined.detail and declined.raw["limits"] is None  # not shown on other declines
    assert advanced_limits("<Limits/>") == [] and advanced_limits(None) == []


def test_the_limit_that_was_hit_is_picked_by_currency_count_and_amount():
    from decimal import Decimal
    from failed_txn.sources import limit_hit
    atm = {"LimitName": "ATM Withdrawal Daily USD1", "Period": "23 hours", "OnlinePerTransactionAmountLimit": "100000",
           "OnlineTotalLimit": "100000", "RemOnlineTotalLimit": "100000", "OnlineNrTransLimit": "10",
           "RemOnlineNrTransLimit": "10"}
    tap_zwg = {"LimitName": "Contacless without PIN ZWG", "Period": "24 hours", "OnlinePerTransactionAmountLimit": "34400",
               "OnlineTotalLimit": "103100", "RemOnlineTotalLimit": "103100", "OnlineNrTransLimit": "3",
               "RemOnlineNrTransLimit": "0"}
    tap_usd = dict(tap_zwg, LimitName="Contacless without PIN USD", OnlinePerTransactionAmountLimit="2500",
                   OnlineTotalLimit="7500", RemOnlineTotalLimit="6300", RemOnlineNrTransLimit="2")
    assert limit_hit([atm], Decimal("50"), "USD") is None  # nothing used up: not this limit
    assert limit_hit([atm, tap_zwg], Decimal("20"), "ZWG") == ("Postilion's limit Contacless without PIN ZWG: 3 transactions "
                                                              "allowed per 24 hours, none left")
    assert limit_hit([tap_zwg, tap_usd], Decimal("30"), "USD") == ("Postilion's limit Contacless without PIN USD: at most "
                                                                  "USD 25.00 per transaction")
    assert limit_hit([dict(atm, OnlineNrTransLimit="999999999999", RemOnlineNrTransLimit="999999999999")],
                     Decimal("50"), "USD") is None  # 12-digit counts mean no count limit
