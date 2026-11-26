"""Fair value gaps that line up across the day, 4 hour, 1 hour and 15 minute charts.

The observation this encodes, written down before any replay (PLAYBOOKS.md, Kappa):

On Volatility 100 (1s), an uptrend retraces to cover bullish fair value gaps, and the
gaps it covers are mostly the ones that overlap on two or more of D1, H4, H1 and M15.
After that cover, it buys again. A downtrend is the mirror image: price retraces up
into bearish gaps that line up, then sells again.

Single-timeframe gaps are marked and then ignored. A gap the newest candle has just
opened is ignored too: price has not come back to it yet. Nothing here is an approved
trade. Kappa has to pass the pre-registered gate before an open is allowed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations, product
from typing import Optional, Sequence

from .geometry import (
    MIN_FVG_ATR,
    TREND_TOL_ATR,
    classify_regime,
    find_gaps,
    find_swings,
)
from .indicators import atr as _atr

TIMEFRAMES: tuple[str, ...] = ("D1", "H4", "H1", "M15")
MIN_ALIGN = 2
# How many bars after the retrace first meets the zone the continuation close may take.
# The cover bar itself counts, so 4 is the cover plus three 15-minute bars.
REACTION_BARS = 4
MAX_GAPS_PER_TF = 8
# Closed candles each timeframe is allowed to contribute. Part of the rule, not a fit.
KEEP = {"D1": 40, "H4": 60, "H1": 80, "M15": 80}
# Fifteen-minute bars inside one closed higher-timeframe candle. These indices trade
# every day of the week, so a day is 96 bars of 15 minutes.
M15_FACTOR = {"H1": 4, "H4": 16, "D1": 96}
# 42 days: a partial day on each end can be dropped and 40 closed days still remain.
LOOKBACK_M15 = 42 * M15_FACTOR["D1"]
SWING_N = 3


@dataclass(frozen=True)
class GapView:
    timeframe: str
    direction: str
    bottom: float
    top: float
    index: int
    fill: float


@dataclass(frozen=True)
class AlignedZone:
    """The price band shared by same-direction gaps on two or more timeframes."""
    direction: str
    bottom: float
    top: float
    timeframes: tuple[str, ...]


@dataclass(frozen=True)
class Read:
    regime: str
    zones: tuple[AlignedZone, ...]
    ignored_single: int
    # (direction, zone) when the latest bar is the continuation after a cover. direction
    # is +1 buy, -1 sell.
    signal: Optional[tuple[int, AlignedZone]]


def unfilled_gaps(timeframe: str, highs: Sequence[float], lows: Sequence[float],
                  closes: Sequence[float]) -> list[GapView]:
    """Unfilled gaps on one timeframe, using that timeframe's own ATR.

    The gap created by the last candle is left out. It is the impulse, not a level
    price has retraced to.
    """
    if len(closes) < 15 or not (len(highs) == len(lows) == len(closes)):
        return []
    atr = _atr(list(highs), list(lows), list(closes))
    if not atr:
        return []
    last = len(closes) - 1
    out: list[GapView] = []
    for gap in find_gaps(list(highs), list(lows), MIN_FVG_ATR * atr):
        if gap.index >= last or gap.fill >= 1.0:
            continue
        out.append(GapView(timeframe, gap.direction, gap.bottom, gap.top, gap.index, gap.fill))
    return out


def aligned_zones(gaps: Sequence[GapView], min_align: int = MIN_ALIGN) -> list[AlignedZone]:
    """Intersections shared by at least `min_align` timeframes, one gap from each.

    A pair that only meets at a single price is not an overlap. When a two-timeframe
    band contains a three-timeframe band of the same direction, only the larger
    confluence is kept: that is the level the retrace is marked against.
    """
    by_tf: dict[str, list[GapView]] = {}
    for gap in gaps:
        by_tf.setdefault(gap.timeframe, []).append(gap)
    tfs = [tf for tf in TIMEFRAMES if by_tf.get(tf)]
    found: list[AlignedZone] = []
    for width in range(min_align, len(tfs) + 1):
        for combo in combinations(tfs, width):
            for picks in product(*(by_tf[tf] for tf in combo)):
                if len({gap.direction for gap in picks}) != 1:
                    continue
                bottom = max(gap.bottom for gap in picks)
                top = min(gap.top for gap in picks)
                if top - bottom <= 1e-9:
                    continue
                found.append(AlignedZone(picks[0].direction, bottom, top, combo))
    return _maximal(_dedupe(found))


def reaction(direction: int, opens: Sequence[float], highs: Sequence[float],
             lows: Sequence[float], closes: Sequence[float], t: int, zone: AlignedZone) -> bool:
    """True when bar t is the first continuation close after a fresh retrace into `zone`.

    Buy (+1): a bar in the last REACTION_BARS came from above the zone and traded into
    it, no close since then has gone through the bottom, and bar t is the first bar in
    that stretch to close up. Sell is the mirror.
    """
    if direction not in (1, -1) or t < 1 or t >= len(closes):
        return False
    if direction * (closes[t] - opens[t]) <= 0:
        return False
    if direction > 0 and closes[t] <= zone.bottom:
        return False
    if direction < 0 and closes[t] >= zone.top:
        return False

    entry_at: Optional[int] = None
    earliest = max(1, t - REACTION_BARS + 1)
    for c in range(t, earliest - 1, -1):
        previous = closes[c - 1]
        came_from_outside = previous > zone.top if direction > 0 else previous < zone.bottom
        if came_from_outside and _overlaps(highs[c], lows[c], zone):
            entry_at = c
            break
    if entry_at is None:
        return False
    for k in range(entry_at, t):
        if direction > 0 and closes[k] <= zone.bottom:
            return False
        if direction < 0 and closes[k] >= zone.top:
            return False
        if direction * (closes[k] - opens[k]) > 0:
            return False
    return True


def regime_of(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> str:
    """Daily trend from the same swing rule the rest of the repo uses."""
    if len(closes) < 15 or not (len(highs) == len(lows) == len(closes)):
        return "UNDEFINED"
    atr = _atr(list(highs), list(lows), list(closes))
    if not atr:
        return "UNDEFINED"
    swings = find_swings(list(highs), list(lows), SWING_N)
    return classify_regime(swings, TREND_TOL_ATR * atr)


def analyse(frames: dict[str, tuple[Sequence[float], Sequence[float], Sequence[float]]],
            opens: Sequence[float], highs: Sequence[float], lows: Sequence[float],
            closes: Sequence[float], t: int) -> Read:
    """Regime from the daily frames, aligned zones in that direction, and any continuation.

    `frames` values are (highs, lows, closes), oldest first, closed candles only.
    The bar arrays are the 15-minute series the continuation is judged on, and `t`
    is the last bar that may be read.
    """
    daily = frames.get("D1")
    regime = regime_of(*daily) if daily else "UNDEFINED"
    if t < 1 or t >= len(closes):
        return Read(regime, (), 0, None)
    want = {"BULLISH_TREND": "bullish", "BEARISH_TREND": "bearish"}.get(regime)
    if want is None:
        return Read(regime, (), 0, None)

    pooled: list[GapView] = []
    for timeframe in TIMEFRAMES:
        series = frames.get(timeframe)
        if not series:
            continue
        gaps = [gap for gap in unfilled_gaps(timeframe, *series) if gap.direction == want]
        pooled.extend(_nearest(gaps, closes[t], MAX_GAPS_PER_TF))
    zones = _sort_zones(aligned_zones(pooled), closes[t])
    ignored = sum(1 for gap in pooled if not _aligned_with_others(gap, pooled))
    signal: Optional[tuple[int, AlignedZone]] = None
    side = 1 if want == "bullish" else -1
    for zone in zones:
        if reaction(side, opens, highs, lows, closes, t, zone):
            signal = (side, zone)
            break
    return Read(regime, tuple(zones), ignored, signal)


def frames_as_of(bars, t: int) -> dict[str, tuple[list[float], list[float], list[float]]]:
    """Closed D1, H4 and H1 candles, plus the recent M15 bars, using only bars 0..t.

    A higher-timeframe candle is closed once the next one has started and every
    15-minute bar that belongs to it is present. The candle still forming is left out,
    so a gap cannot be built from unfinished data. When bar times are missing, candles
    are grouped by index instead of by the clock; the two are not mixed.
    """
    frames: dict[str, tuple[list[float], list[float], list[float]]] = {}
    for timeframe, factor in M15_FACTOR.items():
        frames[timeframe] = _closed_higher(bars, t, timeframe, factor, KEEP[timeframe])
    start = max(0, t - KEEP["M15"] + 1)
    frames["M15"] = (
        list(bars.highs[start:t + 1]),
        list(bars.lows[start:t + 1]),
        list(bars.closes[start:t + 1]),
    )
    return frames


def continuation_at(bars, t: int) -> Optional[tuple[int, AlignedZone]]:
    """Kappa's signal at the close of 15-minute bar t, or None. Reads only bars 0..t."""
    if t < 1 or t >= len(bars) or bars.atr[t] is None:
        return None
    frames = frames_as_of(bars, t)
    # The M15 frame is a tail of the same series. Judge the continuation on the full
    # series so the reaction window still sees the bars just before that tail.
    reading = analyse(frames, bars.opens, bars.highs, bars.lows, bars.closes, t)
    return reading.signal


def read_broker_candles(candles_by_tf: dict[str, list]) -> Read:
    """Same read as `analyse`, from broker candles.

    The newest D1, H4 and H1 bar is dropped. It may still be forming. The newest
    15-minute bar is the bar being read.
    """
    m15 = list(candles_by_tf.get("M15") or [])[-KEEP["M15"]:]
    frames: dict[str, tuple[list[float], list[float], list[float]]] = {}
    for timeframe in TIMEFRAMES:
        candles = list(candles_by_tf.get(timeframe) or [])
        if timeframe != "M15" and candles:
            candles = candles[:-1]
        candles = candles[-KEEP[timeframe]:]
        frames[timeframe] = (
            [c.high for c in candles],
            [c.low for c in candles],
            [c.close for c in candles],
        )
    if len(m15) < 2:
        regime = regime_of(*frames["D1"]) if frames.get("D1") and frames["D1"][2] else "UNDEFINED"
        return Read(regime, (), 0, None)
    return analyse(
        frames,
        [c.open for c in m15],
        [c.high for c in m15],
        [c.low for c in m15],
        [c.close for c in m15],
        len(m15) - 1,
    )


def as_payload(reading: Read) -> dict:
    """JSON-ready fact sheet. The note is a reminder, not a permission to open."""
    signal = None
    if reading.signal is not None:
        signal = "buy" if reading.signal[0] > 0 else "sell"
    zones = [
        {
            "direction": zone.direction,
            "bottom": round(zone.bottom, 6),
            "top": round(zone.top, 6),
            "timeframes": list(zone.timeframes),
        }
        for zone in reading.zones
    ]
    if reading.regime == "BULLISH_TREND":
        expectation = "uptrend: mark the bullish gaps below price; a retrace that covers one, then a buy"
    elif reading.regime == "BEARISH_TREND":
        expectation = "downtrend: mark the bearish gaps above price; a retrace that covers one, then a sell"
    else:
        expectation = "no daily trend, so no retrace targets are marked"
    return {
        "timeframes": list(TIMEFRAMES),
        "daily_regime": reading.regime,
        "aligned_zones": zones,
        "single_timeframe_gaps_ignored": reading.ignored_single,
        "continuation": signal,
        "expectation": expectation,
        "note": (
            "Aligned means the same-direction gap overlaps on 2 or more of D1, H4, H1 and M15. "
            "This is playbook Kappa, a registered hypothesis for Volatility 100 (1s) Index. "
            "Record the zones on the chart read. It is not an approved open."
        ),
    }


# ---- internals -------------------------------------------------------------------


def _overlaps(high: float, low: float, zone: AlignedZone) -> bool:
    return low <= zone.top and high >= zone.bottom


def _dedupe(zones: list[AlignedZone]) -> list[AlignedZone]:
    seen: set[tuple] = set()
    out: list[AlignedZone] = []
    for zone in zones:
        key = (zone.direction, zone.timeframes, round(zone.bottom, 6), round(zone.top, 6))
        if key in seen:
            continue
        seen.add(key)
        out.append(zone)
    return out


def _maximal(zones: list[AlignedZone]) -> list[AlignedZone]:
    kept: list[AlignedZone] = []
    for zone in zones:
        covered = False
        for other in zones:
            if other is zone or other.direction != zone.direction:
                continue
            if set(zone.timeframes) >= set(other.timeframes):
                continue
            inside = other.bottom >= zone.bottom - 1e-9 and other.top <= zone.top + 1e-9
            if inside:
                covered = True
                break
        if not covered:
            kept.append(zone)
    return kept


def _distance(zone: AlignedZone, price: float) -> float:
    if zone.bottom <= price <= zone.top:
        return 0.0
    return min(abs(price - zone.bottom), abs(price - zone.top))


def _nearest(gaps: list[GapView], price: float, limit: int) -> list[GapView]:
    def dist(gap: GapView) -> float:
        if gap.bottom <= price <= gap.top:
            return 0.0
        return min(abs(price - gap.bottom), abs(price - gap.top))

    return sorted(gaps, key=dist)[:limit]


def _sort_zones(zones: list[AlignedZone], price: float) -> list[AlignedZone]:
    return sorted(zones, key=lambda zone: (-len(zone.timeframes), _distance(zone, price), zone.bottom))


def _aligned_with_others(gap: GapView, gaps: Sequence[GapView]) -> bool:
    frames = {gap.timeframe}
    for other in gaps:
        if other is gap or other.timeframe == gap.timeframe or other.direction != gap.direction:
            continue
        if min(gap.top, other.top) - max(gap.bottom, other.bottom) > 1e-9:
            frames.add(other.timeframe)
    return len(frames) >= MIN_ALIGN


def _as_utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        return when
    return when.astimezone(timezone.utc).replace(tzinfo=None)


def _closed_higher(bars, t: int, timeframe: str, factor: int, keep: int
                   ) -> tuple[list[float], list[float], list[float]]:
    start = max(0, t + 1 - LOOKBACK_M15)
    times = list(bars.times[start:t + 1]) if bars.times else []
    use_clock = len(times) == t + 1 - start and all(isinstance(item, datetime) for item in times)

    buckets: dict[tuple, list[int]] = {}
    order: list[tuple] = []
    for i in range(start, t + 1):
        if use_clock:
            when = _as_utc(bars.times[i])
            if timeframe == "H1":
                key = ("c", when.year, when.month, when.day, when.hour)
            elif timeframe == "H4":
                key = ("c", when.year, when.month, when.day, when.hour // 4)
            else:
                key = ("c", when.year, when.month, when.day)
        else:
            key = ("i", i // factor)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(i)

    closed = [
        key for n, key in enumerate(order)
        if n < len(order) - 1 and len(buckets[key]) >= factor
    ][-keep:]
    highs, lows, closes = [], [], []
    for key in closed:
        idxs = buckets[key]
        highs.append(max(bars.highs[i] for i in idxs))
        lows.append(min(bars.lows[i] for i in idxs))
        closes.append(bars.closes[idxs[-1]])
    return highs, lows, closes
