"""Running the anomaly watch, keeping its reports, alerting admins and briefing the ticket agent."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from agentcore.audit import AuditLog, now_iso
from ..notify import Notifier
from .collect import Collector
from .detect import SEVERITY_RANK, analyse, incident_key
from .summarize import admin_text, fallback_summary, summarise

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS anomaly_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    severity TEXT NOT NULL,
    incident_key TEXT,
    headline TEXT,
    summary TEXT,
    analysis TEXT,
    data TEXT,
    alert TEXT
);
CREATE INDEX IF NOT EXISTS idx_anomaly_created ON anomaly_reports(created_at);
CREATE TABLE IF NOT EXISTS anomaly_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    active INTEGER NOT NULL DEFAULT 0,
    incident_key TEXT,
    started_at TEXT,
    last_alert_at TEXT
);
"""


def _slim(data: dict[str, Any]) -> dict[str, Any]:
    """What is kept with a report: totals and series, not the per-message samples."""
    out = json.loads(json.dumps(data, default=str))
    samples = out.get("samples", {})
    for key in ("failed", "linked"):
        if isinstance(samples.get(key), list):
            samples[key] = len(samples[key])
    return out


class AnomalyService:
    def __init__(self, settings: Any, audit: AuditLog, collector: Optional[Collector], client: Any,
                 notifier: Optional[Notifier]) -> None:
        self.s = settings
        self.audit = audit
        self.collector = collector
        self.client = client
        self.notifier = notifier
        audit.db.executescript(SCHEMA)

    # ---- for the ticket agent ---------------------------------------------------------
    def latest(self) -> Optional[dict[str, Any]]:
        row = self.audit.db.execute("SELECT * FROM anomaly_reports ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def context(self, max_age_hours: float = 3.0) -> Optional[str]:
        """One-line briefing for the ticket agent while an incident or watch is open."""
        report = self.latest()
        if not report or report["severity"] == "ok":
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(report["created_at"])
        if age > timedelta(hours=max_age_hours):
            return None
        summary = json.loads(report["summary"] or "{}")
        causes = "; ".join(summary.get("likely_causes", [])[:2])
        return (f"{report['severity'].upper()} (anomaly watch, {report['created_at'][11:16]} UTC): {report['headline']}."
                + (f" Likely: {causes}." if causes else ""))

    # ---- running -------------------------------------------------------------------------
    def due(self) -> bool:
        row = self.audit.db.execute("SELECT MAX(created_at) FROM anomaly_reports").fetchone()
        if not row or not row[0]:
            return True
        return datetime.now(timezone.utc) - datetime.fromisoformat(row[0]) >= timedelta(minutes=self.s.anomaly_interval_minutes)

    def maybe_run(self) -> Optional[dict[str, Any]]:
        if not self.s.anomaly_enabled or self.collector is None or not self.due():
            return None
        try:
            return self.run()
        except Exception:  # noqa: BLE001 - the watch must never break a ticket pass
            log.exception("Anomaly analysis failed")
            return None

    def run(self, alert: bool = True, use_model: bool = True) -> dict[str, Any]:
        assert self.collector is not None
        data = self.collector.collect()
        analysis = analyse(data, min_tickets=self.s.anomaly_min_tickets, min_attempts=self.s.anomaly_min_attempts)
        if analysis["severity"] == "ok" or not use_model:
            summary = fallback_summary(analysis)
        else:
            summary = summarise(self.client, self.s.anomaly_model or self.s.model, self.s.anomaly_prompt_file, analysis, data)
        report = {"created_at": now_iso(), "severity": analysis["severity"], "incident_key": incident_key(analysis),
                  "summary": summary, "analysis": analysis}
        alert_sent = self._alert(report) if alert else None
        cursor = self.audit.db.execute(
            "INSERT INTO anomaly_reports (created_at, severity, incident_key, headline, summary, analysis, data, alert) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (report["created_at"], report["severity"], report["incident_key"], summary["headline"],
             json.dumps(summary), json.dumps(analysis, default=str), json.dumps(_slim(data)), alert_sent))
        self.audit.db.commit()
        report["id"] = cursor.lastrowid
        report["alert"] = alert_sent
        log.info("Anomaly watch: %s — %s%s", report["severity"], summary["headline"],
                 f" (alert: {alert_sent})" if alert_sent else "")
        return report

    def _alert(self, report: dict[str, Any]) -> Optional[str]:
        """Alert on a new or changed incident, remind while it lasts, and say when it is over."""
        db = self.audit.db
        state = db.execute("SELECT * FROM anomaly_state WHERE id = 1").fetchone()
        active = bool(state and state["active"])
        alerting = SEVERITY_RANK[report["severity"]] >= SEVERITY_RANK[self.s.anomaly_alert_level]
        now = datetime.now(timezone.utc)
        stamp = now.isoformat(timespec="seconds")
        kind: Optional[str] = None
        if alerting:
            # Signals at the alerting level so far in this incident. A new one is an update; one that
            # eases and comes back is not, so a signal hovering at its threshold is announced once.
            previous = set(filter(None, (state["incident_key"] or "").split("|"))) if active else set()
            level = SEVERITY_RANK[self.s.anomaly_alert_level]
            current = {s["key"] for s in report["analysis"]["signals"]
                       if SEVERITY_RANK[s["severity"]] >= level and s["value"] is not None}
            last = state["last_alert_at"] if active else None
            if not active:
                kind = "incident"
            elif current - previous:
                kind = "update"
            elif last is None or now - datetime.fromisoformat(last) >= timedelta(hours=self.s.anomaly_realert_hours):
                kind = "reminder"
            started = state["started_at"] if active and state["started_at"] else stamp
            db.execute("INSERT OR REPLACE INTO anomaly_state (id, active, incident_key, started_at, last_alert_at) "
                       "VALUES (1, 1, ?, ?, ?)", ("|".join(sorted(previous | current)), started, stamp if kind else last))
        elif active and report["severity"] == "ok":
            # Only over when everything is back to normal: an incident easing to a watch stays open,
            # so a signal hovering around the threshold does not send incident/resolved pairs.
            kind = "resolved"
            db.execute("INSERT OR REPLACE INTO anomaly_state (id, active, incident_key, started_at, last_alert_at) "
                       "VALUES (1, 0, NULL, NULL, ?)", (stamp,))
        db.commit()
        if kind is None or self.notifier is None:
            return kind
        sms, subject, body = admin_text(report, self.s.portal_url, resolved=(kind == "resolved"))
        if kind == "reminder":
            subject, sms = subject.replace("]", " still open]", 1), sms.replace(":", " (still open):", 1)
        self.notifier.enqueue_admin(kind=f"admin_{kind}", subject=subject, email_body=body, sms_text=sms)
        self.notifier.dispatch(self.s.notify_max_attempts)
        return kind
