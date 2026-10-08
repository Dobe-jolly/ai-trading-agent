"""The ported SMS checks (status for status) and the osTicket write path."""
from __future__ import annotations

from datetime import date

import pytest

from agent.delivery import SocketDeliveryChecker
from agent.metabase import MetabaseError
from agent.osticket import ESCALATION_NOTE_MARKER, OsTicket, html_body
from agent.sms_checks import ESCALATING_STATUSES, Databases, SmsChecks
from tests.conftest import AGENT_STAFF, CUSTOMERS_DB, DISPATCH_DB, SOCKET_DB, STANDBY_DB, TICKET_DB, FakeMetabase

ACCT = "00012345678901"
AS_OF = date(2026, 10, 7)


def make_checks(mb: FakeMetabase, **kw) -> SmsChecks:
    socket = SocketDeliveryChecker(mb, window_minutes=60, dispatch_lag_minutes=1080)  # type: ignore[arg-type]
    return SmsChecks(mb, Databases(STANDBY_DB, CUSTOMERS_DB, SOCKET_DB, DISPATCH_DB), socket, **kw)  # type: ignore[arg-type]


def debit(mb: FakeMetabase, phone: str = "0772123456") -> dict:
    return make_checks(mb).check_account(ACCT, ["DEBITS"], phone, as_of=AS_OF)


def test_invalid_account(mb):
    assert debit(mb)["status"] == "invalid_account_number"


def test_account_sms_flag_and_registration(mb):
    mb.add_account(ACCT, sms_oper="0")
    assert debit(mb)["status"] == "sms_not_enabled"
    mb.add_account(ACCT, sms_oper="maybe")
    assert debit(mb)["status"] == "sms_activation_unknown"
    mb.add_account(ACCT, cob=[{"COBICADDR_MOB_NO": "0772123456", "COBICADDR_SMS_ALERT": "N"}])
    result = debit(mb)
    assert result["status"] == "sms_registration_disabled" and "IDCCOB" in result["reply"]


def test_different_number_and_invalid_phone(mb):
    mb.add_account(ACCT, triggers=[mb.trigger("2026-10-06 10:00:00", phone="263771999999")])
    result = debit(mb)
    assert result["status"] == "different_number" and "263771999999" in result["reply"]
    assert debit(mb, phone="07721")["status"] == "invalid_ticket_phone"


def test_match_then_gateway_verdicts(mb):
    mb.add_account(ACCT, triggers=[mb.trigger("2026-10-06 10:00:00")])
    mb.socket_rows = [mb.socket_row("2026-10-06 10:02:00")]
    result = debit(mb)
    assert result["status"] == "delivered_check_spam" and result["auto_close_allowed"]
    assert result["socket_delivery"]["delivered"] == 1

    mb.socket_rows = [mb.socket_row("2026-10-06 10:02:00", status="FAILED_TO_SEND")]
    result = debit(mb)
    assert result["status"] == "socket_failed" and not result["auto_close_allowed"]


def test_not_found_with_and_without_customer_email(mb):
    mb.add_account(ACCT, triggers=[mb.trigger("2026-09-30 10:00:00")], email="client@example.com")
    assert debit(mb)["status"] == "socket_not_found"
    mb.accounts[ACCT]["email"] = "none@nmbz.co.zw"
    result = debit(mb)
    assert result["status"] == "sms_no_customer_email" and result["auto_close_allowed"]


def test_socket_db_unavailable_is_unknown_not_delivered(mb):
    mb.add_account(ACCT, triggers=[mb.trigger("2026-10-06 10:00:00")])
    checks = make_checks(mb)
    checks.dbs.socket = None
    assert checks.check_account(ACCT, ["DEBITS"], "0772123456", as_of=AS_OF)["status"] == "socket_unknown"


def test_no_transactions_uses_cob_number(mb):
    mb.add_account(ACCT)
    assert debit(mb)["status"] == "no_txn_number_match"
    assert debit(mb, phone="0712000111")["status"] == "no_txn_different_number"
    mb.add_account(ACCT, cob=[], tel_res=None)
    assert debit(mb)["status"] == "no_txn_no_number"


def test_salaries_and_both(mb):
    mb.add_account(ACCT)
    checks = make_checks(mb)
    assert checks.check_account(ACCT, ["SALARIES"], "0772123456", as_of=AS_OF)["status"] == "salaries_account_missing"
    mb.salaries[ACCT] = "263700000000"
    assert checks.check_account(ACCT, ["SALARIES"], "0772123456", as_of=AS_OF)["status"] == "salaries_phone_missing"
    mb.salaries[ACCT] = "0772123456"
    assert checks.check_account(ACCT, ["SALARIES"], "0772123456", as_of=AS_OF)["status"] == "salaries_match"
    strict = make_checks(mb, salaries_require_delivery_proof=True)
    assert strict.check_account(ACCT, ["SALARIES"], "0772123456", as_of=AS_OF)["status"] == "salaries_unverified"

    # BOTH: an escalating sub-status wins the merged status.
    mb.accounts[ACCT]["triggers"] = [mb.trigger("2026-10-06 10:00:00")]
    mb.socket_rows = [mb.socket_row("2026-10-06 10:02:00", status="FAILED_TO_DELIVER")]
    both = checks.check_account(ACCT, ["SALARIES", "DEBITS"], "0772123456", as_of=AS_OF)
    assert both["status"] == "socket_failed" and both["reply"].startswith("Salaries — ")
    assert "socket_failed" in ESCALATING_STATUSES


def test_tran_table_follows_the_year_and_spans_new_year(mb):
    mb.add_account(ACCT, triggers=[mb.trigger("2026-12-30 10:00:00"), mb.trigger("2027-01-03 10:00:00", amount="9")])
    checks = make_checks(mb)
    rows = checks.standby_rows(ACCT, date(2026, 12, 25), date(2027, 1, 9))
    tables = [sql.split("FROM ")[1].split(" S\n")[0] for db, sql in mb.sql_log if "K.UPTD_ON" in sql]
    assert tables == ["NMBPRODIDC.TRAN2027", "NMBPRODIDC.TRAN2026"]
    assert [r["UPTD_ON"][:4] for r in rows] == ["2027", "2026"]
    slices = [sql for db, sql in mb.sql_log if "TRAN2027" in sql][0]
    assert "TO_DATE('01-01-2027'" in slices and "TO_DATE('09-01-2027'" in slices


def test_missing_new_year_table_is_tolerated_only_if_another_year_worked(mb):
    mb.add_account(ACCT, triggers=[mb.trigger("2026-12-30 10:00:00")])
    mb.missing_tables = {"NMBPRODIDC.TRAN2027"}
    checks = make_checks(mb)
    assert len(checks.standby_rows(ACCT, date(2026, 12, 25), date(2027, 1, 2))) == 1
    with pytest.raises(MetabaseError):
        checks.standby_rows(ACCT, date(2027, 1, 1), date(2027, 1, 2))


def test_custom_tran_pattern_and_bad_pattern(mb):
    mb.add_account(ACCT)
    checks = make_checks(mb, tran_table_pattern="NMBPRODIDC.TRAN{year}")
    assert checks.tran_table(2031) == "NMBPRODIDC.TRAN2031"
    with pytest.raises(ValueError):
        make_checks(mb, tran_table_pattern="TRAN2026; DROP")


def test_account_literal_is_validated(mb):
    with pytest.raises(Exception):
        make_checks(mb).internal_account_number("123' OR '1'='1")


# ---- osTicket -------------------------------------------------------------------
def store_with_ticket(mb: FakeMetabase) -> tuple[OsTicket, object]:
    mb.add_ticket(7, "482913", {"phone_number": "0772123456", "account_number": "12345678901", "currency": "USD",
                                "type": "Debits"}, ["No alerts since Monday"])
    store = OsTicket(mb, TICKET_DB)  # type: ignore[arg-type]
    ticket = store.open_sms_tickets()[0]
    ticket.thread_id = store.thread_ids([ticket])[ticket.number]
    store.load_thread(ticket)
    return store, ticket


def test_ticket_fetch_normalises_like_java(mb):
    store, ticket = store_with_ticket(mb)
    assert ticket.account_number == ACCT and ticket.required_accounts == [ACCT]
    assert ticket.entries[0].type == "M" and ticket.entries[0].body == "No alerts since Monday"
    sql = next(s for db, s in mb.sql_log if "ost_form_entry" in s)
    assert "CONCAT_WS(' / ', ht4.topic, ht3.topic, ht2.topic, ht.topic) = 'SMS Alerts'" in sql


def test_reply_and_close_writes_like_java(mb):
    store, ticket = store_with_ticket(mb)
    store.post_reply(ticket, AGENT_STAFF, "SMS Agent", "Kindly update the number. It's <b>urgent</b>\\")
    store.close(ticket, AGENT_STAFF, 3)
    insert, update = mb.writes
    assert "INSERT INTO ost_thread_entry" in insert and f"{AGENT_STAFF}, 0, 'R', 0, 'SMS Agent'" in insert
    assert "It&#x27;s &lt;b&gt;urgent&lt;/b&gt;\\\\" in insert and "'html'" in insert
    assert "SET status_id = 3," in update and f"staff_id = {AGENT_STAFF}" in update and "isanswered = 1" in update
    assert mb.tickets[7]["state"] == "closed"


def test_close_verification_failure_raises(mb):
    store, ticket = store_with_ticket(mb)
    mb.write = lambda db, sql: None  # update silently does nothing
    with pytest.raises(MetabaseError, match="close verification"):
        store.close(ticket, AGENT_STAFF, 3)


def test_escalation_note_is_posted_once(mb):
    store, ticket = store_with_ticket(mb)
    assert store.post_note(ticket, AGENT_STAFF, "SMS Agent", f"{ESCALATION_NOTE_MARKER}\nfirst body", ESCALATION_NOTE_MARKER)
    assert not store.post_note(ticket, AGENT_STAFF, "SMS Agent", f"{ESCALATION_NOTE_MARKER}\ndrifted body", ESCALATION_NOTE_MARKER)
    assert len(mb.writes) == 1
    store.load_thread(ticket)
    assert ticket.escalated_by_agent(AGENT_STAFF)
    mb.add_entry(7, "M", "Any update?")
    store.load_thread(ticket)
    assert not ticket.escalated_by_agent(AGENT_STAFF)


def test_thread_helpers(mb):
    store, ticket = store_with_ticket(mb)
    mb.add_entry(7, "R", "Please send the correct number", staff_id=AGENT_STAFF)
    store.load_thread(ticket)
    assert ticket.answered_since_last_message() and ticket.agent_replies(AGENT_STAFF) == 1
    mb.add_entry(7, "M", "Correct account is 0009-8765-4321 thanks")
    store.load_thread(ticket)
    assert not ticket.answered_since_last_message()
    assert ticket.mentions_account("00000987654321") and ticket.mentions_account(ACCT)
    assert not ticket.mentions_account("00000000000042")


def test_html_body():
    assert html_body("a\nb & 'c'") == "<p>a<br />b &amp; &#x27;c&#x27;</p>"
