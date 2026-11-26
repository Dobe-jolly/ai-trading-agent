"""Kappa: a retrace covers fair value gaps that line up on two or more timeframes, then the trend resumes.

The cases below are the rule in PLAYBOOKS.md, one condition at a time. They do not replay history.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from trading.broker.metatrader import Candle
from trading.fvg_align import (
    LOOKBACK_M15,
    AlignedZone,
    GapView,
    aligned_zones,
    analyse,
    frames_as_of,
    reaction,
)
from trading.playbooks import REGISTRY
from trading.playbooks.base import Bars
from trading.playbooks.evaluate import run_playbook
from trading.playbooks.kappa import SYMBOL, Kappa
from trading.survivors import open_allowed
from trading.tools import TradeEvidence, TradingToolbox


def _path(points: list[float], leg: int = 5) -> list[float]:
    closes = [points[0]]
    for left, right in zip(points, points[1:]):
        closes += [left + (right - left) * k / leg for k in range(1, leg + 1)]
    return closes


def _ohlc(closes: list[float], pad: float = 0.4):
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + pad for o, c in zip(opens, closes)]
    lows = [min(o, c) - pad for o, c in zip(opens, closes)]
    return opens, highs, lows, closes


def _grind(turns: list[tuple[float, int]], pad: float = 0.5):
    """A trend whose candles overlap the bar two back, so the series itself has no gap.

    `turns` is (price, bars to get there) after the first price, which has a bar count of 0.
    """
    closes = [turns[0][0]]
    for end, bars in turns[1:]:
        start = closes[-1]
        closes += [start + (end - start) * k / bars for k in range(1, bars + 1)]
    return _ohlc(closes, pad)


def _daily_uptrend():
    # Short enough that the tool's 40-bar daily window still sees the whole trend
    # after it drops the newest, still-forming day.
    return _grind([(10, 0), (16, 8), (13, 6), (19, 8), (16, 6), (22, 8), (19, 6)])


def _daily_downtrend():
    return _grind([(22, 0), (16, 8), (19, 6), (13, 8), (16, 6), (10, 8), (13, 6)])


def _quiet(n: int, price: float):
    opens, highs, lows, closes = [], [], [], []
    for _ in range(n):
        opens.append(price)
        closes.append(price + 0.1)
        highs.append(price + 0.5)
        lows.append(price - 0.5)
        price += 0.1
    return opens, highs, lows, closes


def _bullish_gap():
    """Quiet bars, then one bullish gap from 100 to 110, then a bar that leaves it open.

    The middle candle of the pattern overlaps both neighbours, so it does not open a
    second gap on the way up.
    """
    opens, highs, lows, closes = _quiet(20, 90.0)
    opens += [closes[-1], 100, 115, 118]
    highs += [100, 120, 121, 122]
    lows += [closes[-1] - 1, 90, 110, 115]
    closes += [99, 118, 119, 121]
    return opens, highs, lows, closes


def _with_retrace(opens, highs, lows, closes, bars):
    opens, highs, lows, closes = list(opens), list(highs), list(lows), list(closes)
    for o, h, l, c in bars:
        opens.append(o)
        highs.append(h)
        lows.append(l)
        closes.append(c)
    return opens, highs, lows, closes


def _frames(daily, h1, m15, h4=None):
    if h4 is None:
        h4 = _quiet(20, 50.0)
    return {
        "D1": daily[1:],
        "H4": h4[1:],
        "H1": h1[1:],
        "M15": m15[1:],
    }


def _read(daily, h1, m15):
    m_opens, m_highs, m_lows, m_closes = m15
    return analyse(_frames(daily, h1, m15), m_opens, m_highs, m_lows, m_closes, len(m_closes) - 1)


# ---- alignment --------------------------------------------------------------------


def test_two_timeframes_that_share_a_band_are_one_zone():
    gaps = [
        GapView("D1", "bullish", 100, 120, 3, 0.0),
        GapView("H1", "bullish", 108, 130, 3, 0.0),
    ]
    zones = aligned_zones(gaps)
    assert len(zones) == 1
    assert zones[0].timeframes == ("D1", "H1")
    assert (zones[0].bottom, zones[0].top) == (108, 120)


def test_a_single_timeframe_is_not_a_zone():
    gaps = [GapView("M15", "bullish", 100, 110, 3, 0.0)]
    assert aligned_zones(gaps) == []


def test_opposite_directions_do_not_align():
    gaps = [
        GapView("D1", "bullish", 100, 120, 3, 0.0),
        GapView("H1", "bearish", 100, 120, 3, 0.0),
    ]
    assert aligned_zones(gaps) == []


def test_meeting_at_one_price_is_not_an_overlap():
    gaps = [
        GapView("D1", "bullish", 100, 110, 3, 0.0),
        GapView("H1", "bullish", 110, 120, 3, 0.0),
    ]
    assert aligned_zones(gaps) == []


def test_a_wider_pair_is_dropped_when_a_third_timeframe_sits_inside_it():
    gaps = [
        GapView("D1", "bullish", 100, 130, 3, 0.0),
        GapView("H1", "bullish", 100, 130, 3, 0.0),
        GapView("M15", "bullish", 120, 125, 3, 0.0),
    ]
    zones = aligned_zones(gaps)
    assert len(zones) == 1
    assert zones[0].timeframes == ("D1", "H1", "M15")
    assert (round(zones[0].bottom, 6), round(zones[0].top, 6)) == (120, 125)


def test_two_separate_pairs_are_both_kept():
    gaps = [
        GapView("D1", "bullish", 100, 110, 3, 0.0),
        GapView("H1", "bullish", 100, 110, 4, 0.0),
        GapView("H4", "bullish", 80, 90, 3, 0.0),
        GapView("M15", "bullish", 80, 90, 4, 0.0),
    ]
    bands = {(zone.bottom, zone.top) for zone in aligned_zones(gaps)}
    assert bands == {(100, 110), (80, 90)}


# ---- the retrace ------------------------------------------------------------------


def _zone():
    return AlignedZone("bullish", 100, 110, ("D1", "H1"))


def test_the_cover_bar_can_be_the_buy_when_it_closes_up():
    # Previous close above the zone, this bar trades into it and closes up.
    opens, highs, lows, closes = [112, 112], [113, 114], [111, 105], [112, 113]
    assert reaction(1, opens, highs, lows, closes, 1, _zone())


def test_a_down_close_into_the_zone_then_an_up_close_is_the_buy():
    opens = [121, 121, 108]
    highs = [122, 122, 113]
    lows = [120, 105, 106]
    closes = [121, 108, 112]
    assert reaction(1, opens, highs, lows, closes, 1, _zone()) is False
    assert reaction(1, opens, highs, lows, closes, 2, _zone()) is True


def test_a_second_up_close_does_not_signal_again():
    opens = [120, 121, 108, 112]
    highs = [122, 122, 113, 116]
    lows = [119, 105, 106, 111]
    closes = [121, 108, 112, 115]
    # Bar 1 covers the zone. Bar 2 is the first buy. Bar 3 closes up again and does not re-enter.
    assert reaction(1, opens, highs, lows, closes, 2, _zone()) is True
    assert reaction(1, opens, highs, lows, closes, 3, _zone()) is False


def test_the_buy_must_arrive_within_four_bars_of_the_cover():
    # Cover at bar 1. Bars 2, 3, 4 close down inside the zone. Bar 5 closes up: five bars
    # after the cover, so the window (cover plus three) has expired.
    opens = [121, 121, 108, 107, 106, 105]
    highs = [122, 122, 109, 108, 107, 112]
    lows = [120, 105, 106, 105, 104, 104]
    closes = [121, 108, 107, 106, 105, 111]
    assert reaction(1, opens, highs, lows, closes, 5, _zone()) is False
    assert reaction(1, opens, highs, lows, closes, 4, _zone()) is False  # still a down close
    # Bar 4 is index 4, which is cover (1) plus three bars: the last slot. Make it the up close.
    opens[4], closes[4] = 106, 109
    assert reaction(1, opens, highs, lows, closes, 4, _zone()) is True


def test_a_close_through_the_far_side_cancels_the_buy():
    opens = [121, 121, 99]
    highs = [122, 122, 104]
    lows = [120, 98, 97]
    closes = [121, 99, 103]   # bar 1 closed through 100; bar 2 closes up
    assert reaction(1, opens, highs, lows, closes, 2, _zone()) is False


def test_price_that_was_already_inside_the_zone_is_not_a_fresh_retrace():
    opens, highs, lows, closes = [105, 105], [108, 109], [104, 104], [106, 108]
    assert reaction(1, opens, highs, lows, closes, 1, _zone()) is False


def test_tagging_the_wide_band_but_not_the_shared_band_is_not_a_cover():
    shared = AlignedZone("bullish", 120, 125, ("D1", "H1", "M15"))
    opens, highs, lows, closes = [130, 130], [131, 131], [129, 110], [130, 112]
    assert reaction(1, opens, highs, lows, closes, 1, shared) is False


def test_a_downtrend_sells_the_first_down_close_after_the_rally_into_the_zone():
    zone = AlignedZone("bearish", 100, 110, ("D1", "H1"))
    opens = [90, 90, 104]
    highs = [91, 105, 106]
    lows = [88, 89, 100]
    closes = [90, 104, 101]
    assert reaction(-1, opens, highs, lows, closes, 1, zone) is False
    assert reaction(-1, opens, highs, lows, closes, 2, zone) is True


def test_a_close_through_the_top_cancels_the_sell():
    zone = AlignedZone("bearish", 100, 110, ("D1", "H1"))
    opens = [90, 90, 112]
    highs = [91, 112, 113]
    lows = [88, 89, 108]
    closes = [90, 112, 109]
    assert reaction(-1, opens, highs, lows, closes, 2, zone) is False


# ---- daily trend plus the gaps ----------------------------------------------------


def test_an_uptrend_buys_when_the_retrace_covers_an_aligned_bullish_gap():
    h1 = _bullish_gap()
    m15 = _with_retrace(*h1, [(121, 122, 105, 108), (108, 113, 106, 112)])
    reading = _read(_daily_uptrend(), h1, m15)
    assert reading.regime == "BULLISH_TREND"
    assert reading.signal is not None and reading.signal[0] == 1
    zone = reading.signal[1]
    assert zone.timeframes == ("H1", "M15")
    assert (round(zone.bottom, 6), round(zone.top, 6)) == (100, 110)
    assert reading.ignored_single == 0


def test_the_same_retrace_is_ignored_when_only_one_timeframe_has_the_gap():
    h1 = _quiet(30, 90.0)
    m15 = _with_retrace(*_bullish_gap(), [(121, 122, 105, 108), (108, 113, 106, 112)])
    reading = _read(_daily_uptrend(), h1, m15)
    assert reading.regime == "BULLISH_TREND"
    assert reading.zones == ()
    assert reading.signal is None
    assert reading.ignored_single == 1


def test_a_range_marks_nothing_even_if_gaps_overlap():
    flat = _ohlc(_path([10, 20, 12, 20, 12, 20, 12]))
    h1 = _bullish_gap()
    m15 = _with_retrace(*h1, [(121, 122, 105, 108), (108, 113, 106, 112)])
    reading = _read(flat, h1, m15)
    assert reading.regime == "RANGE"
    assert reading.zones == ()
    assert reading.signal is None


def test_a_downtrend_sells_when_the_rally_covers_an_aligned_bearish_gap():
    def bearish_gap():
        opens, highs, lows, closes = _quiet(20, 140.0)
        # Gap top is bar A's low (110); gap bottom is bar C's high (100).
        opens += [closes[-1], 112, 100, 96]
        highs += [closes[-1] + 0.5, 112, 100, 98]
        lows += [110, 100, 94, 94]
        closes += [112, 102, 96, 95]
        return opens, highs, lows, closes

    h1 = bearish_gap()
    # Rally from below 100 into the zone, then a down close.
    m15 = _with_retrace(*h1, [(95, 105, 94, 104), (104, 106, 100, 101)])
    reading = _read(_daily_downtrend(), h1, m15)
    assert reading.regime == "BEARISH_TREND"
    assert reading.signal is not None and reading.signal[0] == -1
    assert reading.signal[1].direction == "bearish"
    assert reading.signal[1].timeframes == ("H1", "M15")


# ---- building higher timeframes from 15-minute bars -------------------------------


def _timed(n: int, start: datetime, price: float = 100.0) -> Bars:
    times = [start + timedelta(minutes=15 * i) for i in range(n)]
    opens = [price] * n
    highs = [price + 1] * n
    lows = [price - 1] * n
    closes = [price] * n
    return Bars(opens, highs, lows, closes, times)


def test_a_higher_timeframe_candle_is_closed_only_after_the_next_one_starts():
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    bars = _timed(9, start)  # 00:00 through 02:00
    # At 01:45 the 00:00 hour has closed and the 01:00 hour is still open.
    assert len(frames_as_of(bars, 7)["H1"][2]) == 1
    closed = frames_as_of(bars, 8)["H1"]                 # 02:00 starts, so 00:00 and 01:00 are both closed
    assert len(closed[2]) == 2
    assert closed[0] == [101, 101]


def test_an_hour_missing_a_bar_is_not_used():
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    bars = _timed(8, start)
    # Drop 00:45 by jumping that timestamp forward an hour's worth of labels: three bars in
    # hour 0, four in hour 1, and the last bar opens hour 2.
    bars.times = [
        start + timedelta(minutes=15 * i) for i in (0, 1, 2, 4, 5, 6, 7, 8)
    ]
    closed = frames_as_of(bars, 7)["H1"]
    assert len(closed[2]) == 1
    assert closed[0] == [101]


def test_a_daily_candle_uses_the_whole_utc_day_and_not_the_next_open():
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    bars = _timed(97, start)
    bars.highs[50] = 150
    assert frames_as_of(bars, 95)["D1"][2] == []
    assert frames_as_of(bars, 96)["D1"][0] == [150]
    bars.highs[96] = 9999
    assert frames_as_of(bars, 96)["D1"][0] == [150]


def test_a_later_spike_does_not_change_the_read_at_an_earlier_bar():
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    bars = _timed(40, start)
    before = frames_as_of(bars, 20)
    before_signal = Kappa().scan(bars, 20)
    bars.highs[-1] = 9999
    assert frames_as_of(bars, 20) == before
    assert Kappa().scan(bars, 20) == before_signal


def test_kappa_is_silent_until_there_is_enough_history_for_a_daily_trend():
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    bars = _timed(30, start)
    assert Kappa().scan(bars, len(bars) - 1) is None


# ---- registration: recorded, not cleared to trade --------------------------------


def test_kappa_is_registered_with_the_written_geometry_and_one_symbol():
    kappa = REGISTRY["kappa"]
    assert (kappa.timeframe, kappa.stop_atr, kappa.target_atr, kappa.max_bars) == ("M15", 1.5, 3.0, 32)
    assert kappa.asset_classes == ("vol_index",)
    assert kappa.symbols == (SYMBOL,)
    assert kappa.warmup == LOOKBACK_M15 == 42 * 96


def test_kappa_is_not_an_approved_open():
    allowed, reason = open_allowed("kappa", SYMBOL)
    assert allowed is False
    assert "no approved setups" in reason


def _flat_candles(n=5):
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    return [
        Candle(start + timedelta(minutes=15 * i), 1.0, 1.0, 1.0, 1.0, 0)
        for i in range(n)
    ]


def test_the_replay_scores_only_volatility_100_1s():
    candles = {
        SYMBOL: _flat_candles(),
        "Volatility 75 Index": _flat_candles(),
        "EURUSD": _flat_candles(),
    }
    report = run_playbook(Kappa(), candles)
    assert [row.symbols for row in report.rows] == [[SYMBOL]]
    assert report.rows[0].real.n == 0


def _candles_from_ohlc(ohlc) -> list[Candle]:
    opens, highs, lows, closes = ohlc
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    return [
        Candle(start + timedelta(minutes=15 * i), o, h, l, c, 0)
        for i, (o, h, l, c) in enumerate(zip(opens, highs, lows, closes))
    ]


def test_the_tool_marks_the_aligned_zone_and_the_buy():
    h1 = _bullish_gap()
    m15 = _with_retrace(*h1, [(121, 122, 105, 108), (108, 113, 106, 112)])
    # One extra H1 bar so dropping the forming bar still leaves the gap, with a bar after it.
    h1_open = _with_retrace(*h1, [(121, 122, 118, 121)])
    book = {
        "D1": _candles_from_ohlc(_daily_uptrend()),
        "H4": _candles_from_ohlc(_quiet(20, 50.0)),
        "H1": _candles_from_ohlc(h1_open),
        "M15": _candles_from_ohlc(m15),
    }

    class Broker:
        def get_candles(self, symbol, timeframe, count):
            return book[timeframe][:count]

    box = TradingToolbox(Broker())
    evidence = TradeEvidence()
    text, is_error = box.run("get_aligned_fvgs", {"symbol": SYMBOL}, evidence)
    assert is_error is False
    payload = json.loads(text)
    assert payload["daily_regime"] == "BULLISH_TREND"
    assert payload["continuation"] == "buy"
    assert payload["aligned_zones"] == [{
        "direction": "bullish",
        "bottom": 100.0,
        "top": 110.0,
        "timeframes": ["H1", "M15"],
    }]
    assert "not an approved open" in payload["note"]
    assert evidence.timeframes_used == ["D1", "H4", "H1", "M15"]
    assert SYMBOL in evidence.symbols_checked


def test_the_tool_refuses_a_symbol_with_a_missing_timeframe():
    class Broker:
        def get_candles(self, symbol, timeframe, count):
            return [] if timeframe == "H1" else _flat_candles(20)

    text, is_error = TradingToolbox(Broker()).run(
        "get_aligned_fvgs", {"symbol": SYMBOL}, TradeEvidence())
    assert is_error is True
    assert "H1" in text


def test_the_forming_higher_timeframe_bar_cannot_create_the_gap():
    """A gap whose third candle is the newest H1 bar is still forming, so it is not marked."""
    h1 = _bullish_gap()  # the gap's third candle is not the last bar; one bar follows it
    # Move that following bar onto the end of a series where the gap candle IS last by
    # cutting the follower off. The tool then drops one more bar (the forming one).
    gap_is_last = tuple(series[:-1] for series in h1)
    m15 = _quiet(20, 90.0)
    daily = _daily_uptrend()
    from trading.fvg_align import read_broker_candles

    marked = read_broker_candles({
        "D1": _candles_from_ohlc(daily),
        "H4": _candles_from_ohlc(_quiet(20, 50.0)),
        "H1": _candles_from_ohlc(gap_is_last),
        "M15": _candles_from_ohlc(m15),
    })
    assert all("H1" not in zone.timeframes for zone in marked.zones)
