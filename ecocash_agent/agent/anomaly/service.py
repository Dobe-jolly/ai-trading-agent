"""Running the anomaly watch (agentcore.anomaly.service) with this agent's checks, summaries and kept data."""
from __future__ import annotations

import json
from typing import Any

from agentcore.anomaly import service as _core
from agentcore.anomaly.service import SCHEMA  # noqa: F401 - used by tools/build_preview.py
from .detect import analyse
from .summarize import admin_text, fallback_summary, summarise


def _slim(data: dict[str, Any]) -> dict[str, Any]:
    """What is kept with a report: totals and series, not the per-message samples."""
    out = json.loads(json.dumps(data, default=str))
    samples = out.get("samples", {})
    for key in ("failed", "linked"):
        if isinstance(samples.get(key), list):
            samples[key] = len(samples[key])
    return out


class AnomalyService(_core.AnomalyService):
    fallback_summary = staticmethod(fallback_summary)
    summarise = staticmethod(summarise)
    admin_text = staticmethod(admin_text)
    slim = staticmethod(_slim)

    def analyse(self, data: dict[str, Any]) -> dict[str, Any]:
        return analyse(data, min_tickets=self.s.anomaly_min_tickets, min_attempts=self.s.anomaly_min_attempts)
