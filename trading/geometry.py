"""Market structure and liquidity from raw candles. Pure Python, no indicators.

Everything here is a deterministic function of the candle arrays (oldest first), so the
same chart always gives the same answer. That is the point: the model gets facts
(swings, regime, breaks, liquidity, gaps) and spends its effort judging them.

No lookahead. A swing at bar i needs `n` bars on each side, so it only becomes known at
bar i+n and the last `n` bars never contain one. Breaks and sweeps are only judged against
swings that were already confirmed at that bar.

Tolerances are fractions of ATR, not pips, so the same settings work on forex and on
synthetic indices whose prices differ by orders of magnitude.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .indicators import atr as _atr

MIN_SWING_N = 1
ATR_PERIOD = 14

# Defaults, all in ATR units unless noted.
EQUAL_TOL_ATR = 0.15      # swings this close count as the same level
TREND_TOL_ATR = 0.10      # a higher high must beat the previous by at least this
MIN_FVG_ATR = 0.20        # smaller gaps are noise
EQUAL_MIN_GAP_BARS = 3    # touches closer than this are one touch, not two
RECENT_SWEEP_BARS = 5
SINGLE_SWING_LEVELS = 5   # how many recent lone swings per side to watch for sweeps


@dataclass(frozen=True)
class Swing:
    index: int
    price: float
    kind: str                 # "high" | "low"


@dataclass(frozen=True)
class Break:
    kind: str                 # "BOS" continues the prior break direction; "CHOCH" reverses it
    direction: str            # "up" | "down"
    level: float
    index: int                # bar whose close broke the level
    swing_index: int


@dataclass(frozen=True)
class Level:
    """A pool of resting stops: two or more equal swings, or one recent swing."""
    kind: str                 # "high" | "low"
    price: float
    touches: int
    last_index: int
    source: str               # "equal" | "swing"
    status: str = "intact"    # intact | swept | broken
    event_index: Optional[int] = None
    extreme: Optional[float] = None   # wick extreme of the sweep candle (the stop-loss anchor)


@dataclass(frozen=True)
class Gap:
    direction: str            # "bullish" | "bearish"
    bottom: float
    top: float
    index: int                # the third candle of the pattern
    fill: float               # 0.0 untouched .. 1.0 fully filled


# ---- swings ---------------------------------------------------------------------


def find_swings(highs: list[float], lows: list[float], n: int = 3) -> list[Swing]:
    """Confirmed swing highs and lows, oldest first.

    A high must exceed the `n` bars before it and match or exceed the `n` after, so a flat
    top yields one swing, not several. Lows mirror that.
    """
    if n < MIN_SWING_N:
        raise ValueError(f"n must be at least {MIN_SWING_N}")
    swings: list[Swing] = []
    for i in range(n, len(highs) - n):
        if all(highs[i] > highs[i - j] for j in range(1, n + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, n + 1)):
            swings.append(Swing(i, highs[i], "high"))
        if all(lows[i] < lows[i - j] for j in range(1, n + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, n + 1)):
            swings.append(Swing(i, lows[i], "low"))
    return swings


# ---- regime ---------------------------------------------------------------------


def classify_regime(swings: list[Swing], tol: float = 0.0) -> str:
    """BULLISH_TREND (higher highs and higher lows), BEARISH_TREND, RANGE or UNDEFINED.

    Compares the last two swing highs and the last two swing lows. A move smaller than
    `tol` counts as equal, which keeps overlapping, choppy swings out of the trend buckets.
    """
    highs = [s.price for s in swings if s.kind == "high"][-2:]
    lows = [s.price for s in swings if s.kind == "low"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return "UNDEFINED"
    higher_high, lower_high = highs[1] > highs[0] + tol, highs[1] < highs[0] - tol
    higher_low, lower_low = lows[1] > lows[0] + tol, lows[1] < lows[0] - tol
    if higher_high and higher_low:
        return "BULLISH_TREND"
    if lower_high and lower_low:
        return "BEARISH_TREND"
    return "RANGE"


# ---- breaks of structure --------------------------------------------------------


def find_breaks(closes: list[float], swings: list[Swing], n: int = 3) -> list[Break]:
    """Closes beyond the latest confirmed swing. Each swing can be broken once.

    The first break is a BOS. After that, a break in the same direction as the last one is a
    BOS (trend continues) and a break the other way is a CHOCH (character changes).
    """
    ordered = sorted(swings, key=lambda s: (s.index, s.kind))
    last: dict[str, Optional[Swing]] = {"high": None, "low": None}
    broken: set[tuple[str, int]] = set()
    trend = 0
    out: list[Break] = []
    k = 0
    for j, close in enumerate(closes):
        while k < len(ordered) and ordered[k].index + n <= j:
            last[ordered[k].kind] = ordered[k]
            k += 1
        high, low = last["high"], last["low"]
        if high and ("high", high.index) not in broken and close > high.price:
            out.append(Break("CHOCH" if trend == -1 else "BOS", "up", high.price, j, high.index))
            broken.add(("high", high.index))
            trend = 1
        elif low and ("low", low.index) not in broken and close < low.price:
            out.append(Break("CHOCH" if trend == 1 else "BOS", "down", low.price, j, low.index))
            broken.add(("low", low.index))
            trend = -1
    return out


# ---- liquidity: equal highs/lows and sweeps -------------------------------------


def find_levels(
    highs: list[float], lows: list[float], closes: list[float],
    swings: list[Swing], tol: float,
) -> list[Level]:
    """Liquidity pools and what has happened to each since it formed.

    A level is judged by the first bar that trades beyond it:
      - wick through, close back inside  -> "swept" (the stops were taken and price rejected)
      - close beyond it                   -> "broken" (a genuine breakout, not a sweep)
      - never traded beyond               -> "intact"
    """
    levels: list[Level] = []
    for kind in ("high", "low"):
        pts = [s for s in swings if s.kind == kind]
        in_cluster: set[int] = set()
        for a, first in enumerate(pts):
            if a in in_cluster:
                continue
            cluster = [first]
            for b in range(a + 1, len(pts)):
                if b in in_cluster:
                    continue
                mean = sum(p.price for p in cluster) / len(cluster)
                if abs(pts[b].price - mean) <= tol and pts[b].index - cluster[-1].index >= EQUAL_MIN_GAP_BARS:
                    cluster.append(pts[b])
                    in_cluster.add(b)
            if len(cluster) >= 2:
                in_cluster.add(a)
                prices = [p.price for p in cluster]
                levels.append(Level(kind, max(prices) if kind == "high" else min(prices),
                                    len(cluster), cluster[-1].index, "equal"))
        lone = [p for i, p in enumerate(pts) if i not in in_cluster][-SINGLE_SWING_LEVELS:]
        levels += [Level(kind, p.price, 1, p.index, "swing") for p in lone]
    return [_judge(level, highs, lows, closes) for level in levels]


def _judge(level: Level, highs: list[float], lows: list[float], closes: list[float]) -> Level:
    for j in range(level.last_index + 1, len(closes)):
        if level.kind == "high" and highs[j] > level.price:
            swept = closes[j] < level.price
            return Level(level.kind, level.price, level.touches, level.last_index, level.source,
                         "swept" if swept else "broken", j, highs[j] if swept else None)
        if level.kind == "low" and lows[j] < level.price:
            swept = closes[j] > level.price
            return Level(level.kind, level.price, level.touches, level.last_index, level.source,
                         "swept" if swept else "broken", j, lows[j] if swept else None)
    return level


# ---- fair value gaps ------------------------------------------------------------


def find_gaps(highs: list[float], lows: list[float], min_size: float = 0.0) -> list[Gap]:
    """Three-candle imbalances that are not fully filled yet, oldest first.

    Bullish: the third candle's low is above the first candle's high. Bearish: the mirror.
    A gap is filled once price trades back through its far edge.
    """
    gaps: list[Gap] = []
    for i in range(2, len(highs)):
        if lows[i] > highs[i - 2] and lows[i] - highs[i - 2] >= min_size:
            bottom, top, direction = highs[i - 2], lows[i], "bullish"
        elif highs[i] < lows[i - 2] and lows[i - 2] - highs[i] >= min_size:
            bottom, top, direction = highs[i], lows[i - 2], "bearish"
        else:
            continue
        size = top - bottom
        if direction == "bullish":
            deepest = min((lows[k] for k in range(i + 1, len(lows))), default=top)
            fill = (top - deepest) / size
        else:
            deepest = max((highs[k] for k in range(i + 1, len(highs))), default=bottom)
            fill = (deepest - bottom) / size
        fill = min(max(fill, 0.0), 1.0)
        if fill < 1.0:
            gaps.append(Gap(direction, bottom, top, i, fill))
    return gaps


# ---- everything at once ---------------------------------------------------------


def analyze(
    highs: list[float], lows: list[float], closes: list[float],
    n: int = 3, recent_sweep_bars: int = RECENT_SWEEP_BARS,
) -> dict:
    """Structure summary for one symbol and timeframe, safe to serialise as JSON."""
    need = 2 * n + ATR_PERIOD + 2
    if not (len(highs) == len(lows) == len(closes)) or len(closes) < need:
        return {"error": f"need at least {need} candles, got {len(closes)}"}

    last = len(closes) - 1
    price = closes[-1]
    atr = _atr(highs, lows, closes, ATR_PERIOD) or 0.0
    swings = find_swings(highs, lows, n)
    breaks = find_breaks(closes, swings, n)
    levels = find_levels(highs, lows, closes, swings, EQUAL_TOL_ATR * atr)
    gaps = find_gaps(highs, lows, MIN_FVG_ATR * atr)

    sweeps = [lv for lv in levels if lv.status == "swept" and last - lv.event_index <= recent_sweep_bars]
    sweeps.sort(key=lambda lv: lv.event_index, reverse=True)

    def nearest(kind: str, above: bool) -> Optional[Level]:
        pool = [lv for lv in levels if lv.kind == kind and lv.status == "intact" and lv.source == "equal"
                and (lv.price > price if above else lv.price < price)]
        return min(pool, key=lambda lv: abs(lv.price - price), default=None)

    eq_high, eq_low = nearest("high", True), nearest("low", False)

    # Price is "in" a gap when the latest candle overlaps it. A gap the latest candle just
    # created doesn't count: price hasn't come back to it.
    in_gap = [g for g in gaps if g.index < last and lows[-1] <= g.top and highs[-1] >= g.bottom]
    by_distance = sorted(gaps, key=lambda g: min(abs(price - g.bottom), abs(price - g.top)))

    return {
        "bars": len(closes),
        "price": _r(price),
        "atr": _r(atr),
        "regime": classify_regime(swings, TREND_TOL_ATR * atr),
        "swing_highs": [_r(s.price) for s in swings if s.kind == "high"][-3:],
        "swing_lows": [_r(s.price) for s in swings if s.kind == "low"][-3:],
        "last_break": _break(breaks[-1], last) if breaks else None,
        "nearest_equal_high": _r(eq_high.price) if eq_high else None,
        "nearest_equal_high_atr": _atr_away(eq_high, price, atr),
        "nearest_equal_low": _r(eq_low.price) if eq_low else None,
        "nearest_equal_low_atr": _atr_away(eq_low, price, atr),
        "recent_sweep": _sweep(sweeps[0], last) if sweeps else None,
        "fvg_zone_active": bool(in_gap),
        "fvg_direction": in_gap[-1].direction if in_gap else None,
        "fvg_boundaries": [_r(in_gap[-1].bottom), _r(in_gap[-1].top)] if in_gap else None,
        "active_fvgs": [_gap(g, last) for g in by_distance[:3]],
        "notes": (
            "Swings need n bars after them, so the last bars never hold one. "
            "A sweep is a wick through a level with a close back inside; a close beyond it is a "
            "breakout, not a sweep. Equal levels are swings within a fraction of ATR."
        ),
    }


def analyze_candles(candles: list, n: int = 3) -> dict:
    return analyze([c.high for c in candles], [c.low for c in candles], [c.close for c in candles], n)


def _r(x: float) -> float:
    return round(float(x), 6)


def _atr_away(level: Optional[Level], price: float, atr: float) -> Optional[float]:
    """How far a level is from price in ATRs, so a level 15 ATRs away is not mistaken for a nearby one."""
    return round(abs(level.price - price) / atr, 1) if level and atr else None


def _break(b: Break, last: int) -> dict:
    return {"type": b.kind, "direction": b.direction, "level": _r(b.level), "bars_ago": last - b.index}


def _sweep(lv: Level, last: int) -> dict:
    # Highs swept and rejected is bearish; lows swept and reclaimed is bullish.
    return {
        "signal": "HIGH_SWEPT_AND_REJECTED" if lv.kind == "high" else "LOW_SWEPT_AND_RECLAIMED",
        "direction": "bearish" if lv.kind == "high" else "bullish",
        "level": _r(lv.price),
        "wick_extreme": _r(lv.extreme),
        "source": lv.source,
        "touches": lv.touches,
        "bars_ago": last - lv.event_index,
    }


def _gap(g: Gap, last: int) -> dict:
    return {"direction": g.direction, "bottom": _r(g.bottom), "top": _r(g.top),
            "filled": round(g.fill, 2), "bars_ago": last - g.index}
