"""Collecting what the anomaly watch compares, through Metabase and the agent's own log.

Every source is optional: a source that fails is reported as unavailable and the rest of the
analysis still runs. Complete days are cached locally (metric_daily), so after the first run
each hourly analysis only queries the current window.

Sources (all columns already used by the Java agent):
- osTicket: SMS Alerts tickets created, by topic and by the owner's organisation (branch)
- SOCKET SMS DB public.transaction_activity: SEND_SMS outcomes by gateway route and hour,
  plus samples of failed and delivered sends for comparison
- SMS dispatch DB sms_message: dispatch_status per day
- STANDBY DB NMBPRODCAE.AUDIT_LETTER_MGMT_REQ: CAE request status per day
- the agent's decisions: per-account facts from the checks it ran on tickets
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .. import sql
from ..metabase import MetabaseClient, MetabaseError
from ..normalize import LOCAL_ZONE, now_local, parse_timestamp
from ..osticket import TOPIC_FILTER, TOPIC_JOINS, TOPIC_PATH
from .features import account_facts, gateway_features

log = logging.getLogger(__name__)

GATEWAY_WHERE = ("service_type = 'VALUE_ADDED_SERVICES' AND service_operation_type = 'SEND_SMS' "
                 "AND COALESCE(service_provider_name, '') <> 'WHATSAPP-META'")
GATEWAY_STATUS = {"DELIVERED_SUCCESSFULLY": "delivered", "FAILED_TO_SEND": "failed", "FAILED_TO_DELIVER": "failed",
                  "SENT_AWAITING_DELIVERY": "pending"}
RECENT_HOURS = 2
NOT_DELIVERED = {"socket_failed", "socket_not_found", "socket_pending", "socket_unknown", "sms_no_customer_email"}
METRIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_daily (
    source TEXT NOT NULL, day TEXT NOT NULL, key TEXT NOT NULL, value REAL NOT NULL,
    PRIMARY KEY (source, day, key)
);
"""


def gateway_bucket(status: Any) -> str:
    return GATEWAY_STATUS.get(str(status or "").upper(), "other")


class Collector:
    def __init__(self, metabase: MetabaseClient, db: sqlite3.Connection, *, ticket_db: Optional[int],
                 socket_db: Optional[int], dispatch_db: Optional[int], standby_db: Optional[int],
                 window_hours: int = 24, baseline_days: int = 14, sample_size: int = 600,
                 stuck_pending_hours: int = 2) -> None:
        self.mb = metabase
        self.db = db
        self.ticket_db, self.socket_db, self.dispatch_db, self.standby_db = ticket_db, socket_db, dispatch_db, standby_db
        self.window_hours = window_hours
        self.baseline_days = baseline_days
        self.sample_size = sample_size
        self.stuck_hours = stuck_pending_hours
        db.executescript(METRIC_SCHEMA)

    # ---- helpers ------------------------------------------------------------------
    def _cached_days(self, source: str, days: list[date],
                     fetch: Callable[[date], dict[str, float]]) -> tuple[dict[str, dict[str, float]], list[str]]:
        """Per-day metrics for complete days, from cache or fetched (and then cached)."""
        out: dict[str, dict[str, float]] = {}
        errors: list[str] = []
        for day in days:
            key = day.isoformat()
            rows = self.db.execute("SELECT key, value FROM metric_daily WHERE source = ? AND day = ?", (source, key)).fetchall()
            if rows:
                out[key] = {r[0]: r[1] for r in rows if r[0] != "__empty__"}
                continue
            try:
                values = fetch(day)
            except (MetabaseError, sql.UnsafeValue) as exc:
                errors.append(f"{key}: {exc}")
                continue
            out[key] = values
            with self.db:
                self.db.executemany("INSERT OR REPLACE INTO metric_daily (source, day, key, value) VALUES (?, ?, ?, ?)",
                                    [(source, key, k, v) for k, v in values.items()] or [(source, key, "__empty__", 0)])
        return out, errors

    def _baseline_days(self, today: date) -> list[date]:
        return [today - timedelta(days=i) for i in range(self.baseline_days, 0, -1)]

    # ---- osTicket -------------------------------------------------------------------
    def tickets(self, now: datetime) -> dict[str, Any]:
        if self.ticket_db is None:
            return {"available": False, "error": "ticket database not configured"}
        since = (now - timedelta(days=self.baseline_days + 1)).astimezone(LOCAL_ZONE)
        query = f"""SELECT t.ticket_id, MAX(t.created) AS created, MAX({TOPIC_PATH}) AS help_topic, MAX(o.name) AS org_name
FROM ost_ticket t
{TOPIC_JOINS}
LEFT JOIN ost_user u ON (u.id = t.user_id)
LEFT JOIN ost_organization o ON (o.id = u.org_id)
WHERE t.created >= '{sql.mysql_text(since.strftime("%Y-%m-%d %H:%M:%S"))}'
  AND {TOPIC_FILTER}
GROUP BY t.ticket_id"""
        try:
            rows = self.mb.query(self.ticket_db, query)
        except MetabaseError as exc:
            return {"available": False, "error": str(exc)[:300]}
        # Rolling 24-hour windows ending now: index 0 is the current window.
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

    # ---- SMS gateway --------------------------------------------------------------------
    def _gateway_grouped(self, start: datetime, end: datetime, by_hour: bool) -> list[dict[str, Any]]:
        hour = "date_trunc('hour', time_stamp) AS hour, " if by_hour else ""
        group = "1, 2, 3" if by_hour else "1, 2"
        query = (f"SELECT {hour}COALESCE(service_provider_name, 'unknown') AS provider, external_status, COUNT(*) AS n "
                 f"FROM public.transaction_activity WHERE {GATEWAY_WHERE} "
                 f"AND time_stamp >= {sql.pg_timestamptz(start)} AND time_stamp < {sql.pg_timestamptz(end)} "
                 f"GROUP BY {group}")
        return self.mb.query(self.socket_db, query)  # type: ignore[arg-type]

    def gateway(self, now: datetime) -> dict[str, Any]:
        if self.socket_db is None:
            return {"available": False, "error": "socket SMS database not available"}
        start = now - timedelta(hours=max(48, self.window_hours))
        try:
            hourly_rows = self._gateway_grouped(start, now, by_hour=True)
        except MetabaseError as exc:
            return {"available": False, "error": str(exc)[:300]}
        window_start = now - timedelta(hours=self.window_hours)
        stuck_before = now - timedelta(hours=self.stuck_hours)
        # The last two full hours plus the current one: catches a sudden failure that the
        # 24-hour figures would only show hours later.
        recent_start = now.astimezone(LOCAL_ZONE).replace(minute=0, second=0, microsecond=0) - timedelta(hours=RECENT_HOURS)
        recent: dict[str, Counter] = defaultdict(Counter)
        current: dict[str, Counter] = defaultdict(Counter)
        hourly: dict[str, Counter] = defaultdict(Counter)
        stuck = 0
        for row in hourly_rows:
            at = parse_timestamp(row.get("hour"))
            bucket = gateway_bucket(row.get("external_status"))
            n = int(row.get("n") or 0)
            if at is None:
                continue
            hourly[at.astimezone(LOCAL_ZONE).strftime("%Y-%m-%dT%H:00")][bucket] += n
            if at >= recent_start:
                recent[str(row.get("provider"))][bucket] += n
            if at >= window_start:
                current[str(row.get("provider"))][bucket] += n
                if bucket == "pending" and at < stuck_before:
                    stuck += n

        def fetch(day: date) -> dict[str, float]:
            day_start = datetime.combine(day, datetime.min.time(), tzinfo=LOCAL_ZONE)
            values: Counter = Counter()
            for row in self._gateway_grouped(day_start, day_start + timedelta(days=1), by_hour=False):
                values[f"{row.get('provider')}|{gateway_bucket(row.get('external_status'))}"] += int(row.get("n") or 0)
            return dict(values)

        days, errors = self._cached_days("gateway", self._baseline_days(now.astimezone(LOCAL_ZONE).date()), fetch)
        baseline: dict[str, Counter] = defaultdict(Counter)
        for values in days.values():
            for key, n in values.items():
                provider, bucket = key.split("|", 1)
                baseline[provider][bucket] += n
        n_days = max(1, len(days))
        return {
            "available": True,
            "current": {p: dict(c) for p, c in current.items()},
            "recent": {p: dict(c) for p, c in recent.items()},
            "recent_since": recent_start.strftime("%H:00"),
            "baseline_per_day": {p: {b: round(v / n_days, 1) for b, v in c.items()} for p, c in baseline.items()},
            "baseline_days": len(days),
            "stuck_pending": stuck,
            "hourly": [{"hour": h, **{b: c.get(b, 0) for b in ("delivered", "failed", "pending", "other")}}
                       for h, c in sorted(hourly.items())],
            "errors": errors,
        }

    def gateway_samples(self, now: datetime) -> dict[str, Any]:
        """Recent failed (or stuck) and delivered sends, described as features."""
        if self.socket_db is None:
            return {"available": False, "error": "socket SMS database not available"}
        start = sql.pg_timestamptz(now - timedelta(hours=self.window_hours))
        end = sql.pg_timestamptz(now)
        stuck_end = sql.pg_timestamptz(now - timedelta(hours=self.stuck_hours))
        base = ("SELECT time_stamp, service_provider_name, external_status, LEFT(logged_responses, 3000) AS logged_responses "
                f"FROM public.transaction_activity WHERE {GATEWAY_WHERE} AND time_stamp >= {start} ")
        try:
            failed = self.mb.query(self.socket_db, base + f"AND time_stamp < {end} AND external_status IN "
                                   f"('FAILED_TO_SEND', 'FAILED_TO_DELIVER') ORDER BY time_stamp DESC LIMIT {self.sample_size}")
            stuck = self.mb.query(self.socket_db, base + f"AND time_stamp < {stuck_end} AND external_status = "
                                  f"'SENT_AWAITING_DELIVERY' ORDER BY time_stamp DESC LIMIT {self.sample_size // 2}")
            ok = self.mb.query(self.socket_db, base + f"AND time_stamp < {end} AND external_status = "
                               f"'DELIVERED_SUCCESSFULLY' ORDER BY time_stamp DESC LIMIT {self.sample_size}")
        except MetabaseError as exc:
            return {"available": False, "error": str(exc)[:300]}
        return {"available": True,
                "failed": [gateway_features(r) for r in failed] + [{**gateway_features(r), "stuck": "yes"} for r in stuck],
                "delivered": [gateway_features(r) for r in ok]}

    # ---- dispatch service and CAE (daily shares) ---------------------------------------
    def _daily_status(self, source: str, db: Optional[int], query_for: Callable[[date], str],
                      status_key: str, today: date) -> dict[str, Any]:
        if db is None:
            return {"available": False, "error": f"{source} database not available"}

        def fetch(day: date) -> dict[str, float]:
            values: Counter = Counter()
            for row in self.mb.query(db, query_for(day)):
                lowered = {str(k).lower(): v for k, v in row.items()}
                values[str(lowered.get(status_key) or "unknown").upper()] += int(lowered.get("n") or 0)
            return dict(values)

        try:
            current = fetch(today)
        except (MetabaseError, sql.UnsafeValue) as exc:
            return {"available": False, "error": str(exc)[:300]}
        days, errors = self._cached_days(source, self._baseline_days(today), fetch)
        baseline: Counter = Counter()
        for values in days.values():
            baseline.update(values)
        return {"available": True, "today": current, "baseline_total": dict(baseline), "baseline_days": len(days),
                "errors": errors}

    def dispatch(self, now: datetime) -> dict[str, Any]:
        def q(day: date) -> str:
            return ("SELECT dispatch_status, COUNT(*) AS n FROM sms_message "
                    f"WHERE created_at >= DATE '{day.isoformat()}' AND created_at < DATE '{(day + timedelta(days=1)).isoformat()}' "
                    "GROUP BY dispatch_status")
        return self._daily_status("dispatch", self.dispatch_db, q, "dispatch_status", now.astimezone(LOCAL_ZONE).date())

    def cae(self, now: datetime) -> dict[str, Any]:
        def q(day: date) -> str:
            return ("SELECT REQ_STATUS, COUNT(*) AS N FROM NMBPRODCAE.AUDIT_LETTER_MGMT_REQ "
                    f"WHERE CREATED_ON_DT = DATE '{day.isoformat()}' GROUP BY REQ_STATUS")
        return self._daily_status("cae", self.standby_db, q, "req_status", now.astimezone(LOCAL_ZONE).date())

    # ---- the agent's own ticket checks ------------------------------------------------------
    def ticket_checks(self, now: datetime) -> dict[str, Any]:
        """Accounts the agent checked: delivered vs not delivered, with their facts."""
        window_start = (now - timedelta(hours=self.window_hours)).astimezone(timezone.utc).isoformat(timespec="seconds")
        base_start = (now - timedelta(days=self.baseline_days)).astimezone(timezone.utc).isoformat(timespec="seconds")
        rows = self.db.execute(
            "SELECT created_at, facts FROM decisions WHERE created_at >= ? AND mode != 'audit' AND facts IS NOT NULL",
            (base_start,)).fetchall()
        current = {"delivered": [], "not_delivered": [], "other": 0}
        baseline = {"delivered": 0, "not_delivered": 0}
        for created_at, facts_json in rows:
            try:
                facts = json.loads(facts_json)
            except (TypeError, ValueError):
                continue
            in_window = created_at >= window_start
            for fact in facts.values():
                status = fact.get("status")
                group = ("delivered" if status == "delivered_check_spam" else
                         "not_delivered" if status in NOT_DELIVERED else None)
                if group is None:
                    if in_window:
                        current["other"] += 1
                    continue
                if in_window:
                    current[group].append(fact)
                else:
                    baseline[group] += 1
        return {"available": True, "current": current, "baseline": baseline}

    def collect(self, now: Optional[datetime] = None) -> dict[str, Any]:
        now = now or now_local()
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "window_hours": self.window_hours,
            "baseline_days": self.baseline_days,
            "tickets": self.tickets(now),
            "gateway": self.gateway(now),
            "gateway_samples": self.gateway_samples(now),
            "dispatch": self.dispatch(now),
            "cae": self.cae(now),
            "ticket_checks": self.ticket_checks(now),
        }


__all__ = ["Collector", "account_facts", "gateway_bucket", "NOT_DELIVERED"]
