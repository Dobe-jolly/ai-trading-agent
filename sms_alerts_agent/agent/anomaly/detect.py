"""Turning collected numbers into signals with a severity: ok, watch or incident.

Deterministic on purpose: the thresholds decide whether there is an incident; the model only
explains it. Every signal carries its current value, its usual value and the reason.
"""
from __future__ import annotations

import statistics
from collections import Counter
from typing import Any, Optional

from .features import compare

SEVERITY_RANK = {"ok": 0, "watch": 1, "incident": 2}
# The provider error text is not compared: delivered messages never carry one, so every error
# would look "infinitely" more common among failures. It is listed separately instead.
GATEWAY_FEATURES = ("provider", "network", "phone_format", "currency", "amount_band", "has_account", "parts",
                    "encoding", "kind", "hour_band")
TICKET_FEATURES = ("alert_type", "account_currency", "phone_network", "email_on_file", "cob_registered",
                   "trigger_band", "event", "providers", "dispatch_lag", "branch")


def _signal(key: str, label: str, severity: str, value: Any, usual: Any, detail: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "severity": severity, "value": value, "usual": usual, "detail": detail, **extra}


def _rate(counts: dict[str, float], good: str, bad: tuple[str, ...]) -> tuple[Optional[float], float]:
    g = counts.get(good, 0)
    b = sum(counts.get(k, 0) for k in bad)
    total = g + b
    return (g / total if total else None), total


def _drop_severity(drop_points: float, watch: float, incident: float) -> str:
    return "incident" if drop_points >= incident else "watch" if drop_points >= watch else "ok"


def ticket_volume(t: dict[str, Any], min_tickets: int) -> list[dict[str, Any]]:
    if not t.get("available"):
        return []
    current, baseline = t["current"], t["baseline"] or [0]
    median = statistics.median(baseline)
    mad = statistics.median([abs(x - median) for x in baseline]) * 1.4826
    spread = max(mad, median ** 0.5, 1.0)
    z = (current - median) / spread
    severity = "ok"
    if current >= min_tickets and z >= 2.5:
        severity = "watch"
    if current >= min_tickets and z >= 4 and current >= 2 * max(median, 1):
        severity = "incident"
    signals = [_signal("ticket_volume", "SMS Alerts tickets, last 24 h", severity, current, round(median, 1),
                       f"{current} tickets vs a usual {median:g} (typical range up to {median + 2.5 * spread:.0f})",
                       z=round(z, 1))]
    if severity != "ok":
        # Where the extra tickets come from.
        for kind, cur, base in (("branch", t["by_branch"], t["by_branch_baseline"]),
                                ("topic", t["by_topic"], t["by_topic_baseline"])):
            excess = sorted(((n - base.get(name, 0), name, n) for name, n in cur.items()), reverse=True)[:3]
            parts = [f"{name}: {n} (usual {base.get(name, 0):g})" for gap, name, n in excess if gap >= 2]
            if parts:
                signals.append(_signal(f"ticket_volume_{kind}", f"Most of the increase, by {kind}", "watch", None, None,
                                       "; ".join(parts)))
    return signals


def gateway(g: dict[str, Any], min_sends: int) -> list[dict[str, Any]]:
    if not g.get("available"):
        return []
    signals = []
    days = max(1, g.get("baseline_days") or 1)
    total_cur: dict[str, float] = {}
    total_base: dict[str, float] = {}
    for provider, counts in g["current"].items():
        for k, v in counts.items():
            total_cur[k] = total_cur.get(k, 0) + v
    for provider, counts in g["baseline_per_day"].items():
        for k, v in counts.items():
            total_base[k] = total_base.get(k, 0) + v

    rate, n = _rate(total_cur, "delivered", ("failed",))
    base_rate, base_n = _rate(total_base, "delivered", ("failed",))
    if rate is not None and base_rate is not None and n >= min_sends:
        drop = (base_rate - rate) * 100
        signals.append(_signal("gateway_delivery", "Gateway delivery rate, last 24 h", _drop_severity(drop, 8, 20),
                               f"{rate:.1%}", f"{base_rate:.1%}", f"{int(n)} sends with a final outcome; "
                               f"{drop:+.1f} points vs usual" if drop > 0 else f"{int(n)} sends; at or above usual"))
    # Per route: where the failures are.
    for provider, counts in sorted(g["current"].items()):
        p_rate, p_n = _rate(counts, "delivered", ("failed",))
        p_base, _ = _rate(g["baseline_per_day"].get(provider, {}), "delivered", ("failed",))
        if p_rate is None or p_base is None or p_n < max(30, min_sends // 5):
            continue
        drop = (p_base - p_rate) * 100
        severity = _drop_severity(drop, 10, 25)
        if severity != "ok":
            signals.append(_signal(f"gateway_route_{provider}", f"Delivery via {provider}", severity, f"{p_rate:.1%}",
                                   f"{p_base:.1%}", f"{int(p_n)} sends; {drop:+.1f} points vs usual", segment=provider))
    # The same per route over the last couple of hours: a sudden outage shows here first.
    # Fewer sends, so the thresholds are wider.
    for provider, counts in sorted((g.get("recent") or {}).items()):
        r_rate, r_n = _rate(counts, "delivered", ("failed",))
        p_base, _ = _rate(g["baseline_per_day"].get(provider, {}), "delivered", ("failed",))
        if r_rate is None or p_base is None or r_n < max(30, min_sends // 5):
            continue
        drop = (p_base - r_rate) * 100
        severity = _drop_severity(drop, 15, 30)
        if severity != "ok":
            signals.append(_signal(f"gateway_recent_{provider}", f"Delivery via {provider} since {g.get('recent_since', '')}",
                                   severity, f"{r_rate:.1%}", f"{p_base:.1%}",
                                   f"{int(r_n)} sends in the last hours; {drop:+.1f} points vs usual", segment=provider))
    # Volume: far fewer sends than usual means alerts are not reaching the gateway at all.
    sends = sum(total_cur.values())
    usual = sum(total_base.values())
    if usual >= min_sends:
        ratio = sends / usual if usual else 1
        severity = "incident" if ratio <= 0.3 else "watch" if ratio <= 0.6 else "ok"
        signals.append(_signal("gateway_volume", "SMS sends reaching the gateway, last 24 h", severity, int(sends),
                               int(usual), f"{ratio:.0%} of a usual day"))
    # Backlog: messages accepted but still undelivered after a couple of hours.
    stuck = g.get("stuck_pending", 0)
    usual_pending = total_base.get("pending", 0)
    if stuck >= 20 and stuck >= 3 * max(usual_pending, 1):
        severity = "incident" if stuck >= 10 * max(usual_pending, 1) and stuck >= 100 else "watch"
        signals.append(_signal("gateway_backlog", "Stuck at the gateway (awaiting delivery for 2 h+)", severity,
                               int(stuck), round(usual_pending), "messages accepted but not delivered"))
    return signals


def daily_share(source: str, label: str, d: dict[str, Any], good: str, min_count: int) -> list[dict[str, Any]]:
    if not d.get("available"):
        return []
    today, base = d["today"], d["baseline_total"]
    t_total, b_total = sum(today.values()), sum(base.values())
    if t_total < min_count or not b_total:
        return []
    share, usual = today.get(good, 0) / t_total, base.get(good, 0) / b_total
    drop = (usual - share) * 100
    return [_signal(f"{source}_rate", label, _drop_severity(drop, 8, 20), f"{share:.1%}", f"{usual:.1%}",
                    f"{int(t_total)} today; {drop:+.1f} points vs usual" if drop > 0 else f"{int(t_total)} today; normal")]


def ticket_checks(c: dict[str, Any], min_tickets: int) -> list[dict[str, Any]]:
    cur = c["current"]
    n_bad, n_ok = len(cur["not_delivered"]), len(cur["delivered"])
    total = n_bad + n_ok
    if total < min_tickets:
        return []
    base = c["baseline"]
    b_total = base["delivered"] + base["not_delivered"]
    share = n_bad / total
    usual = base["not_delivered"] / b_total if b_total else 0
    rise = (share - usual) * 100
    # A handful of tickets can swing the share a lot, so an incident needs ten or more accounts.
    severity = "incident" if rise >= 40 and n_bad >= 10 else "watch" if rise >= 20 and n_bad >= 4 else "ok"
    return [_signal("ticket_not_delivered", "Ticket accounts whose alerts did not reach the client", severity,
                    f"{share:.0%}", f"{usual:.0%}", f"{n_bad} of {total} accounts checked in the last 24 h")]


def analyse(data: dict[str, Any], min_tickets: int = 5, min_sends: int = 200) -> dict[str, Any]:
    signals = []
    signals += ticket_volume(data["tickets"], min_tickets)
    signals += gateway(data["gateway"], min_sends)
    signals += daily_share("dispatch", "Dispatch service: messages SENT today", data["dispatch"], "SENT", 50)
    signals += daily_share("cae", "CAE alert requests accepted today", data["cae"], "SUCCESS", 50)
    signals += ticket_checks(data["ticket_checks"], min_tickets)
    severity = max((s["severity"] for s in signals), key=SEVERITY_RANK.get, default="ok")

    samples = data["gateway_samples"]
    gateway_findings = compare(samples.get("failed", []), samples.get("delivered", []), GATEWAY_FEATURES) \
        if samples.get("available") else []
    failed = samples.get("failed", []) if samples.get("available") else []
    error_counts = Counter(f.get("error") for f in failed if f.get("error") not in (None, "none"))
    top_errors = [{"value": value, "failed_n": n, "failed_share": round(n / len(failed), 3)}
                  for value, n in error_counts.most_common(5)]
    tc = data["ticket_checks"]["current"]
    ticket_findings = compare(tc["not_delivered"], tc["delivered"], TICKET_FEATURES, min_failed=3, min_ratio=1.8)
    return {
        "severity": severity,
        "signals": signals,
        "comparisons": {
            "gateway": {"failed_n": len(samples.get("failed", [])), "delivered_n": len(samples.get("delivered", [])),
                        "findings": gateway_findings, "errors": top_errors},
            "tickets": {"failed_n": len(tc["not_delivered"]), "delivered_n": len(tc["delivered"]),
                        "findings": ticket_findings},
        },
        "sources": {k: {"available": bool(v.get("available")), "error": v.get("error")}
                    for k, v in data.items() if isinstance(v, dict) and "available" in v},
    }


def incident_key(analysis: dict[str, Any]) -> str:
    """Identity of what is wrong, so a continuing incident is not re-announced every hour."""
    keys = sorted(s["key"] for s in analysis["signals"] if s["severity"] != "ok" and s["value"] is not None)
    return "|".join(keys)
