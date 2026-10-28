"""Tools Claude can call during a trading session.

Each tool is a read-only lookup: market data, account state, open positions.
The model never executes trades directly — it calls submit_trade, and the
guardrails/runner decide whether to execute.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import textwrap
from dataclasses import dataclass, field
from typing import Any, Optional

from .broker.metatrader import MT5Broker
from .geometry import analyze_candles
from .indicators import atr as _atr

log = logging.getLogger(__name__)

VALID_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")

# Builtins allowed in the run_analysis sandbox
_SANDBOX_BUILTINS: dict = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float, "int": int,
    "isinstance": isinstance, "len": len, "list": list, "map": map,
    "max": max, "min": min, "print": print, "range": range, "round": round,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "True": True, "False": False, "None": None,
}
_SANDBOX_BLOCKED = [
    "__import__", "import ", "open(", "exec(", "eval(", "compile(",
    "os.", "sys.", "subprocess", "socket", "urllib", "requests",
    "builtins", "globals(", "locals(", "vars(", "getattr(", "setattr(",
]


@dataclass
class TradeEvidence:
    """Collects facts gathered during the agent session."""
    symbols_checked: list[str] = field(default_factory=list)
    timeframes_used: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    account_read: bool = False
    positions_read: bool = False
    candle_bars: dict[str, int] = field(default_factory=dict)


class TradingToolbox:
    """Wraps the MT5 broker and exposes read-only tools for the agent."""

    def __init__(self, broker: MT5Broker,
                 history_fn: Optional[Any] = None) -> None:
        self._broker = broker
        self._history_fn = history_fn   # callable() -> list[dict] of recent closed trades

    def schemas(self) -> list[dict]:
        return [
            _SCHEMA_GET_CANDLES,
            _SCHEMA_GET_STRUCTURE,
            _SCHEMA_RUN_ANALYSIS,
            _SCHEMA_GET_ACCOUNT_STATE,
            _SCHEMA_GET_OPEN_POSITIONS,
            _SCHEMA_GET_PRICE_INFO,
            _SCHEMA_ASSESS_MARKET,
            _SCHEMA_GET_TRADE_HISTORY,
        ]

    def run(
        self,
        name: str,
        args: dict[str, Any],
        evidence: TradeEvidence,
    ) -> tuple[str, bool]:
        """Dispatch a tool call. Returns (output_text, is_error)."""
        evidence.tools_used.append(name)
        try:
            if name == "get_candles":
                return self._get_candles(args, evidence)
            if name == "get_structure":
                return self._get_structure(args, evidence)
            if name == "run_analysis":
                return self._run_analysis(args, evidence)
            if name == "get_account_state":
                return self._get_account_state(evidence)
            if name == "get_open_positions":
                return self._get_open_positions(args, evidence)
            if name == "get_price_info":
                return self._get_price_info(args, evidence)
            if name == "assess_market":
                return self._assess_market(args, evidence)
            if name == "get_trade_history":
                return self._get_trade_history(args, evidence)
            return f"Unknown tool: {name}", True
        except Exception as exc:  # noqa: BLE001
            log.warning("Tool %s failed: %s", name, exc)
            return f"Tool error: {exc}", True

    # ---- tools -------------------------------------------------------------------

    def _get_candles(self, args: dict, evidence: TradeEvidence) -> tuple[str, bool]:
        symbol = str(args.get("symbol", ""))
        timeframe = str(args.get("timeframe", "H1")).upper()
        count = min(int(args.get("count", 100)), 500)

        if timeframe not in VALID_TIMEFRAMES:
            return f"Invalid timeframe {timeframe!r}. Use one of: {', '.join(VALID_TIMEFRAMES)}", True

        candles = self._broker.get_candles(symbol, timeframe, count)
        if not candles:
            return f"No candle data for {symbol} {timeframe}. Check that the symbol is available on your MT5.", True

        evidence.symbols_checked.append(symbol)
        evidence.timeframes_used.append(timeframe)
        evidence.candle_bars[f"{symbol}_{timeframe}"] = len(candles)

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        atr_val = _atr(highs, lows, closes)

        recent = [c.as_dict() for c in candles[-30:]]
        result = {
            "symbol": symbol,
            "timeframe": timeframe,
            "bars_fetched": len(candles),
            "last_30_candles": recent,
            "current_price": closes[-1],
            "atr_14": atr_val,
            "note": (
                "To compute custom indicators or structures, use run_analysis with this "
                "symbol and timeframe. All OHLCV arrays are available there."
            ),
        }
        return json.dumps(result, indent=2), False

    def _get_structure(self, args: dict, evidence: TradeEvidence) -> tuple[str, bool]:
        symbol = str(args.get("symbol", ""))
        timeframe = str(args.get("timeframe", "H1")).upper()
        count = min(int(args.get("count", 200)), 500)
        swing_n = max(1, min(int(args.get("swing_n", 3)), 10))

        if timeframe not in VALID_TIMEFRAMES:
            return f"Invalid timeframe {timeframe!r}. Use one of: {', '.join(VALID_TIMEFRAMES)}", True

        candles = self._broker.get_candles(symbol, timeframe, count)
        if not candles:
            return f"No candle data for {symbol} {timeframe}. Check that the symbol is available on your MT5.", True

        evidence.symbols_checked.append(symbol)
        evidence.timeframes_used.append(timeframe)
        evidence.candle_bars[f"{symbol}_{timeframe}"] = len(candles)

        result = analyze_candles(candles, swing_n)
        if "error" in result:
            return f"{symbol} {timeframe}: {result['error']}", True
        return json.dumps({"symbol": symbol, "timeframe": timeframe, "swing_n": swing_n, **result}, indent=2), False

    def _run_analysis(self, args: dict, evidence: TradeEvidence) -> tuple[str, bool]:
        """Execute custom Python analysis code on raw candle data."""
        symbol = str(args.get("symbol", ""))
        timeframe = str(args.get("timeframe", "H1")).upper()
        count = min(int(args.get("count", 100)), 500)
        code = str(args.get("code", ""))

        if not code.strip():
            return "No code provided.", True
        if timeframe not in VALID_TIMEFRAMES:
            return f"Invalid timeframe {timeframe!r}.", True

        # Safety check
        for blocked in _SANDBOX_BLOCKED:
            if blocked in code:
                return f"Blocked pattern '{blocked}' is not allowed in analysis code.", True

        candles = self._broker.get_candles(symbol, timeframe, count)
        if not candles:
            return f"No candle data for {symbol} {timeframe}.", True

        evidence.symbols_checked.append(symbol)
        evidence.timeframes_used.append(timeframe)

        opens   = [c.open  for c in candles]
        highs   = [c.high  for c in candles]
        lows    = [c.low   for c in candles]
        closes  = [c.close for c in candles]
        volumes = [c.volume for c in candles]
        times   = [c.time.isoformat() if hasattr(c.time, "isoformat") else str(c.time)
                   for c in candles]

        namespace: dict = {
            "__builtins__": _SANDBOX_BUILTINS,
            "math": __import__("math"),
            "statistics": __import__("statistics"),
            "opens": opens, "highs": highs, "lows": lows,
            "closes": closes, "volumes": volumes, "times": times,
        }

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                exec(textwrap.dedent(code), namespace)  # noqa: S102
        except Exception as exc:  # noqa: BLE001
            err_out = buf.getvalue()
            return f"Runtime error: {exc}" + (f"\nOutput so far:\n{err_out}" if err_out else ""), True

        output = buf.getvalue().strip()
        # Also expose a 'result' variable if set
        if not output and "result" in namespace:
            output = str(namespace["result"])
        if not output:
            output = "(no output — use print() to show your results)"

        return output[:4000], False

    def _get_account_state(self, evidence: TradeEvidence) -> tuple[str, bool]:
        account = self._broker.get_account()
        if account is None:
            return "Could not read account info from MT5.", True
        evidence.account_read = True
        positions = self._broker.get_positions()
        n = len(positions)
        total_profit = sum(p.profit for p in positions)
        result = {
            **account.as_dict(),
            "open_positions": n,
            "unrealised_profit": round(total_profit, 2),
        }
        return json.dumps(result, indent=2), False

    def _get_open_positions(self, args: dict, evidence: TradeEvidence) -> tuple[str, bool]:
        symbol = args.get("symbol")  # optional filter
        positions = self._broker.get_positions(symbol=symbol)
        evidence.positions_read = True
        if not positions:
            msg = f"No open positions on {symbol}." if symbol else "No open positions."
            return msg, False
        result = [p.as_dict() for p in positions]
        return json.dumps(result, indent=2), False

    def _get_price_info(self, args: dict, evidence: TradeEvidence) -> tuple[str, bool]:
        symbol = str(args.get("symbol", ""))
        info = self._broker.get_symbol_info(symbol)
        if info is None:
            return f"Symbol {symbol!r} not found. Check the exact name as it appears in MT5.", True
        evidence.symbols_checked.append(symbol)
        result = {
            "symbol": symbol,
            "bid": info.bid,
            "ask": info.ask,
            "spread_pips": info.spread_pips(),
            "digits": info.digits,
            "pip_size": info.pip_size,
            "pip_value_per_lot": round(info.pip_value_per_lot, 4),
            "volume_min": info.volume_min,
            "volume_step": info.volume_step,
        }
        return json.dumps(result, indent=2), False

    def _assess_market(self, args: dict, evidence: TradeEvidence) -> tuple[str, bool]:
        """Multi-timeframe raw OHLCV overview across H4 / H1 / M15."""
        symbol = str(args.get("symbol", ""))
        summary: dict[str, Any] = {"symbol": symbol, "timeframes": {}}

        for tf, count in [("H4", 100), ("H1", 50), ("M15", 30)]:
            candles = self._broker.get_candles(symbol, tf, count)
            if not candles:
                summary["timeframes"][tf] = "no data"
                continue
            closes = [c.close for c in candles]
            highs  = [c.high  for c in candles]
            lows   = [c.low   for c in candles]
            atr_val = _atr(highs, lows, closes)
            last5 = [c.as_dict() for c in candles[-5:]]
            summary["timeframes"][tf] = {
                "close": closes[-1],
                "high_20": round(max(highs[-20:]), 6),
                "low_20": round(min(lows[-20:]), 6),
                "atr_14": atr_val,
                "last_5_candles": last5,
            }
            evidence.symbols_checked.append(symbol)
            evidence.timeframes_used.append(tf)

        info = self._broker.get_symbol_info(symbol)
        if info:
            summary["current_price"] = info.ask
            summary["spread_pips"] = info.spread_pips()
            summary["pip_size"] = info.pip_size

        summary["tip"] = "Use run_analysis to compute custom structures on any timeframe."
        return json.dumps(summary, indent=2), False

    def _get_trade_history(self, args: dict, evidence: TradeEvidence) -> tuple[str, bool]:
        """Return recent closed trades and performance stats."""
        limit = min(int(args.get("limit", 20)), 50)
        if self._history_fn is None:
            return json.dumps({"message": "No trade history available yet.", "trades": []}), False
        trades = self._history_fn(limit)
        if not trades:
            return json.dumps({"message": "No closed trades yet.", "trades": []}), False
        pnls = [(t.get("pnl") or t.get("profit") or 0) for t in trades]
        wins = sum(1 for p in pnls if p > 0)
        total_pnl = round(sum(pnls), 2)
        avg_win = round(sum(p for p in pnls if p > 0) / max(wins, 1), 2)
        avg_loss = round(sum(p for p in pnls if p <= 0) / max(len(pnls) - wins, 1), 2)
        by_symbol: dict[str, dict] = {}
        for t, p in zip(trades, pnls):
            sym = t.get("symbol", "?")
            if sym not in by_symbol:
                by_symbol[sym] = {"trades": 0, "wins": 0, "pnl": 0.0}
            by_symbol[sym]["trades"] += 1
            by_symbol[sym]["wins"] += 1 if p > 0 else 0
            by_symbol[sym]["pnl"] = round(by_symbol[sym]["pnl"] + p, 2)
        result = {
            "summary": {
                "closed_trades": len(trades),
                "wins": wins,
                "losses": len(trades) - wins,
                "win_rate": f"{wins/len(trades):.0%}",
                "total_pnl": total_pnl,
                "avg_win": avg_win,
                "avg_loss": avg_loss,
            },
            "by_symbol": by_symbol,
            "recent_trades": [
                {
                    "symbol": t.get("symbol"),
                    "direction": t.get("direction"),
                    "pnl": t.get("pnl") or t.get("profit") or 0,
                    "close_reason": t.get("close_reason") or t.get("status"),
                    "open_time": str(t.get("open_time") or "")[:16],
                }
                for t in trades[:10]
            ],
        }
        return json.dumps(result, indent=2), False


# ---- tool schemas ----------------------------------------------------------------

_SCHEMA_GET_CANDLES: dict = {
    "name": "get_candles",
    "description": (
        "Fetch raw OHLCV candles + ATR for a symbol and timeframe. Returns last 30 candles. "
        "Use run_analysis to compute any custom structures or indicators on the full history."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "e.g. EURUSD, XAUUSD, Volatility 75 Index"},
            "timeframe": {
                "type": "string",
                "enum": list(VALID_TIMEFRAMES),
                "description": "Candle timeframe.",
            },
            "count": {
                "type": "integer",
                "description": "Number of bars to fetch (max 500, default 100).",
            },
        },
        "required": ["symbol", "timeframe"],
    },
}

_SCHEMA_GET_STRUCTURE: dict = {
    "name": "get_structure",
    "description": (
        "Deterministic market structure for a symbol and timeframe, computed in code from the raw "
        "candles: regime (bullish/bearish trend or range), last three swing highs and lows, the last "
        "break of structure (BOS) or change of character (CHOCH), the nearest unswept equal highs "
        "and lows, any liquidity sweep in the last few bars (wick through a level, close back "
        "inside), and unfilled fair value gaps. Use this instead of writing swing-detection code "
        "yourself, then use run_analysis for anything else you want to test."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "e.g. EURUSD, XAUUSD, Volatility 75 Index"},
            "timeframe": {"type": "string", "enum": list(VALID_TIMEFRAMES), "description": "Candle timeframe."},
            "count": {"type": "integer", "description": "Bars to analyse (max 500, default 200)."},
            "swing_n": {
                "type": "integer",
                "description": "Bars on each side that confirm a swing (1-10, default 3). Larger finds fewer, more major swings.",
            },
        },
        "required": ["symbol", "timeframe"],
    },
}

_SCHEMA_RUN_ANALYSIS: dict = {
    "name": "run_analysis",
    "description": (
        "Execute your own Python code on raw OHLCV candle data for a symbol/timeframe. "
        "Available in your code: opens, highs, lows, closes, volumes, times (lists, oldest→newest), "
        "plus math and statistics modules. Use print() to output results. "
        "Write any analysis you need: custom pattern detection, volume structures, "
        "momentum measures, mean-reversion filters, volatility regimes — anything."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Symbol to fetch candles for."},
            "timeframe": {
                "type": "string",
                "enum": list(VALID_TIMEFRAMES),
                "description": "Candle timeframe.",
            },
            "count": {
                "type": "integer",
                "description": "Number of candles to load (max 500, default 100).",
            },
            "code": {
                "type": "string",
                "description": (
                    "Python code to execute. Use print() to show results. "
                    "Imports are not available — use math.sqrt(), statistics.mean() etc. directly."
                ),
            },
        },
        "required": ["symbol", "timeframe", "code"],
    },
}

_SCHEMA_GET_ACCOUNT_STATE: dict = {
    "name": "get_account_state",
    "description": "Read current account balance, equity, free margin, margin level, and total unrealised P&L.",
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

_SCHEMA_GET_OPEN_POSITIONS: dict = {
    "name": "get_open_positions",
    "description": "List all open positions with direction, lots, entry price, SL, TP, and current profit.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Optional: filter by symbol. Leave empty to get all open positions.",
            }
        },
        "required": [],
    },
}

_SCHEMA_GET_PRICE_INFO: dict = {
    "name": "get_price_info",
    "description": "Get the current bid/ask, spread in pips, pip value, and lot size constraints for a symbol.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Symbol name exactly as in MT5."}
        },
        "required": ["symbol"],
    },
}

_SCHEMA_ASSESS_MARKET: dict = {
    "name": "assess_market",
    "description": (
        "Multi-timeframe market assessment (H4, H1, M15) for a symbol: trend direction, "
        "RSI, MACD, ATR, key moving averages, and spread. Use this as a quick overview "
        "before drilling into individual timeframes with get_candles."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Symbol to assess."}
        },
        "required": ["symbol"],
    },
}

_SCHEMA_GET_TRADE_HISTORY: dict = {
    "name": "get_trade_history",
    "description": (
        "Return recent closed trades with win rate, average P&L, and per-symbol breakdown. "
        "Call this early in your session to learn what has been working and adapt your approach."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Number of recent closed trades to return (max 50, default 20).",
            }
        },
        "required": [],
    },
}
