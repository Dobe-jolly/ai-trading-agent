"""SMS gateway delivery reconciliation, ported from SocketDeliveryChecker / SocketMessageParser.

Reconciles core-banking alert triggers against public.transaction_activity (SEND_SMS) on the
same key as the queue-monitor agent: subscriber, last-7 account suffix, currency, amount, time
window. Every failure path reports UNKNOWN, never DELIVERED.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from . import sql
from .metabase import MetabaseClient, MetabaseError
from .normalize import (
    ACCOUNT_SUFFIX_PATTERN,
    AMOUNT_PATTERN,
    amounts_match,
    currencies_match,
    format_timestamp,
    normalize_account_suffix,
    normalize_currency,
    normalize_gateway_phone,
    now_local,
    parse_timestamp,
    subscriber,
    to_decimal,
)

log = logging.getLogger(__name__)
MAX_SOCKET_ROWS = 500

DELIVERED, PENDING, FAILED, NOT_FOUND, UNKNOWN = "delivered", "pending", "failed", "not_found", "unknown"
_RANK = {DELIVERED: 3, FAILED: 2, PENDING: 1}


def status_from_external(external: Optional[str]) -> str:
    value = (external or "").strip().upper()
    if value == "DELIVERED_SUCCESSFULLY":
        return DELIVERED
    if value == "SENT_AWAITING_DELIVERY":
        return PENDING
    if value in ("FAILED_TO_SEND", "FAILED_TO_DELIVER"):
        return FAILED
    return NOT_FOUND


@dataclass
class AlertTrigger:
    triggered_at: Optional[datetime]
    phone: Optional[str]
    account_suffix: Optional[str]
    currency: Optional[str]
    amount: Optional[Decimal]
    event_name: Optional[str]

    @classmethod
    def from_standby_row(cls, row: dict[str, Any]) -> "AlertTrigger":
        return cls(
            triggered_at=parse_timestamp(row.get("UPTD_ON")),
            phone=normalize_gateway_phone(row.get("INDCLIENT_TEL_RES")),
            account_suffix=normalize_account_suffix(row.get("IACLINK_ACTUAL_ACNUM")),
            currency=normalize_currency(row.get("TRAN_CURR_CODE")),
            amount=to_decimal(row.get("TRAN_AMOUNT")),
            event_name=None if row.get("CMNAEC_EVENT_NAME") is None else str(row.get("CMNAEC_EVENT_NAME")),
        )

    @property
    def matchable(self) -> bool:
        return self.triggered_at is not None and self.amount is not None and bool(self.currency)


@dataclass
class SocketDelivery:
    id: Optional[int]
    timestamp: Optional[datetime]
    provider: Optional[str]
    external_status: Optional[str]
    reference: Optional[str]
    phone: Optional[str]
    account_suffix: Optional[str]
    currency: Optional[str]
    amount: Optional[Decimal]

    @property
    def status(self) -> str:
        return status_from_external(self.external_status)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "SocketDelivery":
        lowered = {str(k).lower(): v for k, v in row.items()}
        text = str(lowered.get("logged_responses") or "")
        suffix = currency = None
        amount = None
        match = ACCOUNT_SUFFIX_PATTERN.search(text)
        if match:
            suffix = normalize_account_suffix(match.group(1))
        match = AMOUNT_PATTERN.search(text)
        if match:
            currency = normalize_currency(match.group(1))
            amount = to_decimal(match.group(2))
        raw_id = lowered.get("id")
        return cls(
            id=int(raw_id) if str(raw_id or "").strip().isdigit() else None,
            timestamp=parse_timestamp(lowered.get("time_stamp")),
            provider=None if lowered.get("service_provider_name") is None else str(lowered["service_provider_name"]),
            external_status=None if lowered.get("external_status") is None else str(lowered["external_status"]),
            reference=None if lowered.get("transaction_reference") is None else str(lowered["transaction_reference"]),
            phone=normalize_gateway_phone(text) if text.strip() else None,
            account_suffix=suffix,
            currency=currency,
            amount=amount,
        )


@dataclass
class DeliveryReport:
    status: str
    detail: str
    triggers_checked: int = 0
    delivered: int = 0
    pending: int = 0
    failed: int = 0
    not_found: int = 0
    over_send_detected: bool = False
    max_duplicate_sends: int = 0
    matches: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def unknown(cls, detail: str) -> "DeliveryReport":
        return cls(status=UNKNOWN, detail=detail)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "triggers_checked": self.triggers_checked,
            "delivered": self.delivered,
            "pending": self.pending,
            "failed": self.failed,
            "not_found": self.not_found,
            "over_send_detected": self.over_send_detected,
            "max_duplicate_sends": self.max_duplicate_sends,
            "matches": self.matches,
        }


class SocketDeliveryChecker:
    def __init__(
        self,
        metabase: MetabaseClient,
        enabled: bool = True,
        window_minutes: int = 60,
        dispatch_lag_minutes: int = 1080,
        over_send_threshold: int = 5,
        max_triggers: int = 5,
    ) -> None:
        self.mb = metabase
        self.enabled = enabled
        self.window = window_minutes
        self.lag = dispatch_lag_minutes
        self.over_send_threshold = over_send_threshold
        self.max_triggers = max_triggers

    def check(self, socket_db_id: Optional[int], triggers: list[AlertTrigger], now: Optional[datetime] = None) -> DeliveryReport:
        if not self.enabled:
            return DeliveryReport.unknown("Socket delivery check is disabled (SMS_SOCKET_CHECK_ENABLED=false).")
        if socket_db_id is None:
            return DeliveryReport.unknown("The socket SMS database is not available, so gateway delivery could not be confirmed.")
        matchable = self.select_matchable(triggers)
        if not matchable:
            return DeliveryReport.unknown(
                "No alert trigger carried enough detail (timestamp, currency and amount) to reconcile against the SMS gateway."
            )
        try:
            deliveries = self.fetch_deliveries(socket_db_id, matchable)
        except (MetabaseError, sql.UnsafeValue) as exc:
            log.warning("Socket SMS delivery lookup failed; reporting UNKNOWN: %s", exc)
            return DeliveryReport.unknown("The SMS gateway audit trail could not be read, so delivery could not be confirmed.")
        return self.reconcile(matchable, deliveries, now or now_local())

    def select_matchable(self, triggers: list[AlertTrigger]) -> list[AlertTrigger]:
        matchable = [t for t in triggers if t is not None and t.matchable]
        matchable.sort(key=lambda t: t.triggered_at, reverse=True)  # type: ignore[arg-type,return-value]
        return matchable[: self.max_triggers]

    def match_windows(self, triggers: list[AlertTrigger]) -> list[list[datetime]]:
        forward = max(self.window, self.lag)
        ranges = sorted(
            ([t.triggered_at - timedelta(minutes=self.window), t.triggered_at + timedelta(minutes=forward)]
             for t in triggers if t.triggered_at is not None),
            key=lambda r: r[0],
        )
        merged: list[list[datetime]] = []
        for start, end in ranges:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return merged

    def delivery_query(self, ranges: list[list[datetime]], phone: Optional[str]) -> str:
        range_sql = "\n       OR ".join(
            f"(time_stamp >= {sql.pg_timestamptz(start)} AND time_stamp <= {sql.pg_timestamptz(end)})"
            for start, end in ranges
        )
        lines = [
            "SELECT",
            "    id,",
            "    time_stamp,",
            "    service_provider_name,",
            "    external_status,",
            "    transaction_reference,",
            "    logged_responses",
            "FROM public.transaction_activity",
            "WHERE service_type = 'VALUE_ADDED_SERVICES'",
            "  AND service_operation_type = 'SEND_SMS'",
            "  AND COALESCE(service_provider_name, '') <> 'WHATSAPP-META'",
            "  AND external_status IN ('SENT_AWAITING_DELIVERY', 'DELIVERED_SUCCESSFULLY', 'FAILED_TO_SEND', 'FAILED_TO_DELIVER')",
            f"  AND ({range_sql})",
        ]
        if phone is not None:
            full = sql.digits(phone)
            sub = full[3:]
            lines += [
                f"  AND (logged_responses LIKE '%{full}%'",
                f"       OR logged_responses LIKE '%0{sub}%'",
                f"       OR logged_responses LIKE '%{sub}%')",
            ]
        lines += ["ORDER BY time_stamp DESC, id DESC", f"LIMIT {MAX_SOCKET_ROWS}"]
        return "\n".join(lines)

    def fetch_deliveries(self, socket_db_id: int, triggers: list[AlertTrigger]) -> list[SocketDelivery]:
        ranges = self.match_windows(triggers)
        phone = next((t.phone for t in triggers if t.phone and len(t.phone) == 12), None)
        if phone is not None:
            narrowed = [SocketDelivery.from_row(r) for r in self.mb.query(socket_db_id, self.delivery_query(ranges, phone))]
            if narrowed:
                return narrowed
            # A zero-row narrowed read is ambiguous (payload format quirk); re-read unfiltered.
        return [SocketDelivery.from_row(r) for r in self.mb.query(socket_db_id, self.delivery_query(ranges, None))]

    # ---- matching ---------------------------------------------------------------
    def _phones_compatible(self, a: Optional[str], b: Optional[str]) -> bool:
        if a is None or b is None:
            return True
        sa, sb = subscriber(a), subscriber(b)
        if sa is None or sb is None:
            return a == b
        return sa == sb

    def _base_match(self, trigger: AlertTrigger, delivery: SocketDelivery) -> bool:
        if trigger.triggered_at is None or delivery.timestamp is None:
            return False
        if not self._phones_compatible(trigger.phone, delivery.phone):
            return False
        if not currencies_match(trigger.currency, delivery.currency):
            return False
        if not amounts_match(trigger.amount, delivery.amount):
            return False
        # Java Duration.toMinutes() truncates toward zero.
        seconds = (delivery.timestamp - trigger.triggered_at).total_seconds()
        minutes_after = int(seconds / 60)
        return -self.window <= minutes_after <= self.lag

    def is_strict_match(self, trigger: AlertTrigger, delivery: SocketDelivery) -> bool:
        return (self._base_match(trigger, delivery) and bool(trigger.account_suffix)
                and trigger.account_suffix == delivery.account_suffix)

    def is_loose_match(self, trigger: AlertTrigger, delivery: SocketDelivery) -> bool:
        return self._base_match(trigger, delivery) and not (delivery.account_suffix or "").strip()

    def _best(self, trigger: AlertTrigger, deliveries: list[SocketDelivery]) -> Optional[SocketDelivery]:
        best: Optional[SocketDelivery] = None
        best_key: tuple = ()
        for delivery in deliveries:
            strict = self.is_strict_match(trigger, delivery)
            if not strict and not self.is_loose_match(trigger, delivery):
                continue
            diff = abs((delivery.timestamp - trigger.triggered_at).total_seconds())  # type: ignore[operator]
            key = (strict, _RANK.get(delivery.status, 0), -diff)
            if best is None or key > best_key:
                best, best_key = delivery, key
        return best

    def max_duplicate_sends(self, deliveries: list[SocketDelivery]) -> int:
        counts = Counter(
            f"{subscriber(d.phone)}|{d.account_suffix}|{d.currency}|{'' if d.amount is None else format(d.amount, 'f')}"
            for d in deliveries if d is not None
        )
        return max(counts.values(), default=0)

    def reconcile(self, triggers: list[AlertTrigger], deliveries: list[SocketDelivery], now: datetime) -> DeliveryReport:
        available = list(deliveries)
        counts = {DELIVERED: 0, PENDING: 0, FAILED: 0, NOT_FOUND: 0}
        matches: list[dict[str, Any]] = []
        for trigger in triggers:
            best = self._best(trigger, available)
            match: dict[str, Any] = {
                "triggered_at": format_timestamp(trigger.triggered_at),
                "event": trigger.event_name,
                "currency": trigger.currency,
                "amount": None if trigger.amount is None else format(trigger.amount, "f"),
            }
            if best is None:
                age_minutes = int((now - trigger.triggered_at).total_seconds() / 60) if trigger.triggered_at else None
                if age_minutes is not None and age_minutes < self.lag:
                    counts[PENDING] += 1
                    match["socket_status"] = "pending_dispatch"
                else:
                    counts[NOT_FOUND] += 1
                    match["socket_status"] = "not_found"
                match.update(service_provider=None, sent_at=None, transaction_reference=None)
            else:
                available.remove(best)
                counts[best.status] += 1
                match.update(
                    socket_status=best.status,
                    external_status=best.external_status,
                    service_provider=best.provider,
                    sent_at=format_timestamp(best.timestamp),
                    transaction_reference=best.reference,
                )
            matches.append(match)

        if counts[NOT_FOUND]:
            overall = NOT_FOUND
        elif counts[FAILED]:
            overall = FAILED
        elif counts[PENDING]:
            overall = PENDING
        elif counts[DELIVERED]:
            overall = DELIVERED
        else:
            overall = UNKNOWN

        duplicates = self.max_duplicate_sends(deliveries)
        over_send = duplicates > self.over_send_threshold
        detail = _describe(overall, len(triggers), counts)
        if over_send:
            detail += (f" OVER-SEND: the same alert was sent {duplicates} times to this number — "
                       "a stuck/looping queue job, not normal delivery.")
        return DeliveryReport(
            status=overall, detail=detail, triggers_checked=len(triggers), delivered=counts[DELIVERED],
            pending=counts[PENDING], failed=counts[FAILED], not_found=counts[NOT_FOUND],
            over_send_detected=over_send, max_duplicate_sends=duplicates, matches=matches,
        )


def _describe(overall: str, checked: int, c: dict[str, int]) -> str:
    counts = (f"{checked} alert(s) checked: {c[DELIVERED]} delivered, {c[PENDING]} pending, "
              f"{c[FAILED]} failed, {c[NOT_FOUND]} with no gateway record.")
    if overall == DELIVERED:
        return "Every alert checked was confirmed delivered by the SMS gateway. " + counts
    if overall == PENDING:
        return ("At least one alert has not been delivered yet — it is either still queued at the gateway or too "
                "recent to have been dispatched (alerts can lag the transaction by hours). " + counts)
    if overall == FAILED:
        return "At least one alert was rejected or failed at the SMS gateway. " + counts
    if overall == NOT_FOUND:
        return "At least one alert has no record at the SMS gateway at all, so it was never sent. " + counts
    return counts
