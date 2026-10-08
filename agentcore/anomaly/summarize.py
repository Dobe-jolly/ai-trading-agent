"""Claude explains the analysis for the people on shift. It does not decide the severity.

Only aggregated numbers are sent (counts, rates, categories): no ticket text, no phone or
account numbers. Each agent builds the facts and its own plain summary for when the model is unavailable.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

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


def summarise(client: Any, model: str, prompt_file: Path, facts: dict[str, Any],
              fallback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    if client is None:
        return fallback()
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
    return fallback()


def admin_text(report: dict[str, Any], portal_url: Optional[str], resolved: bool = False, *,
               product: str = "Agent") -> tuple[str, str, str]:
    """(sms, email subject, email body) for the admins."""
    s = report["summary"]
    level = "RESOLVED" if resolved else report["severity"].upper()
    sms = f"{product} {level}: {s['headline']}"
    if portal_url and not resolved:
        sms += f" Details: {portal_url}#anomalies"
    sms = sms[:300]
    subject = f"[{product} {level}] {s['headline']}"[:180]
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
