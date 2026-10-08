"""Turning collected numbers into signals with a severity: ok, watch or incident.

Deterministic on purpose: the thresholds decide whether there is an incident; the model only
explains it. Every signal carries its current value, its usual value and the reason.
"""
from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

from ..ecocash import BY_KEY
from agentcore.anomaly.common import SEVERITY_RANK, _signal, incident_key  # noqa: F401 - shared
from .features import compare

# The EcoCash error itself is listed separately: linked attempts never carry one. "via" already tells
# USSD, branch staff and account opening apart, so the raw channel would only repeat it.
ATTEMPT_FEATURES = ("via", "currency", "network", "mobile_format", "cif_series", "product", "id_format",
                    "hour_band")
FAILED_KINDS = tuple(k for k in BY_KEY)  # error types, plus unknown and no_response


def _failed(counts: dict[str, float]) -> float:
    return sum(v for k, v in counts.items() if k in FAILED_KINDS)


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
    signals = [_signal("ticket_volume", "EcoCash tickets, last 24 h", severity, current, round(median, 1),
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


def attempts(a: dict[str, Any], min_attempts: int) -> list[dict[str, Any]]:
    if not a.get("available"):
        return []
    signals: list[dict[str, Any]] = []
    cur, base, days = a["current"], a.get("baseline_per_day") or {}, a.get("baseline_days") or 0
    if days < 3:
        return [_signal("baseline", "Baseline", "ok", None, None,
                        f"Recording since {a.get('recording_since', '?')[:10]}: {days} complete day(s) so far; signals "
                        "start after 3.")]
    linked, failed = cur.get("linked", 0), _failed(cur)
    b_linked, b_failed = base.get("linked", 0), _failed(base)
    total, b_total = linked + failed, b_linked + b_failed
    # 1. Failure rate over the window.
    if total >= min_attempts and b_total:
        rate, usual = failed / total, b_failed / b_total
        rise = (rate - usual) * 100
        severity = "incident" if rise >= 25 else "watch" if rise >= 10 else "ok"
        signals.append(_signal("failure_rate", "Link failures, last 24 h", severity, f"{rate:.0%}", f"{usual:.0%}",
                               f"{int(failed)} of {int(total)} link requests failed; {rise:+.0f} points vs usual"))
    # 2. Each kind of failure against its usual daily count (an outage, a new bank-side error...).
    for kind in FAILED_KINDS:
        n, usual = cur.get(kind, 0), base.get(kind, 0)
        if n < 5:
            continue
        ratio = n / max(usual, 1.0)
        severity = "incident" if ratio >= 5 and n >= 15 else "watch" if ratio >= 3 else "ok"
        if kind in ("unknown",) and n >= 5:
            severity = "incident" if n >= 15 else max(severity, "watch", key=SEVERITY_RANK.get)
        if severity != "ok":
            label = BY_KEY[kind].label if kind in BY_KEY else kind
            signals.append(_signal(f"kind_{kind}", f"{label}, last 24 h", severity, int(n), usual,
                                   f"{n} vs a usual {usual:g} a day ({ratio:.1f}x)", segment=kind))
    # 3. The last few hours: a sudden outage shows here within the hour.
    recent = a.get("recent") or {}
    r_linked, r_failed = recent.get("linked", 0), _failed(recent)
    if r_linked + r_failed >= 8 and b_total:
        r_rate, usual = r_failed / (r_linked + r_failed), b_failed / b_total
        rise = (r_rate - usual) * 100
        severity = "incident" if rise >= 40 else "watch" if rise >= 25 else "ok"
        if severity != "ok":
            signals.append(_signal("failure_rate_recent", f"Link failures since {a.get('recent_since', '')}", severity,
                                   f"{r_rate:.0%}", f"{usual:.0%}",
                                   f"{int(r_failed)} of {int(r_linked + r_failed)} failed; {rise:+.0f} points vs usual"))
    sys_recent = recent.get("ecocash_system_error", 0) + recent.get("no_response", 0)
    if sys_recent >= 5:
        signals.append(_signal("ecocash_errors_recent", f"EcoCash system errors and timeouts since {a.get('recent_since', '')}",
                               "incident" if sys_recent >= 10 else "watch", int(sys_recent), None,
                               "EcoCash may be having an outage"))
    # 4. Volume: far fewer requests than usual means the service (or the monitor) may be down.
    usual_total = sum(v for k, v in base.items())
    now_total = sum(v for k, v in cur.items())
    if usual_total >= min_attempts:
        ratio = now_total / usual_total if usual_total else 1
        severity = "incident" if ratio <= 0.3 else "watch" if ratio <= 0.6 else "ok"
        signals.append(_signal("volume", "Link and delink requests, last 24 h", severity, int(now_total),
                               int(usual_total), f"{ratio:.0%} of a usual day (the registration service or the monitor "
                               "may be down)" if severity != "ok" else f"{ratio:.0%} of a usual day"))
    return signals


def analyse(data: dict[str, Any], min_tickets: int = 5, min_attempts: int = 30) -> dict[str, Any]:
    signals = []
    signals += ticket_volume(data["tickets"], min_tickets)
    signals += attempts(data["attempts"], min_attempts)
    severity = max((s["severity"] for s in signals), key=SEVERITY_RANK.get, default="ok")
    samples = data["samples"]
    failed = samples.get("failed", []) if samples.get("available") else []
    linked = samples.get("linked", []) if samples.get("available") else []
    findings = compare(failed, linked, ATTEMPT_FEATURES) if failed and linked else []
    error_counts = Counter(f.get("error_type") for f in failed if f.get("error_type"))
    top_errors = [{"value": BY_KEY[k].label if k in BY_KEY else k, "key": k, "failed_n": n,
                   "failed_share": round(n / len(failed), 3)} for k, n in error_counts.most_common(6)]
    return {
        "severity": severity,
        "signals": signals,
        "comparisons": {
            "attempts": {"failed_n": len(failed), "linked_n": len(linked), "findings": findings, "errors": top_errors},
        },
        "sources": {k: {"available": bool(v.get("available")), "error": v.get("error")}
                    for k, v in data.items() if isinstance(v, dict) and "available" in v},
    }

