"""Pure-Python technical indicators. No external dependencies.

All functions take plain lists of floats and return floats or dicts.
Requires at least period+1 data points; returns None when there is not enough data.
"""
from __future__ import annotations

import math
from typing import Optional


def sma(prices: list[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def _ema_series(prices: list[float], period: int) -> list[float]:
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(prices[:period]) / period]
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def ema(prices: list[float], period: int) -> Optional[float]:
    s = _ema_series(prices, period)
    return s[-1] if s else None


def rsi(prices: list[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1 + rs)), 2)


def macd(
    prices: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Optional[dict[str, float]]:
    if len(prices) < slow + signal:
        return None
    ema_fast = _ema_series(prices, fast)
    ema_slow = _ema_series(prices, slow)
    # align: ema_slow is shorter by (slow - fast) values
    offset = slow - fast
    macd_line = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]
    if len(macd_line) < signal:
        return None
    signal_line = _ema_series(macd_line, signal)
    if not signal_line:
        return None
    hist = macd_line[-1] - signal_line[-1]
    return {
        "macd": round(macd_line[-1], 6),
        "signal": round(signal_line[-1], 6),
        "histogram": round(hist, 6),
    }


def bollinger_bands(
    prices: list[float],
    period: int = 20,
    std_dev: float = 2.0,
) -> Optional[dict[str, float]]:
    if len(prices) < period:
        return None
    window = prices[-period:]
    mid = sum(window) / period
    variance = sum((p - mid) ** 2 for p in window) / period
    std = math.sqrt(variance)
    return {
        "upper": round(mid + std_dev * std, 6),
        "middle": round(mid, 6),
        "lower": round(mid - std_dev * std, 6),
        "bandwidth": round((std_dev * 2 * std) / mid * 100, 3) if mid else 0.0,
    }


def atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    atr_val = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
    return round(atr_val, 6)


def stochastic(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    k_period: int = 14,
    d_period: int = 3,
) -> Optional[dict[str, float]]:
    if len(closes) < k_period + d_period:
        return None
    k_values = []
    for i in range(k_period - 1, len(closes)):
        window_h = highs[i - k_period + 1: i + 1]
        window_l = lows[i - k_period + 1: i + 1]
        highest = max(window_h)
        lowest = min(window_l)
        denom = highest - lowest
        k = ((closes[i] - lowest) / denom * 100) if denom else 50.0
        k_values.append(k)
    if len(k_values) < d_period:
        return None
    d = sum(k_values[-d_period:]) / d_period
    return {"k": round(k_values[-1], 2), "d": round(d, 2)}


def pivot_points(high: float, low: float, close: float) -> dict[str, float]:
    """Classic pivot points from the previous candle's H/L/C."""
    pivot = (high + low + close) / 3
    r1 = 2 * pivot - low
    s1 = 2 * pivot - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    r3 = high + 2 * (pivot - low)
    s3 = low - 2 * (high - pivot)
    return {
        "pivot": round(pivot, 5),
        "r1": round(r1, 5), "r2": round(r2, 5), "r3": round(r3, 5),
        "s1": round(s1, 5), "s2": round(s2, 5), "s3": round(s3, 5),
    }


def trend_strength(closes: list[float], period: int = 20) -> str:
    """Simple linear regression slope direction over `period` bars."""
    if len(closes) < period:
        return "unknown"
    window = closes[-period:]
    n = len(window)
    x_mean = (n - 1) / 2.0
    y_mean = sum(window) / n
    num = sum((i - x_mean) * (window[i] - y_mean) for i in range(n))
    denom = sum((i - x_mean) ** 2 for i in range(n))
    if denom == 0:
        return "flat"
    slope = num / denom
    pct = abs(slope) / (y_mean or 1) * 100
    if pct < 0.005:
        return "flat"
    return "up" if slope > 0 else "down"


def compute_all(
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> dict:
    """Compute all indicators in one call. Returns a dict safe for JSON serialisation."""
    result: dict = {}
    rsi_val = rsi(closes)
    if rsi_val is not None:
        result["rsi_14"] = rsi_val

    macd_val = macd(closes)
    if macd_val:
        result["macd"] = macd_val

    bb = bollinger_bands(closes)
    if bb:
        result["bollinger"] = bb

    atr_val = atr(highs, lows, closes)
    if atr_val is not None:
        result["atr_14"] = atr_val

    stoch = stochastic(highs, lows, closes)
    if stoch:
        result["stochastic"] = stoch

    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)
    if sma20 is not None:
        result["sma_20"] = round(sma20, 6)
    if sma50 is not None:
        result["sma_50"] = round(sma50, 6)
    if sma200 is not None:
        result["sma_200"] = round(sma200, 6)

    result["trend_20"] = trend_strength(closes, 20)

    if len(closes) >= 2:
        prev = closes[-2]
        pp = pivot_points(highs[-2], lows[-2], prev)
        result["pivot_points"] = pp

    return result
