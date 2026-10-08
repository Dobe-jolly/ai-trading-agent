"""Describing an SMS (or a ticket's check result) as a handful of comparable features, and
finding what is over-represented among the failures.

Nothing here keeps a phone or account number: features are categories (network, format,
currency, size band, ...).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Iterable, Optional

from ..normalize import (
    ACCOUNT_SUFFIX_PATTERN,
    AMOUNT_PATTERN,
    LOCAL_ZONE,
    normalize_currency,
    normalize_gateway_phone,
    parse_timestamp,
    to_decimal,
)

# GSM 03.38 basic character set (plus the extension table). Anything else forces UCS-2,
# which halves the SMS length and is rejected by some routes.
GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
           "¿abcdefghijklmnopqrstuvwxyzäöñüà^{}\\[~]|€")
MESSAGE_KEYS = re.compile(r'"(?:message|text|body|msg|content|sms)"\s*:\s*"((?:[^"\\]|\\.){1,2000})"', re.I)
ERROR_KEYS = re.compile(
    r'"(?:error(?:Message|Description|Code)?|reason|statusDescription|description|errorText|responseMessage)"\s*:\s*"([^"]{1,120})"',
    re.I)
DEBIT_WORDS = re.compile(r"\b(debit(?:ed)?|withdraw\w*|purchase\w*|paid|payment|transfer(?:red)? to|dr)\b", re.I)
CREDIT_WORDS = re.compile(r"\b(credit(?:ed)?|deposit\w*|received|salary|cr)\b", re.I)

FEATURE_LABELS = {
    "provider": "Gateway route",
    "network": "Mobile network",
    "phone_format": "Number format in the message",
    "currency": "Currency",
    "amount_band": "Amount",
    "has_account": "Account in the message",
    "parts": "Message size",
    "encoding": "Character set",
    "kind": "Alert type",
    "error": "Provider error text",
    "hour_band": "Time of day",
    "alert_type": "Alert type (ticket)",
    "account_currency": "Account currency",
    "phone_network": "Network of the ticket number",
    "email_on_file": "Customer email in core",
    "cob_registered": "IDCCOB SMS registration",
    "providers": "Gateway route used (ticket)",
    "trigger_band": "Alerts in 15 days",
    "event": "Alert event",
    "branch": "Branch (ticket owner)",
    "dispatch_lag": "Trigger to gateway delay",
}


def network_of(phone: Optional[str]) -> str:
    """Zimbabwe mobile network from the 9-digit subscriber prefix."""
    if not phone:
        return "unknown"
    sub = phone[-9:]
    if sub[:2] in ("77", "78"):
        return "Econet"
    if sub[:2] == "71":
        return "NetOne"
    if sub[:2] == "73":
        return "Telecel"
    return "other"


def _phone_format(text: str) -> str:
    digits_runs = re.findall(r"\+?\d[\d ]{7,15}\d", text)
    for run in digits_runs:
        d = re.sub(r"\D", "", run)
        if d.startswith("2632637"):
            return "doubled 263"
        if d.startswith("2637") and len(d) == 12:
            return "+263 / 263"
        if d.startswith("07") and len(d) == 10:
            return "07…"
        if d.startswith("7") and len(d) == 9:
            return "9 digits"
    return "none found"


def _amount_band(amount: Any) -> str:
    if amount is None:
        return "none"
    value = float(amount)
    if value < 10:
        return "under 10"
    if value < 100:
        return "10–99"
    if value < 1000:
        return "100–999"
    return "1,000+"


def _hour_band(ts: Any) -> str:
    parsed = parse_timestamp(ts)
    if parsed is None:
        return "unknown"
    hour = parsed.astimezone(LOCAL_ZONE).hour
    return ("00–05", "06–11", "12–17", "18–23")[hour // 6]


def _message_text(payload: str) -> Optional[str]:
    match = MESSAGE_KEYS.search(payload)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except ValueError:
        return match.group(1)


def normalise_error(text: Optional[str]) -> str:
    if not text:
        return "none"
    text = re.sub(r"\d+", "N", text.strip())
    text = re.sub(r"\s+", " ", text)
    return text[:60]


def gateway_features(row: dict[str, Any]) -> dict[str, str]:
    """Features of one transaction_activity SEND_SMS row (provider, status, time, payload)."""
    lowered = {str(k).lower(): v for k, v in row.items()}
    payload = str(lowered.get("logged_responses") or "")
    phone = normalize_gateway_phone(payload)
    amount_match = AMOUNT_PATTERN.search(payload)
    message = _message_text(payload)
    kind = "unknown"
    probe = message or payload
    if DEBIT_WORDS.search(probe):
        kind = "debit"
    elif CREDIT_WORDS.search(probe):
        kind = "credit"
    if message is None:
        parts, encoding = "unknown", "unknown"
    else:
        unicode_ = any(ch not in GSM7 for ch in message)
        limit = 70 if unicode_ else 160
        parts = "1 SMS" if len(message) <= limit else "multi-part"
        encoding = "unicode (UCS-2)" if unicode_ else "GSM-7"
    error = ERROR_KEYS.search(payload)
    return {
        "provider": str(lowered.get("service_provider_name") or "unknown"),
        "network": network_of(phone),
        "phone_format": _phone_format(payload),
        "currency": normalize_currency(amount_match.group(1)) if amount_match else "none",
        "amount_band": _amount_band(to_decimal(amount_match.group(2)) if amount_match else None),
        "has_account": "yes" if ACCOUNT_SUFFIX_PATTERN.search(payload) else "no",
        "parts": parts,
        "encoding": encoding,
        "kind": kind,
        "error": normalise_error(error.group(1) if error else None),
        "hour_band": _hour_band(lowered.get("time_stamp")),
    }


def _band_count(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "unknown"
    return "none" if n == 0 else "1–3" if n <= 3 else "4–10" if n <= 10 else "over 10"


def account_facts(result: dict[str, Any], phone_used: Optional[str], branch: Optional[str]) -> dict[str, Any]:
    """Non-identifying facts from one check_sms_alert_account result (stored with the decision)."""
    debits = result.get("debits_result") if isinstance(result.get("debits_result"), dict) else result
    socket = debits.get("socket_delivery") if isinstance(debits.get("socket_delivery"), dict) else {}
    matches = socket.get("matches") or []
    lags = []
    for m in matches:
        sent, trig = parse_timestamp(_ddmmyyyy(m.get("sent_at"))), parse_timestamp(_ddmmyyyy(m.get("triggered_at")))
        if sent and trig:
            lags.append((sent - trig).total_seconds() / 60)
    lag = "no send" if not lags else ("under 5 min" if max(lags) < 5 else "5–60 min" if max(lags) < 60 else
                                      "1–6 h" if max(lags) < 360 else "over 6 h")
    email = debits.get("customer_email_usable")
    return {
        "status": result.get("status"),
        "alert_type": str(result.get("alert_type") or "DEBITS").lower(),
        "account_currency": str(result.get("account_currency") or "unknown"),
        "phone_network": network_of(normalize_gateway_phone(phone_used) if phone_used else None),
        "email_on_file": "unknown" if email is None else ("yes" if email else "no"),
        "cob_registered": {True: "on", False: "off", None: "no record"}.get(debits.get("cob_sms_alert"), "no record"),
        "trigger_band": _band_count(debits.get("standby_trigger_count")),
        "event": sorted({str(t.get("event")) for t in debits.get("recent_triggers") or [] if t.get("event")})[:3] or ["none"],
        "providers": sorted({str(m.get("service_provider")) for m in matches if m.get("service_provider")}) or ["none"],
        "dispatch_lag": lag,
        "branch": branch or "unknown",
    }


def _ddmmyyyy(value: Any) -> Any:
    """format_timestamp() gives 'dd-mm-yyyy HH:MM'; turn it back into ISO for parsing."""
    if isinstance(value, str):
        m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4}) (\d{2}:\d{2})", value)
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)} {m.group(4)}:00"
    return value


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
