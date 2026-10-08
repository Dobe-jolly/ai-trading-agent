"""Normalisation and gateway matching must behave exactly like the Java (and the queue monitor)."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from agent.delivery import (
    DELIVERED, FAILED, NOT_FOUND, PENDING, UNKNOWN, AlertTrigger, SocketDelivery, SocketDeliveryChecker,
)
from agent.normalize import (
    LOCAL_ZONE, is_deliverable_phone, is_usable_customer_email, is_valid_ticket_phone, normalize_account_number,
    normalize_gateway_phone, normalize_ticket_phone, parse_timestamp, requested_alert_types, subscriber,
    ticket_account_numbers, uses_both_accounts,
)


def test_ticket_phone_normalisation():
    assert normalize_ticket_phone("0772 123 456") == "263772123456"
    assert normalize_ticket_phone("772123456") == "263772123456"
    assert normalize_ticket_phone("+263 77 212 3456") == "263772123456"
    assert normalize_ticket_phone("12345") == "12345"
    assert not is_valid_ticket_phone("077212345")
    assert is_valid_ticket_phone("0772123456")
    assert not is_deliverable_phone("263770000000")


def test_gateway_phone_rejects_placeholders_and_finds_last_number():
    assert normalize_gateway_phone("263772000000") is None
    assert normalize_gateway_phone("0772000000") is None
    assert normalize_gateway_phone("from 0711111111 to 0772123456") == "263772123456"
    assert subscriber("263772123456") == subscriber("0772123456") == "772123456"
    assert subscriber("123456789") is None


def test_account_and_types():
    assert normalize_account_number("123-456") == "00000000123456"
    assert normalize_account_number("123456789012345") == "123456789012345"
    assert uses_both_accounts("USD and ZWG") and uses_both_accounts("both") and not uses_both_accounts("USD")
    assert ticket_account_numbers("A", "B", "USD/ZiG") == ["A", "B"]
    assert ticket_account_numbers("A", "B", "USD") == ["A"]
    assert requested_alert_types("Salaries and Debits *", None) == ["SALARIES", "DEBITS"]
    assert requested_alert_types("credits", None) == ["SALARIES"]
    assert requested_alert_types(None, "SMS Alerts / Salaries") == ["SALARIES"]
    assert requested_alert_types(None, "SMS Alerts") == ["DEBITS"]
    assert not is_usable_customer_email("none@nmbz.co.zw") and not is_usable_customer_email(None)
    assert is_usable_customer_email("a@b.co")


def test_timestamp_parsing():
    assert parse_timestamp("2026-10-07T08:00:00Z").astimezone(LOCAL_ZONE).hour == 10
    assert parse_timestamp("2026-10-07 10:00:00").utcoffset() == timedelta(hours=2)
    assert parse_timestamp("2026-10-07T10:00:00.1234567+02:00").minute == 0
    assert parse_timestamp(1791360000000) is not None
    assert parse_timestamp("garbage") is None


# ---- gateway reconciliation ---------------------------------------------------
def checker(**kw) -> SocketDeliveryChecker:
    return SocketDeliveryChecker(metabase=None, **kw)  # type: ignore[arg-type]


def at(text: str) -> datetime:
    return parse_timestamp(text)  # type: ignore[return-value]


def trig(when: str, amount: str = "150.00", suffix: str = "5678901") -> AlertTrigger:
    return AlertTrigger(at(when), "263772123456", suffix, "USD", Decimal(amount), "DEBIT")


def deliv(when: str, status: str = "DELIVERED_SUCCESSFULLY", suffix: str = "5678901", amount: str = "150.00",
          phone: str = "263772123456") -> SocketDelivery:
    return SocketDelivery(1, at(when), "ECONET", status, "R", phone, suffix, "USD", Decimal(amount))


NOW = at("2026-10-08 12:00:00")


def test_delivered_failed_and_worst_case():
    c = checker()
    report = c.reconcile([trig("2026-10-07 10:00:00")], [deliv("2026-10-07 10:05:00")], NOW)
    assert report.status == DELIVERED and report.delivered == 1
    report = c.reconcile([trig("2026-10-07 10:00:00"), trig("2026-10-07 11:00:00", amount="20")],
                         [deliv("2026-10-07 10:05:00"), deliv("2026-10-07 11:01:00", "FAILED_TO_SEND", amount="20")], NOW)
    assert report.status == FAILED and report.delivered == 1 and report.failed == 1


def test_missing_send_is_pending_within_dispatch_lag_then_not_found():
    c = checker(dispatch_lag_minutes=1080)
    assert c.reconcile([trig("2026-10-08 06:00:00")], [], NOW).status == PENDING
    assert c.reconcile([trig("2026-10-06 06:00:00")], [], NOW).status == NOT_FOUND


def test_late_send_within_lag_matches_but_not_before_window():
    c = checker(window_minutes=60, dispatch_lag_minutes=1080)
    assert c.reconcile([trig("2026-10-07 01:00:00")], [deliv("2026-10-07 14:00:00")], NOW).status == DELIVERED
    assert c.reconcile([trig("2026-10-07 10:00:00")], [deliv("2026-10-07 08:00:00")], NOW).status == NOT_FOUND


def test_strict_beats_loose_and_other_accounts_never_match():
    c = checker()
    t = trig("2026-10-07 10:00:00")
    loose = deliv("2026-10-07 10:01:00", suffix="")
    strict = deliv("2026-10-07 10:30:00", "FAILED_TO_DELIVER")
    assert c._best(t, [loose, strict]) is strict
    assert not c.is_loose_match(t, deliv("2026-10-07 10:01:00", suffix="1111111"))
    assert c.is_strict_match(t, deliv("2026-10-07 10:01:00", phone="0772123456"))


def test_over_send_flagged():
    c = checker(over_send_threshold=5)
    sends = [deliv("2026-10-07 10:0%d:00" % i) for i in range(7)]
    report = c.reconcile([trig("2026-10-07 10:00:00")], sends, NOW)
    assert report.over_send_detected and report.max_duplicate_sends == 7 and "OVER-SEND" in report.detail


def test_windows_merge_and_query_shape():
    c = checker(window_minutes=60, dispatch_lag_minutes=60)
    ranges = c.match_windows([trig("2026-10-07 10:00:00"), trig("2026-10-07 10:30:00"), trig("2026-10-07 20:00:00")])
    assert len(ranges) == 2
    query = c.delivery_query(ranges, "263772123456")
    assert "TIMESTAMPTZ '2026-10-07 09:00:00+02:00'" in query
    assert "LIKE '%0772123456%'" in query and "WHATSAPP-META" in query and "LIMIT 500" in query


def test_unknown_when_disabled_or_no_db_or_unmatchable():
    assert checker(enabled=False).check(4, [trig("2026-10-07 10:00:00")]).status == UNKNOWN
    assert checker().check(None, [trig("2026-10-07 10:00:00")]).status == UNKNOWN
    bad = AlertTrigger(None, None, None, None, None, None)
    assert checker().check(4, [bad]).status == UNKNOWN


def test_socket_row_parsing():
    d = SocketDelivery.from_row({"ID": 9, "TIME_STAMP": "2026-10-07 10:00:00", "external_status": "SENT_AWAITING_DELIVERY",
                                 "logged_responses": "to=0772123456 Account No. ****5678901 ZWG 1,250.50"})
    assert (d.phone, d.account_suffix, d.currency, d.amount, d.status) == (
        "263772123456", "5678901", "ZIG", Decimal("1250.50"), PENDING)
