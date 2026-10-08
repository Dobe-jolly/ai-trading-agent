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
    findings = analysis["comparisons"]["attempts"]["findings"][:3]
    if not bad:
        return {"headline": "EcoCash linking looks normal", "summary": "No signal is outside its usual range.",
                "likely_causes": [], "actions": [], "branch_message": "", "by": "rules"}
    worst = sorted(bad, key=lambda s: {"incident": 0, "watch": 1}[s["severity"]])[0]
    lines = [f"{s['label']}: {s['value'] if s['value'] is not None else ''} (usual {s['usual']}). {s['detail']}" for s in bad]
    causes = [f"Failures are concentrated in {f['label'].lower()} = {f['value']} "
              f"({f['failed_share']:.0%} of failures vs {f['ok_share']:.0%} of successful links)" for f in findings]
    errors = analysis["comparisons"]["attempts"].get("errors") or []
    if errors:
        causes.append("Most common EcoCash error: " + ", ".join(f"{e['value']} ({e['failed_share']:.0%} of failures)"
                                                                 for e in errors[:2]))
    return {"headline": f"{worst['label']}: {worst['value']} (usual {worst['usual']})",
            "summary": " ".join(lines), "likely_causes": causes,
            "actions": ["Check ecocashreg.service and the EcoCash API for the segments named above."],
            "branch_message": "", "by": "rules"}


def summarise(client: Any, model: str, prompt_file: Path, analysis: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    facts = {
        "severity_decided_by_rules": analysis["severity"],
        "signals": analysis["signals"],
        "failed_vs_linked_attempts": analysis["comparisons"]["attempts"],
        "attempts_hourly_last_48h": data.get("attempts", {}).get("hourly", [])[-48:],
        "tickets_per_day": data.get("tickets", {}).get("daily", []),
        "unavailable_sources": {k: v["error"] for k, v in analysis["sources"].items() if not v["available"]},
    }
    return _core.summarise(client, model, prompt_file, facts, lambda: fallback_summary(analysis))


def admin_text(report: dict[str, Any], portal_url: Optional[str], resolved: bool = False) -> tuple[str, str, str]:
    """(sms, email subject, email body) for the admins."""
    return _core.admin_text(report, portal_url, resolved, product='EcoCash')
