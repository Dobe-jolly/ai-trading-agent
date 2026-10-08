"""Claude explains the analysis for the people on shift. It does not decide the severity.

Only aggregated numbers are sent (counts, rates, categories): no ticket text, no phone or
account numbers.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

SUMMARY_TOOL = {
    "name": "submit_summary",
    "description": "Submit the incident summary for the operations team.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "One line, under 110 characters, that names what is wrong and where."},
            "summary": {"type": "string", "description": "3-6 sentences: what changed, how big, since when, who is affected."},
            "likely_causes": {"type": "array", "items": {"type": "string"},
                              "description": "Hypotheses that the numbers support, most likely first, each saying which numbers point to it."},
            "actions": {"type": "array", "items": {"type": "string"},
                        "description": "Concrete next checks or actions for the ICT team, in order."},
            "branch_message": {"type": "string",
                               "description": "Optional short note the service desk could send to branches. Empty if nothing should be said yet."},
        },
        "required": ["headline", "summary", "likely_causes", "actions"],
    },
}


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
    if client is None:
        return fallback_summary(analysis)
    facts = {
        "severity_decided_by_rules": analysis["severity"],
        "signals": analysis["signals"],
        "failed_vs_delivered_gateway": analysis["comparisons"]["gateway"],
        "failed_vs_delivered_ticket_checks": analysis["comparisons"]["tickets"],
        "gateway_hourly_last_48h": data.get("gateway", {}).get("hourly", [])[-48:],
        "tickets_per_day": data.get("tickets", {}).get("daily", []),
        "unavailable_sources": {k: v["error"] for k, v in analysis["sources"].items() if not v["available"]},
    }
    try:
        response = client.messages.create(
            model=model,
            max_tokens=1200,
            system=prompt_file.read_text(encoding="utf-8"),
            tools=[SUMMARY_TOOL],
            tool_choice={"type": "tool", "name": "submit_summary"},
            messages=[{"role": "user", "content": "Analysis of the last 24 hours:\n" + json.dumps(facts, default=str)}],
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_summary":
                out = dict(block.input)
                out.setdefault("branch_message", "")
                out["by"] = "model"
                out["headline"] = str(out.get("headline", ""))[:160]
                return out
    except Exception as exc:  # noqa: BLE001 - any model failure falls back to the plain summary
        log.warning("Anomaly summary from the model failed (%s); using the plain summary", exc)
    return fallback_summary(analysis)


def admin_text(report: dict[str, Any], portal_url: Optional[str], resolved: bool = False) -> tuple[str, str, str]:
    """(sms, email subject, email body) for the admins."""
    s = report["summary"]
    level = "RESOLVED" if resolved else report["severity"].upper()
    sms = f"SMS Alerts {level}: {s['headline']}"
    if portal_url and not resolved:
        sms += f" Details: {portal_url}#anomalies"
    sms = sms[:300]
    subject = f"[SMS Alerts {level}] {s['headline']}"[:180]
    lines = [s["headline"], "", s["summary"], ""]
    if not resolved:
        if s.get("likely_causes"):
            lines += ["Likely causes:"] + [f"- {c}" for c in s["likely_causes"]] + [""]
        if s.get("actions"):
            lines += ["Next steps:"] + [f"{i}. {a}" for i, a in enumerate(s["actions"], 1)] + [""]
        lines += ["Signals:"] + [f"- [{x['severity']}] {x['label']}: {x['value']} (usual {x['usual']}). {x['detail']}"
                                 for x in report["analysis"]["signals"] if x["severity"] != "ok"] + [""]
        if s.get("branch_message"):
            lines += ["Suggested note to branches:", s["branch_message"], ""]
    if portal_url:
        lines.append(f"Portal: {portal_url}#anomalies")
    return sms, subject, "\n".join(lines).strip() + "\n"
