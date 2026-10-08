"""Pieces of the anomaly watch both agents use: signals and their severity, comparing failures with
successes, and the local cache of per-day metrics."""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date
from typing import Any, Callable, Iterable, Optional

from .. import sql
from ..metabase import MetabaseError
from ..normalize import LOCAL_ZONE, parse_timestamp

SEVERITY_RANK = {"ok": 0, "watch": 1, "incident": 2}
RECENT_HOURS = 2
METRIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_daily (
    source TEXT NOT NULL, day TEXT NOT NULL, key TEXT NOT NULL, value REAL NOT NULL,
    PRIMARY KEY (source, day, key)
);
"""


def signal(key: str, label: str, severity: str, value: Any, usual: Any, detail: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "severity": severity, "value": value, "usual": usual, "detail": detail, **extra}


_signal = signal


def incident_key(analysis: dict[str, Any]) -> str:
    """Identity of what is wrong, so a continuing incident is not re-announced every hour."""
    keys = sorted(s["key"] for s in analysis["signals"] if s["severity"] != "ok" and s["value"] is not None)
    return "|".join(keys)


def hour_band(ts: Any) -> str:
    parsed = parse_timestamp(ts)
    if parsed is None:
        return "unknown"
    hour = parsed.astimezone(LOCAL_ZONE).hour
    return ("00–05", "06–11", "12–17", "18–23")[hour // 6]


def values_of(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value] or ["none"]
    return [str(value if value is not None else "unknown")]


def compare(failed: Iterable[dict[str, Any]], ok: Iterable[dict[str, Any]], features: Iterable[str],
            min_failed: int = 5, min_ratio: float = 1.8,
            labels: Optional[dict[str, str]] = None) -> list[dict[str, Any]]:
    """For each feature value: its share among failures vs among successes. Values much more
    common among failures (ratio >= min_ratio, with enough failures behind them) are findings.
    Samples may be drawn separately; shares within each sample are still comparable."""
    failed, ok = list(failed), list(ok)
    if not failed or not ok:
        return []
    findings = []
    for feature in features:
        f_counts: Counter = Counter()
        o_counts: Counter = Counter()
        for row in failed:
            for value in values_of(row.get(feature)):
                f_counts[value] += 1
        for row in ok:
            for value in values_of(row.get(feature)):
                o_counts[value] += 1
        for value, n_failed in f_counts.items():
            failed_share = n_failed / len(failed)
            ok_share = o_counts.get(value, 0) / len(ok)
            # Smoothed (add-one) shares for the ratio, so a value that happens to be absent from a
            # small set of deliveries does not get an infinite ratio and outrank real patterns.
            ratio = ((n_failed + 1) / (len(failed) + 2)) / ((o_counts.get(value, 0) + 1) / (len(ok) + 2))
            if n_failed >= max(min_failed, 0.05 * len(failed)) and ratio >= min_ratio:
                findings.append({
                    "feature": feature, "label": (labels or {}).get(feature, feature), "value": value,
                    "failed_share": round(failed_share, 3), "ok_share": round(ok_share, 3),
                    "ratio": round(ratio, 1), "failed_n": n_failed, "ok_n": o_counts.get(value, 0),
                })
    # Strongest first: share of failures it explains, weighted by how distinctive it is.
    findings.sort(key=lambda f: f["failed_share"] * min(f["ratio"], 50.0), reverse=True)
    return findings[:12]


class MetricCache:
    """Per-day metrics for complete days, kept in metric_daily so each run only queries the current window."""

    db: sqlite3.Connection

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
