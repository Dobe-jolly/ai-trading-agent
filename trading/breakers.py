"""Scan-level circuit breakers that run before the model is asked anything.

Each check is deterministic and cheap, so a halted scan costs no API calls:
  - blackout hours: daily rollover, when forex spreads blow out (synthetics trade 24/7)
  - broker latency: orders sent over a slow link fill far from the price the model saw
  - closed markets: forex and metals with no fresh quote (weekends) are skipped, saving model calls
  - drawdown lockout: after the daily loss limit trips, stay out for a fixed time

A halt blocks new opens only. Symbols that already have a position still go through
the model so it can close or tighten them.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

Window = tuple[int, int]  # start, end in minutes since 00:00 UTC; end < start wraps midnight


def is_synthetic(symbol: str) -> bool:
    return "Index" in symbol


def closed_markets(last_tick: dict[str, Optional[int]], stale_min: float) -> dict[str, str]:
    """Forex and metals whose market looks closed, with the reason, from each symbol's last quote time.

    Weekends and holidays stop forex and metals quotes while synthetics tick 24/7, so a quote far
    older than the freshest one on the list means the market is shut. Comparing against that
    reference, not the clock, avoids guessing the broker's time zone. Synthetics are never reported.
    A symbol with no quote at all is reported too: the model could not see a price for it either.
    """
    seen = [t for t in last_tick.values() if t]
    if stale_min <= 0 or not seen:
        return {}
    newest = max(seen)
    closed: dict[str, str] = {}
    for symbol, t in last_tick.items():
        if is_synthetic(symbol):
            continue
        if not t:
            closed[symbol] = "no quote from the broker"
        elif newest - t > stale_min * 60:
            closed[symbol] = f"market closed: last quote {(newest - t) / 60:.0f} min older than the freshest"
    return closed


def parse_blackouts(spec: str) -> list[Window]:
    """Parse "21:00-22:00,23:30-00:15" into minute windows. Raises ValueError on bad input."""
    windows: list[Window] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            start, end = part.split("-")
            windows.append((_minutes(start), _minutes(end)))
        except ValueError as exc:
            raise ValueError(f"bad blackout window {part!r}; expected HH:MM-HH:MM") from exc
    return windows


def _minutes(hhmm: str) -> int:
    hh, mm = hhmm.strip().split(":")
    h, m = int(hh), int(mm)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(hhmm)
    return h * 60 + m


def in_blackout(now: datetime, windows: list[Window]) -> bool:
    """`now` must be UTC."""
    minute = now.hour * 60 + now.minute
    for start, end in windows:
        if start <= end:
            if start <= minute < end:
                return True
        elif minute >= start or minute < end:
            return True
    return False


def halt_reason(
    symbol: str,
    now: datetime,
    windows: list[Window],
    ping_ms: Optional[float],
    max_latency_ms: float,
) -> Optional[str]:
    """Why new opens on `symbol` are halted right now, or None."""
    if windows and not is_synthetic(symbol) and in_blackout(now, windows):
        return f"blackout hours (UTC {now:%H:%M}): spreads are unreliable around the daily rollover"
    if max_latency_ms > 0 and ping_ms is not None and ping_ms > max_latency_ms:
        return f"broker latency {ping_ms:.0f} ms is above the {max_latency_ms:.0f} ms limit"
    return None


class Lockout:
    """A file-backed stop that outlasts the process, so a restart cannot skip it.

    Delete the file to lift it early.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def active(self, now: datetime) -> Optional[tuple[datetime, str]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            until = datetime.fromisoformat(data["until"])
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, OSError) as exc:
            # An unreadable lockout must fail closed, not open.
            log.error("lockout file %s is unreadable (%s); treating as locked", self._path, exc)
            return now + timedelta(hours=1), "lockout file unreadable"
        if until <= now:
            self._path.unlink(missing_ok=True)
            return None
        return until, str(data.get("reason", ""))

    def trip(self, now: datetime, hours: float, reason: str) -> datetime:
        until = now + timedelta(hours=hours)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"until": until.isoformat(), "reason": reason, "tripped_at": now.isoformat()}),
            encoding="utf-8",
        )
        return until
