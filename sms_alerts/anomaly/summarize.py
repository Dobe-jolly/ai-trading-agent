"""Claude explains the analysis for the people on shift. It does not decide the severity.

Only aggregated numbers are sent (counts, rates, categories): no ticket text, no phone or
account numbers.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from agentcore.anomaly import summarize as _core
from agentcore.anomaly.summarize import SUMMARY_TOOL  # noqa: F401 - shared

log = logging.getLogger(__name__)

def fallback_summary(analysis: dict[str, Any]) -> dict[str, Any]:
    """Plain summary from the signals, used when the model is unavailable."""
    bad = [s for s in analysis["signals"] if s["severity"] != "ok"]
    findings = analysis["comparisons"]["gateway"]["findings"][:2] + analysis["comparisons"]["tickets"]["findings"][:2]
    if not bad:
        return {"headline": "SMS alerts look normal", "summary": "No signal is outside its usual range.",
                "likely_causes": [], "actions": [], "branch_message": "", "by": "rules"}
    worst = sorted(bad, key=lambda s: {"incident": 0, "watch": 1}[s["severity"]])[0]
    lines = [f"{s['label']}: {s['value'] if s['value'] is not None else ''} (usual {s['usual']}). {s['detail']}" for s in bad]
    causes = [f"Failures are concentrated in {f['label'].lower()} = {f['value']} "
              f"({f['failed_share']:.0%} of failures vs {f['ok_share']:.0%} of deliveries)" for f in findings]
    errors = analysis["comparisons"]["gateway"].get("errors") or []
    if errors:
        causes.append("Most common provider error: " + ", ".join(f"{e['value']} ({e['failed_share']:.0%} of failures)"
                                                                 for e in errors[:2]))
    return {"headline": f"{worst['label']}: {worst['value']} (usual {worst['usual']})",
            "summary": " ".join(lines), "likely_causes": causes,
            "actions": ["Check the SMS queue monitor and the gateway for the segments named above."],
            "branch_message": "", "by": "rules"}


def summarise(client: Any, model: str, prompt_file: Path, analysis: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    facts = {
        "severity_decided_by_rules": analysis["severity"],
        "signals": analysis["signals"],
        "failed_vs_delivered_gateway": analysis["comparisons"]["gateway"],
        "failed_vs_delivered_ticket_checks": analysis["comparisons"]["tickets"],
        "gateway_hourly_last_48h": data.get("gateway", {}).get("hourly", [])[-48:],
        "tickets_per_day": data.get("tickets", {}).get("daily", []),
        "unavailable_sources": {k: v["error"] for k, v in analysis["sources"].items() if not v["available"]},
    }
    return _core.summarise(client, model, prompt_file, facts, lambda: fallback_summary(analysis))


def admin_text(report: dict[str, Any], portal_url: Optional[str], resolved: bool = False) -> tuple[str, str, str]:
    """(sms, email subject, email body) for the admins."""
    return _core.admin_text(report, portal_url, resolved, product='SMS Alerts')
