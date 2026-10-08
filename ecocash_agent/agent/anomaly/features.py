"""Describing an EcoCash link attempt as a handful of comparable features, and finding what is
over-represented among the failures.

Nothing here keeps a phone, account or ID number: features are categories (channel, network,
customer-number series, product code, currency as sent, ID format, time of day).
"""
from __future__ import annotations

from typing import Any, Iterable

from ..normalize import (
    digits_only,
    id_key,
    mobile_key,
    network_of,
    normalize_account_number,
)
from agentcore.anomaly.common import compare as _compare, hour_band as _hour_band, values_of as _values  # noqa: F401

FEATURE_LABELS = {
    "via": "Started from",
    "channel": "Channel",
    "operation": "Request",
    "currency": "Currency as sent",
    "network": "Mobile network",
    "mobile_format": "Mobile number format sent",
    "cif_series": "Customer number series (first 2 digits)",
    "product": "Account product code",
    "id_format": "National ID as sent",
    "hour_band": "Time of day",
    "error_type": "EcoCash error",
    "branch": "Branch (ticket owner)",
    "status": "Latest attempt (ticket)",
}
VIA = {"USSD": "client on USSD", "VBS": "branch staff", "IAPPROVE": "account opening (iApprove)"}


def mobile_format(value: Any) -> str:
    text = str(value or "").strip()
    digits = digits_only(text)
    if not digits:
        return "missing"
    if mobile_key(digits) is None:
        return "not a Zimbabwe mobile"
    if digits.startswith("263263"):
        return "doubled 263"
    if text.startswith("+263"):
        return "+263"
    if digits.startswith("263"):
        return "263"
    if digits.startswith("0"):
        return "0 prefix"
    return "9 digits"


def account_parts(value: Any) -> tuple[str, str]:
    """(customer number series, product code): NMB accounts are CIF (7) + product (4) + sequence (3)."""
    acct = normalize_account_number(value)
    if not acct or len(acct) != 14:
        return ("unknown", "unknown")
    return (acct[:2], acct[7:11])


def id_format(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "missing"
    return "valid format" if id_key(text) else "unusual format"


def attempt_features(row: dict[str, Any]) -> dict[str, str]:
    """Features of one ecocash_link_attempts row (a sample for the failed-vs-linked comparison)."""
    lowered = {str(k).lower(): v for k, v in row.items()}
    series, product = account_parts(lowered.get("account_number"))
    via = str(lowered.get("via") or "unknown")
    return {
        "via": VIA.get(via, via),
        "channel": str(lowered.get("channel") or "unknown"),
        "operation": "delink" if str(lowered.get("operation")) == "UNLINK" else "link",
        "currency": str(lowered.get("currency") or "missing"),
        "network": network_of(lowered.get("mobile_number")),
        "mobile_format": mobile_format(lowered.get("mobile_number")),
        "cif_series": series,
        "product": product,
        "id_format": id_format(lowered.get("id_number")),
        "hour_band": _hour_band(lowered.get("requested_at")),
    }


def attempt_facts(attempt: Any, ticket: Any = None) -> dict[str, Any]:
    """Non-identifying facts about the latest attempt behind a ticket decision (stored with it)."""
    series, product = account_parts(attempt.account)
    return {
        "status": attempt.type_key,
        "via": VIA.get(attempt.via or "", attempt.via or "unknown"),
        "currency": attempt.currency or "missing",
        "network": network_of(attempt.mobile),
        "cif_series": series,
        "product": product,
        "id_format": id_format(attempt.id_number),
        "branch": (getattr(ticket, "org_name", None) or "unknown"),
    }


def compare(failed: Iterable[dict[str, Any]], ok: Iterable[dict[str, Any]], features: Iterable[str],
            min_failed: int = 5, min_ratio: float = 1.8) -> list[dict[str, Any]]:
    """Feature values much more common among failures than successes (agentcore.anomaly.common.compare)."""
    return _compare(failed, ok, features, min_failed, min_ratio, labels=FEATURE_LABELS)
