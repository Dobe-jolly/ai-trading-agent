"""Collecting what the anomaly watch compares, through Metabase and the agent's own log.

Sources (each optional: one that fails is reported as unavailable and the rest still runs):
- osTicket: EcoCash tickets created, by topic and by the owner's organisation (branch)
- ecocash_monitor.ecocash_link_attempts: every link and delink request and its outcome, per
  hour, plus samples of failed and linked attempts for comparison

Complete days are cached locally (metric_daily), so after the first run each hourly analysis
only queries the current window. The baseline only uses days since the Python monitor started
recording (days before that would read as zero).
"""
from __future__ import annotations

import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Optional

from agentcore import sql
from ..ecocash import AttemptStore
from agentcore.metabase import MetabaseClient, MetabaseError
from ..normalize import LOCAL_ZONE, now_local, parse_timestamp
from ..osticket import TOPIC_JOINS, TOPIC_PATH, topic_condition
from .features import attempt_features
from agentcore.anomaly.common import METRIC_SCHEMA, RECENT_HOURS, MetricCache

log = logging.getLogger(__name__)

class Collector(MetricCache):
    def __init__(self, metabase: MetabaseClient, db: sqlite3.Connection, *, ticket_db: Optional[int],
                 attempts: Optional[AttemptStore], topics: list[str], window_hours: int = 24,
                 baseline_days: int = 14, sample_size: int = 400) -> None:
        self.mb = metabase
        self.db = db
        self.ticket_db = ticket_db
        self.attempts = attempts
        self.topics = topics
        self.window_hours = window_hours
        self.baseline_days = baseline_days
        self.sample_size = sample_size
        db.executescript(METRIC_SCHEMA)

    # ---- helpers ------------------------------------------------------------------
    def _baseline_days(self, today: date, first: Optional[date] = None) -> list[date]:
        days = [today - timedelta(days=i) for i in range(self.baseline_days, 0, -1)]
        return [d for d in days if first is None or d > first]

    # ---- osTicket -------------------------------------------------------------------
    def tickets(self, now: datetime) -> dict[str, Any]:
        if self.ticket_db is None:
            return {"available": False, "error": "ticket database not configured"}
        since = (now - timedelta(days=self.baseline_days + 1)).astimezone(LOCAL_ZONE)
        query = f"""SELECT t.ticket_id, t.created, {TOPIC_PATH} AS help_topic, o.name AS org_name
FROM ost_ticket t
{TOPIC_JOINS}
LEFT JOIN ost_user u ON (u.id = t.user_id)
LEFT JOIN ost_organization o ON (o.id = u.org_id)
WHERE t.created >= '{sql.mysql_text(since.strftime("%Y-%m-%d %H:%M:%S"))}'
  AND {topic_condition(self.topics)}"""
        try:
            rows = self.mb.query(self.ticket_db, query)
        except MetabaseError as exc:
            return {"available": False, "error": str(exc)[:300]}
        windows: list[list[dict[str, Any]]] = [[] for _ in range(self.baseline_days + 1)]
        for row in rows:
            created = parse_timestamp(row.get("created"))
            if created is None:
                continue
            index = int((now - created).total_seconds() // (self.window_hours * 3600))
            if 0 <= index < len(windows):
                windows[index].append(row)
        current, baseline = windows[0], windows[1:]

        def by(key: str, items: list[dict[str, Any]]) -> Counter:
            return Counter(str(r.get(key) or "unknown") for r in items)

        base_topic: dict[str, list[int]] = defaultdict(list)
        base_org: dict[str, list[int]] = defaultdict(list)
        for w in baseline:
            for name, n in by("help_topic", w).items():
                base_topic[name].append(n)
            for name, n in by("org_name", w).items():
                base_org[name].append(n)
        today = now.astimezone(LOCAL_ZONE).date()
        daily = Counter()
        for row in rows:
            created = parse_timestamp(row.get("created"))
            if created:
                daily[created.astimezone(LOCAL_ZONE).date().isoformat()] += 1
        return {
            "available": True,
            "current": len(current),
            "baseline": [len(w) for w in baseline],
            "by_topic": dict(by("help_topic", current)),
            "by_topic_baseline": {k: round(sum(v) / len(baseline), 2) for k, v in base_topic.items()},
            "by_branch": dict(by("org_name", current)),
            "by_branch_baseline": {k: round(sum(v) / len(baseline), 2) for k, v in base_org.items()},
            "daily": [{"day": (today - timedelta(days=i)).isoformat(),
                       "count": daily.get((today - timedelta(days=i)).isoformat(), 0)}
                      for i in range(self.baseline_days, -1, -1)],
        }

    # ---- link attempts -------------------------------------------------------------------
    def attempts_data(self, now: datetime) -> dict[str, Any]:
        if self.attempts is None:
            return {"available": False, "error": "ecocash_monitor database not available"}
        start = now - timedelta(hours=max(48, self.window_hours))
        try:
            first = self.attempts.attempts_start()
            rows = self.attempts.counts(start, now, by_hour=True)
        except MetabaseError as exc:
            return {"available": False, "error": str(exc)[:300]}
        if first is None:
            return {"available": False, "error": "no attempts recorded yet (is the monitor running?)"}
        window_start = now - timedelta(hours=self.window_hours)
        recent_start = now.astimezone(LOCAL_ZONE).replace(minute=0, second=0, microsecond=0) - timedelta(hours=RECENT_HOURS)
        current: Counter = Counter()
        recent: Counter = Counter()
        by_via: dict[str, Counter] = defaultdict(Counter)
        hourly: dict[str, Counter] = defaultdict(Counter)
        for row in rows:
            at = parse_timestamp(row.get("bucket"))
            n = int(row.get("n") or 0)
            if at is None:
                continue
            kind = str(row.get("kind") or "unknown")
            if str(row.get("operation")) == "UNLINK":
                kind = "delink_" + ("ok" if kind == "unlinked" else "failed")
            hourly[at.astimezone(LOCAL_ZONE).strftime("%Y-%m-%dT%H:00")][_bucket(kind)] += n
            if at >= window_start:
                current[kind] += n
                by_via[str(row.get("via") or "unknown")][_bucket(kind)] += n
            if at >= recent_start:
                recent[kind] += n

        def fetch(day: date) -> dict[str, float]:
            day_start = datetime.combine(day, datetime.min.time(), tzinfo=LOCAL_ZONE)
            values: Counter = Counter()
            for r in self.attempts.counts(day_start, day_start + timedelta(days=1), by_hour=False):  # type: ignore[union-attr]
                kind = str(r.get("kind") or "unknown")
                if str(r.get("operation")) == "UNLINK":
                    kind = "delink_" + ("ok" if kind == "unlinked" else "failed")
                values[kind] += int(r.get("n") or 0)
            return dict(values)

        days, errors = self._cached_days("attempts", self._baseline_days(now.astimezone(LOCAL_ZONE).date(),
                                                                         first.astimezone(LOCAL_ZONE).date()), fetch)
        baseline: Counter = Counter()
        for values in days.values():
            baseline.update(values)
        n_days = len(days)
        return {
            "available": True,
            "current": dict(current),
            "recent": dict(recent),
            "recent_since": recent_start.strftime("%H:00"),
            "by_via": {k: dict(v) for k, v in by_via.items()},
            "baseline_per_day": {k: round(v / n_days, 1) for k, v in baseline.items()} if n_days else {},
            "baseline_days": n_days,
            "recording_since": first.isoformat(),
            "hourly": [{"hour": h, **{b: c.get(b, 0) for b in ("linked", "failed", "no_response", "delink")}}
                       for h, c in sorted(hourly.items())],
            "errors": errors,
        }

    def samples(self, now: datetime) -> dict[str, Any]:
        """Failed link attempts and linked ones from the window, described as features."""
        if self.attempts is None:
            return {"available": False, "error": "ecocash_monitor database not available"}
        start = now - timedelta(hours=self.window_hours)
        try:
            failed = self.attempts.sample(start, now, "operation = 'LINK' AND outcome IN ('failed', 'no_response')",
                                          self.sample_size)
            ok = self.attempts.sample(start, now, "operation = 'LINK' AND outcome = 'linked'", self.sample_size)
        except MetabaseError as exc:
            return {"available": False, "error": str(exc)[:300]}
        from ..ecocash import classify

        def with_error(row: dict[str, Any]) -> dict[str, str]:
            f = attempt_features(row)
            lowered = {str(k).lower(): v for k, v in row.items()}
            f["error_type"] = ("no_response" if lowered.get("outcome") == "no_response"
                               else classify(lowered.get("response_code"), lowered.get("message")).key)
            return f

        return {"available": True, "failed": [with_error(r) for r in failed],
                "linked": [attempt_features(r) for r in ok]}

    def collect(self, now: Optional[datetime] = None) -> dict[str, Any]:
        now = now or now_local()
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "window_hours": self.window_hours,
            "baseline_days": self.baseline_days,
            "tickets": self.tickets(now),
            "attempts": self.attempts_data(now),
            "samples": self.samples(now),
        }


def _bucket(kind: str) -> str:
    if kind == "linked":
        return "linked"
    if kind == "no_response":
        return "no_response"
    if kind.startswith("delink_"):
        return "delink"
    return "failed"


__all__ = ["Collector", "RECENT_HOURS"]
