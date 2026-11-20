"""Study E: planted cross-sectional momentum PASSes; anti-aligned FAILs; constants match registration."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from trading.xsmom import (
    CURRENCY_PAIR, HOLD, LOOKBACK, OVERNIGHT_ATR_PER_DAY, TOP_K, pair_direction_for_currency, run,
)


def candle(d: date, o, h, l, c):
    return SimpleNamespace(
        time=datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c, volume=0,
    )


def test_registered_constants():
    assert LOOKBACK == 21 and HOLD == 21 and TOP_K == 2
    assert OVERNIGHT_ATR_PER_DAY == 0.015
    assert set(CURRENCY_PAIR) == {"EUR", "GBP", "AUD", "NZD", "JPY", "CHF", "CAD"}


def test_pair_direction_mapping():
    assert pair_direction_for_currency("EUR", +1) == ("EURUSD", 1)
    assert pair_direction_for_currency("JPY", +1) == ("USDJPY", -1)
    assert pair_direction_for_currency("JPY", -1) == ("USDJPY", 1)


def _aligned_books(n=900, winner_drift=True):
    """Shared calendar; give AUD/NZD persistent relative strength (or weakness)."""
    start = date(2008, 1, 1)
    candles = {}
    # Bases
    bases = {
        "EURUSD": 1.20, "GBPUSD": 1.50, "AUDUSD": 0.75, "NZDUSD": 0.65,
        "USDJPY": 110.0, "USDCHF": 0.95, "USDCAD": 1.25,
    }
    # Daily drifts: if winner_drift, AUD/NZD rise (ccyusd) and JPY/CHF strengthen less / weaken
    # so AUD/NZD rank top and JPY/CHF bottom → long AUD/NZD short JPY/CHF wins if those pairs continue.
    drifts = {
        "EURUSD": 0.00005, "GBPUSD": 0.00005,
        "AUDUSD": 0.0008 if winner_drift else -0.0008,
        "NZDUSD": 0.0007 if winner_drift else -0.0007,
        "USDJPY": 0.04 if winner_drift else -0.04,   # JPY weak when winner_drift (USDJPY up)
        "USDCHF": 0.0004 if winner_drift else -0.0004,
        "USDCAD": 0.0001,
    }
    for pair, px0 in bases.items():
        out, px, day = [], px0, start
        dpx = drifts[pair]
        for _ in range(n):
            new = px + dpx
            hi, lo = max(px, new) + abs(dpx) * 2, min(px, new) - abs(dpx) * 2
            out.append(candle(day, px, hi, lo, new))
            px, day = new, day + timedelta(days=1)
        candles[pair] = out
    return candles


def test_planted_cross_section_passes():
    candles = _aligned_books(winner_drift=True)
    report = run(candles, {s: 0.0 for s in candles}, seed="0", data_source="test")
    assert report.cell.verdict == "PASS", report.cell.failed
    assert report.cell.real.avg_r > 0


def test_mean_reverting_cross_section_fails_after_costs():
    """Alternating relative strength each lookback → ranks reverse into the hold → costs dominate."""
    start = date(2008, 1, 1)
    n = 900
    bases = {
        "EURUSD": 1.20, "GBPUSD": 1.50, "AUDUSD": 0.75, "NZDUSD": 0.65,
        "USDJPY": 110.0, "USDCHF": 0.95, "USDCAD": 1.25,
    }
    candles = {p: [] for p in bases}
    px = dict(bases)
    day = start
    for i in range(n):
        # Flip which pair is "hot" every LOOKBACK days so winners mean-revert in the hold
        block = (i // LOOKBACK) % 2
        for pair, px0 in bases.items():
            if pair in ("AUDUSD", "NZDUSD"):
                dpx = 0.0008 if block == 0 else -0.0008
            elif pair in ("USDJPY", "USDCHF"):
                dpx = 0.04 if block == 1 else -0.04
            else:
                dpx = 0.00005
            new = px[pair] + dpx
            hi, lo = max(px[pair], new) + abs(dpx) * 2, min(px[pair], new) - abs(dpx) * 2
            candles[pair].append(candle(day, px[pair], hi, lo, new))
            px[pair] = new
        day += timedelta(days=1)
    report = run(
        candles,
        {s: 0.0002 if "JPY" not in s else 0.02 for s in candles},
        seed="0", data_source="test",
    )
    assert report.cell.verdict == "FAIL"
    assert report.cell.real.n >= 100
