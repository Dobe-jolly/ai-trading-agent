"""Describing an EcoCash link attempt as a handful of comparable features, and finding what is
over-represented among the failures.

Nothing here keeps a phone, account or ID number: features are categories (channel, network,
customer-number series, product code, currency as sent, ID format, time of day).
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Optional

from ..normalize import (
    LOCAL_ZONE,
    digits_only,
    id_key,
    mobile_key,
    network_of,
    normalize_account_number,
    parse_timestamp,
)

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


def _hour_band(ts: Any) -> str:
    parsed = parse_timestamp(ts)
    if parsed is None:
        return "unknown"
    hour = parsed.astimezone(LOCAL_ZONE).hour
    return ("00–05", "06–11", "12–17", "18–23")[hour // 6]


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
    """For each feature value: its share among failures vs among successes. Values much more
    common among failures (ratio >= min_ratio, with enough failures behind them) are findings.
    Samples may be drawn separately; shares within each sample are still comparable."""
    failed, ok = list(failed), list(ok)
    if not failed or not ok:
        return []
    findings = []
    for feature in features:
        f_counts: Counter = Counter()
        o_counts: Counter = Counter()
        for row in failed:
            for value in _values(row.get(feature)):
                f_counts[value] += 1
        for row in ok:
            for value in _values(row.get(feature)):
                o_counts[value] += 1
        for value, n_failed in f_counts.items():
            failed_share = n_failed / len(failed)
            ok_share = o_counts.get(value, 0) / len(ok)
            # Smoothed (add-one) shares for the ratio, so a value that happens to be absent from a
            # small set of deliveries does not get an infinite ratio and outrank real patterns.
            ratio = ((n_failed + 1) / (len(failed) + 2)) / ((o_counts.get(value, 0) + 1) / (len(ok) + 2))
            if n_failed >= max(min_failed, 0.05 * len(failed)) and ratio >= min_ratio:
                findings.append({
                    "feature": feature, "label": FEATURE_LABELS.get(feature, feature), "value": value,
                    "failed_share": round(failed_share, 3), "ok_share": round(ok_share, 3),
                    "ratio": round(ratio, 1), "failed_n": n_failed, "ok_n": o_counts.get(value, 0),
                })
    # Strongest first: share of failures it explains, weighted by how distinctive it is.
    findings.sort(key=lambda f: f["failed_share"] * min(f["ratio"], 50.0), reverse=True)
    return findings[:12]


def _values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value] or ["none"]
    return [str(value if value is not None else "unknown")]
