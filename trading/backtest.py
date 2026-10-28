"""Historical backtester for the trading agent.

Pre-loads all candles from MT5 for the requested window, then walks
regular decision timestamps through the same MarketAgent loop.
SL/TP hits are simulated on H1 candles between decision points.

Usage:
    python -m trading backtest --from 2026-05-01 --to 2026-10-15
    python -m trading backtest --symbol EURUSD --from 2026-09-01
    python -m trading backtest --from 2026-05-01 --interval 14
    python -m trading backtest --list
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .broker.metatrader import AccountState, Candle, MT5Broker, Position, SymbolInfo
from .config import Settings
from .decision import CLOSE, MODIFY, OPEN, WAIT, TradeDecision
from .guardrails import apply_guardrails, atr_for, open_risk_pct
from .prompt import render_prompt
from .loop import AgentError, MarketAgent, TradeResult
from .risk_manager import RiskManager
from .tools import TradingToolbox

log = logging.getLogger(__name__)

# ── Symbol specifications (approximate; used when MT5 is offline) ──────────────
# tick_value = USD per tick per 1 standard lot
_SPEC: dict[str, dict] = {
    # Forex — USD quoted
    "EURUSD":                      dict(digits=5, point=1e-5, tick_size=1e-5, tick_value=1.0,  vol_min=0.01,  vol_step=0.01),
    "GBPUSD":                      dict(digits=5, point=1e-5, tick_size=1e-5, tick_value=1.0,  vol_min=0.01,  vol_step=0.01),
    "AUDUSD":                      dict(digits=5, point=1e-5, tick_size=1e-5, tick_value=1.0,  vol_min=0.01,  vol_step=0.01),
    "NZDUSD":                      dict(digits=5, point=1e-5, tick_size=1e-5, tick_value=1.0,  vol_min=0.01,  vol_step=0.01),
    "EURGBP":                      dict(digits=5, point=1e-5, tick_size=1e-5, tick_value=1.27, vol_min=0.01,  vol_step=0.01),
    # Forex — USD base (counter is not USD, tick value approx)
    "USDJPY":                      dict(digits=3, point=0.001, tick_size=0.001, tick_value=0.67, vol_min=0.01,  vol_step=0.01),
    "USDCHF":                      dict(digits=5, point=1e-5, tick_size=1e-5, tick_value=1.10, vol_min=0.01,  vol_step=0.01),
    "USDCAD":                      dict(digits=5, point=1e-5, tick_size=1e-5, tick_value=0.74, vol_min=0.01,  vol_step=0.01),
    "EURJPY":                      dict(digits=3, point=0.001, tick_size=0.001, tick_value=0.67, vol_min=0.01,  vol_step=0.01),
    # Metals
    "XAUUSD":                      dict(digits=2, point=0.01,  tick_size=0.01,  tick_value=1.0,  vol_min=0.01,  vol_step=0.01),
    "XAGUSD":                      dict(digits=3, point=0.001, tick_size=0.001, tick_value=5.0,  vol_min=0.01,  vol_step=0.01),
    # Volatility indices
    "Volatility 10 Index":         dict(digits=2, point=0.01,  tick_size=0.01,  tick_value=0.10, vol_min=0.001, vol_step=0.001),
    "Volatility 25 Index":         dict(digits=2, point=0.01,  tick_size=0.01,  tick_value=0.10, vol_min=0.001, vol_step=0.001),
    "Volatility 50 Index":         dict(digits=2, point=0.01,  tick_size=0.01,  tick_value=0.10, vol_min=0.001, vol_step=0.001),
    "Volatility 75 Index":         dict(digits=2, point=0.01,  tick_size=0.01,  tick_value=0.10, vol_min=0.001, vol_step=0.001),
    "Volatility 100 Index":        dict(digits=2, point=0.01,  tick_size=0.01,  tick_value=0.10, vol_min=0.001, vol_step=0.001),
    # Volatility 1s indices
    "Volatility 10 (1s) Index":    dict(digits=2, point=0.01,  tick_size=0.01,  tick_value=0.10, vol_min=0.001, vol_step=0.001),
    "Volatility 25 (1s) Index":    dict(digits=2, point=0.01,  tick_size=0.01,  tick_value=0.10, vol_min=0.001, vol_step=0.001),
    "Volatility 50 (1s) Index":    dict(digits=2, point=0.01,  tick_size=0.01,  tick_value=0.10, vol_min=0.001, vol_step=0.001),
    "Volatility 75 (1s) Index":    dict(digits=2, point=0.01,  tick_size=0.01,  tick_value=0.10, vol_min=0.001, vol_step=0.001),
    "Volatility 100 (1s) Index":   dict(digits=2, point=0.01,  tick_size=0.01,  tick_value=0.10, vol_min=0.001, vol_step=0.001),
    # Step Index
    "Step Index":                  dict(digits=2, point=0.1,   tick_size=0.1,   tick_value=0.10, vol_min=0.1,   vol_step=0.1),
    # Boom indices
    "Boom 300 Index":              dict(digits=2, point=1.0,   tick_size=1.0,   tick_value=0.10, vol_min=0.01,  vol_step=0.01),
    "Boom 500 Index":              dict(digits=2, point=1.0,   tick_size=1.0,   tick_value=0.10, vol_min=0.01,  vol_step=0.01),
    "Boom 1000 Index":             dict(digits=2, point=1.0,   tick_size=1.0,   tick_value=0.10, vol_min=0.01,  vol_step=0.01),
    # Crash indices
    "Crash 300 Index":             dict(digits=2, point=1.0,   tick_size=1.0,   tick_value=0.10, vol_min=0.01,  vol_step=0.01),
    "Crash 500 Index":             dict(digits=2, point=1.0,   tick_size=1.0,   tick_value=0.10, vol_min=0.01,  vol_step=0.01),
    "Crash 1000 Index":            dict(digits=2, point=1.0,   tick_size=1.0,   tick_value=0.10, vol_min=0.01,  vol_step=0.01),
}
_DFLT_SPEC = dict(digits=5, point=1e-5, tick_size=1e-5, tick_value=1.0, vol_min=0.01, vol_step=0.01)

# Approximate spread in price-pips for backtesting (kept low so guardrail passes in simulation)
_SPREAD_PIPS: dict[str, float] = {
    # Forex
    "EURUSD": 1.2, "GBPUSD": 1.5, "AUDUSD": 1.5, "NZDUSD": 2.0,
    "USDJPY": 1.5, "USDCHF": 2.0, "USDCAD": 2.0,
    "EURJPY": 2.0, "EURGBP": 2.0,
    # Metals
    "XAUUSD": 3.0, "XAGUSD": 3.0,
    # Volatility indices
    "Volatility 10 Index": 0.5,  "Volatility 25 Index": 0.5,
    "Volatility 50 Index": 0.5,  "Volatility 75 Index": 0.5,
    "Volatility 100 Index": 1.0,
    # Volatility 1s indices
    "Volatility 10 (1s) Index": 1.0,  "Volatility 25 (1s) Index": 1.0,
    "Volatility 50 (1s) Index": 1.0,  "Volatility 75 (1s) Index": 1.0,
    "Volatility 100 (1s) Index": 1.0,
    # Step / Boom / Crash
    "Step Index": 0.5,
    "Boom 300 Index": 2.0,  "Boom 500 Index": 2.0,  "Boom 1000 Index": 2.0,
    "Crash 300 Index": 2.0, "Crash 500 Index": 2.0, "Crash 1000 Index": 2.0,
}

TIMEFRAMES_NEEDED = ["M1", "M5", "M15", "H1", "H4", "D1"]


# ── Historical data store ──────────────────────────────────────────────────────

class HistoricalDataStore:
    """Candles pre-loaded for all (symbol, timeframe) pairs.

    Provides sliced views strictly before a given timestamp so the
    BacktestBroker can serve data without leaking future prices.
    """

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], list[Candle]] = {}

    def load(self, broker: MT5Broker, symbols: list[str],
             date_from: datetime, date_to: datetime) -> None:
        """Download all candles for the backtest window plus warm-up."""
        total = 0
        for symbol in symbols:
            for tf in TIMEFRAMES_NEEDED:
                candles = broker.get_candles_range(symbol, tf, date_from, date_to)
                self._data[(symbol, tf)] = sorted(candles, key=lambda c: c.time)
                log.info("  %-28s %-4s  %d bars", symbol, tf, len(candles))
                total += len(candles)
        log.info("Historical store: %d candles across %d series", total, len(self._data))

    def slice_before(self, symbol: str, tf: str,
                     as_of: datetime, count: int) -> list[Candle]:
        """Return up to `count` candles with time < as_of."""
        all_c = self._data.get((symbol, tf), [])
        idx = _bisect_lt(all_c, as_of)
        start = max(0, idx - count)
        return all_c[start:idx]

    def candles_between(self, symbol: str, tf: str,
                        t_from: datetime, t_to: datetime) -> list[Candle]:
        """Candles where t_from <= time < t_to."""
        all_c = self._data.get((symbol, tf), [])
        lo = _bisect_lt(all_c, t_from)
        hi = _bisect_lt(all_c, t_to)
        return all_c[lo:hi]


def _bisect_lt(candles: list[Candle], target: datetime) -> int:
    """Index of the first candle with time >= target (binary search)."""
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid].time < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ── Simulated trade record ─────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    ticket: int
    symbol: str
    direction: str
    lots: float
    open_price: float
    sl: float
    tp: float
    open_time: datetime
    comment: str = ""
    close_price: float = 0.0
    close_time: Optional[datetime] = None
    pnl: float = 0.0
    close_reason: str = ""   # "sl" | "tp" | "agent" | "end"


# ── Backtest broker ────────────────────────────────────────────────────────────

class BacktestBroker:
    """Drop-in replacement for MT5Broker that serves historical data.

    Call set_time(ts) at each decision step. Candle requests return data
    strictly before that timestamp. Execution is simulated in-memory.
    """

    def __init__(self, store: HistoricalDataStore,
                 initial_balance: float = 10000.0) -> None:
        self._store = store
        self._balance: float = initial_balance
        self._open: list[BacktestTrade] = []
        self._closed: list[BacktestTrade] = []
        self._ticket = 1
        self._connected = True
        self.current_time: datetime = datetime.now(tz=timezone.utc)

    # ---- time control --------------------------------------------------------

    def set_time(self, ts: datetime) -> None:
        self.current_time = ts

    # ---- MT5Broker interface (duck-typed) ------------------------------------

    def get_candles(self, symbol: str, timeframe: str,
                    count: int = 100) -> list[Candle]:
        return self._store.slice_before(symbol, timeframe, self.current_time, count)

    def get_candles_range(self, symbol: str, timeframe: str,
                          date_from: datetime, date_to: datetime) -> list[Candle]:
        return self._store.candles_between(symbol, timeframe, date_from, date_to)

    def get_symbol_info(self, symbol: str) -> Optional[SymbolInfo]:
        candles = self._store.slice_before(symbol, "H1", self.current_time, 1)
        if not candles:
            return None
        sp = _SPEC.get(symbol, _DFLT_SPEC)
        spread_pips = _SPREAD_PIPS.get(symbol, 2.0)
        spread_price = spread_pips * sp["point"]
        mid = candles[-1].close
        return SymbolInfo(
            name=symbol,
            digits=sp["digits"],
            point=sp["point"],
            trade_tick_size=sp["tick_size"],
            trade_tick_value=sp["tick_value"],
            trade_contract_size=1.0,
            volume_min=sp["vol_min"],
            volume_max=500.0,
            volume_step=sp["vol_step"],
            spread=int(round(spread_pips)),
            bid=round(mid, sp["digits"]),
            ask=round(mid + spread_price, sp["digits"]),
        )

    def get_account(self) -> Optional[AccountState]:
        equity = self._calc_equity()
        return AccountState(
            balance=round(self._balance, 2),
            equity=round(equity, 2),
            margin=0.0,
            free_margin=round(equity, 2),
            margin_level=0.0,
            profit=round(equity - self._balance, 2),
        )

    def get_positions(self, symbol: Optional[str] = None) -> list[Position]:
        result = []
        for t in self._open:
            if symbol and t.symbol != symbol:
                continue
            result.append(Position(
                ticket=t.ticket, symbol=t.symbol, direction=t.direction,
                lots=t.lots, open_price=t.open_price, sl=t.sl, tp=t.tp,
                profit=self._unrealised(t), open_time=t.open_time,
                comment=t.comment,
            ))
        return result

    def place_order(self, symbol: str, direction: str, lots: float,
                    sl_price: float, tp_price: float,
                    comment: str = "backtest") -> int:
        info = self.get_symbol_info(symbol)
        price = (info.ask if direction == "buy" else info.bid) if info else 0.0
        ticket = self._ticket
        self._ticket += 1
        self._open.append(BacktestTrade(
            ticket=ticket, symbol=symbol, direction=direction,
            lots=lots, open_price=price, sl=sl_price, tp=tp_price,
            open_time=self.current_time, comment=comment,
        ))
        log.info("BT OPEN   %s %s %.4f lots @ %.5f  SL=%.5f  TP=%.5f",
                 direction, symbol, lots, price, sl_price, tp_price)
        return ticket

    def close_position(self, ticket: int) -> bool:
        for i, t in enumerate(self._open):
            if t.ticket == ticket:
                info = self.get_symbol_info(t.symbol)
                exit_price = (info.bid if t.direction == "buy" else info.ask) if info else t.open_price
                self._close_trade(t, exit_price, "agent")
                self._open.pop(i)
                return True
        return False

    def modify_position(self, ticket: int, sl_price: float,
                        tp_price: float) -> bool:
        for t in self._open:
            if t.ticket == ticket:
                t.sl = sl_price
                t.tp = tp_price
                return True
        return False

    def calculate_lots(self, symbol: str, account_balance: float,
                       risk_pct: float, sl_pips: int) -> float:
        info = self.get_symbol_info(symbol)
        sp = _SPEC.get(symbol, _DFLT_SPEC)
        if info is None or info.pip_value_per_lot == 0 or sl_pips <= 0:
            return sp["vol_min"]
        risk_amount = account_balance * (risk_pct / 100.0)
        raw_lots = risk_amount / (sl_pips * info.pip_value_per_lot)
        step = sp["vol_step"]
        lots = round(round(raw_lots / step) * step, 8)
        # Safety cap: never risk more than 20% of balance on a single position
        max_lots = (account_balance * 0.2) / max(sl_pips * info.pip_value_per_lot, 0.001)
        return max(sp["vol_min"], min(max_lots, lots))

    # ---- SL/TP simulation ----------------------------------------------------

    def simulate_sl_tp(self, t_from: datetime,
                       t_to: datetime) -> list[BacktestTrade]:
        """Walk H1 candles between t_from and t_to for each open position.
        Closes positions that hit SL or TP. Returns newly-closed trades."""
        closed_now: list[BacktestTrade] = []
        still_open: list[BacktestTrade] = []

        for trade in self._open:
            h1 = self._store.candles_between(trade.symbol, "H1", t_from, t_to)
            hit_price = 0.0
            hit_reason = ""
            hit_time: datetime = t_to

            for candle in h1:
                if trade.direction == "buy":
                    if trade.sl > 0 and candle.low <= trade.sl:
                        hit_price, hit_reason, hit_time = trade.sl, "sl", candle.time
                        break
                    if trade.tp > 0 and candle.high >= trade.tp:
                        hit_price, hit_reason, hit_time = trade.tp, "tp", candle.time
                        break
                else:
                    if trade.sl > 0 and candle.high >= trade.sl:
                        hit_price, hit_reason, hit_time = trade.sl, "sl", candle.time
                        break
                    if trade.tp > 0 and candle.low <= trade.tp:
                        hit_price, hit_reason, hit_time = trade.tp, "tp", candle.time
                        break

            if hit_reason:
                self._close_trade(trade, hit_price, hit_reason, hit_time)
                closed_now.append(trade)
            else:
                still_open.append(trade)

        self._open = still_open
        return closed_now

    def close_all_at_market(self, reason: str = "end") -> list[BacktestTrade]:
        """Close all remaining positions at current market price."""
        closed = []
        for trade in self._open:
            info = self.get_symbol_info(trade.symbol)
            exit_price = (info.bid if trade.direction == "buy" else info.ask
                          ) if info else trade.open_price
            self._close_trade(trade, exit_price, reason)
            closed.append(trade)
        self._open = []
        return closed

    # ---- helpers -------------------------------------------------------------

    def _calc_pnl(self, trade: BacktestTrade, exit_price: float) -> float:
        sp = _SPEC.get(trade.symbol, _DFLT_SPEC)
        if sp["tick_size"] == 0:
            return 0.0
        delta = (exit_price - trade.open_price
                 if trade.direction == "buy"
                 else trade.open_price - exit_price)
        return round((delta / sp["tick_size"]) * sp["tick_value"] * trade.lots, 2)

    def _close_trade(self, trade: BacktestTrade, exit_price: float,
                     reason: str, close_time: Optional[datetime] = None) -> None:
        pnl = self._calc_pnl(trade, exit_price)
        trade.close_price = exit_price
        trade.close_time = close_time or self.current_time
        trade.pnl = pnl
        trade.close_reason = reason
        self._balance += pnl
        self._closed.append(trade)
        log.info("BT CLOSE  %s %s @ %.5f  P&L=%.2f  [%s]",
                 trade.direction, trade.symbol, exit_price, pnl, reason)

    def _unrealised(self, trade: BacktestTrade) -> float:
        info = self.get_symbol_info(trade.symbol)
        if info is None:
            return 0.0
        exit_price = info.bid if trade.direction == "buy" else info.ask
        return self._calc_pnl(trade, exit_price)

    def _calc_equity(self) -> float:
        return self._balance + sum(self._unrealised(t) for t in self._open)


# ── SQLite result store ────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bt_runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    date_from       TEXT,
    date_to         TEXT,
    symbols         TEXT,
    interval_days   REAL,
    initial_balance REAL,
    final_balance   REAL,
    total_pnl       REAL,
    trades_opened   INTEGER DEFAULT 0,
    trades_closed   INTEGER DEFAULT 0,
    wins            INTEGER DEFAULT 0,
    losses          INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    max_drawdown    REAL DEFAULT 0.0,
    finished_at     TEXT
);
CREATE TABLE IF NOT EXISTS bt_decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    ts            TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    proposed      TEXT,
    final         TEXT,
    direction     TEXT,
    confidence    REAL,
    setup         TEXT,
    reason        TEXT,
    sl_pips       REAL,
    tp_pips       REAL,
    lots          REAL,
    entry_price   REAL,
    sl_price      REAL,
    tp_price      REAL,
    guardrail     TEXT,
    error         TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    turns         INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bt_trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    ticket       INTEGER,
    symbol       TEXT NOT NULL,
    direction    TEXT NOT NULL,
    lots         REAL NOT NULL,
    open_price   REAL NOT NULL,
    sl           REAL,
    tp           REAL,
    open_time    TEXT,
    close_price  REAL,
    close_time   TEXT,
    pnl          REAL,
    close_reason TEXT
);
CREATE TABLE IF NOT EXISTS bt_equity (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL,
    ts      TEXT NOT NULL,
    balance REAL NOT NULL,
    equity  REAL NOT NULL
);
"""


class BacktestStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def start_run(self, date_from: datetime, date_to: datetime,
                  symbols: list[str], interval_days: float,
                  initial_balance: float) -> str:
        import uuid
        run_id = (datetime.now(tz=timezone.utc).strftime("BT%Y%m%dT%H%M%S")
                  + "_" + uuid.uuid4().hex[:6])
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO bt_runs (run_id, started_at, date_from, date_to, "
                "symbols, interval_days, initial_balance) VALUES (?,?,?,?,?,?,?)",
                (run_id,
                 datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                 date_from.isoformat(), date_to.isoformat(),
                 json.dumps(symbols), interval_days, initial_balance),
            )
        return run_id

    def record_decision(self, run_id: str, ts: datetime, symbol: str,
                        result: TradeResult, final: TradeDecision,
                        guard_notes: list[str], error: Optional[str],
                        lots: float, entry_price: float,
                        sl_price: float, tp_price: float) -> None:
        d = result.decision
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO bt_decisions "
                "(run_id,ts,symbol,proposed,final,direction,confidence,setup,reason,"
                "sl_pips,tp_pips,lots,entry_price,sl_price,tp_price,guardrail,error,"
                "input_tokens,output_tokens,turns) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, ts.isoformat(), symbol,
                 d.action, final.action, final.direction,
                 d.confidence, d.setup, d.reason,
                 final.sl_pips, final.tp_pips,
                 lots, entry_price, sl_price, tp_price,
                 json.dumps(guard_notes), error,
                 result.input_tokens, result.output_tokens, result.turns),
            )

    def record_trade(self, run_id: str, trade: BacktestTrade) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO bt_trades "
                "(run_id,ticket,symbol,direction,lots,open_price,sl,tp,"
                "open_time,close_price,close_time,pnl,close_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, trade.ticket, trade.symbol, trade.direction,
                 trade.lots, trade.open_price, trade.sl, trade.tp,
                 trade.open_time.isoformat() if trade.open_time else None,
                 trade.close_price,
                 trade.close_time.isoformat() if trade.close_time else None,
                 trade.pnl, trade.close_reason),
            )

    def record_equity(self, run_id: str, ts: datetime,
                      balance: float, equity: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO bt_equity (run_id,ts,balance,equity) VALUES (?,?,?,?)",
                (run_id, ts.isoformat(), round(balance, 2), round(equity, 2)),
            )

    def finish_run(self, run_id: str, broker: BacktestBroker,
                   errors: int) -> None:
        closed = broker._closed
        wins = sum(1 for t in closed if t.pnl > 0)
        losses = sum(1 for t in closed if t.pnl <= 0)
        total_pnl = round(sum(t.pnl for t in closed), 2)
        with self._conn() as conn:
            eq = [r["equity"] for r in conn.execute(
                "SELECT equity FROM bt_equity WHERE run_id=? ORDER BY id",
                (run_id,)).fetchall()]
        conn_closed = len(closed)
        conn_opened = conn_closed + len(broker._open)
        with self._conn() as conn:
            conn.execute(
                "UPDATE bt_runs SET "
                "final_balance=?,total_pnl=?,trades_opened=?,trades_closed=?,"
                "wins=?,losses=?,errors=?,max_drawdown=?,finished_at=? "
                "WHERE run_id=?",
                (round(broker._balance, 2), total_pnl,
                 conn_opened, conn_closed,
                 wins, losses, errors,
                 round(_max_drawdown(eq), 2),
                 datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                 run_id),
            )

    def summary(self, run_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM bt_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return dict(row) if row else {}

    def trades(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM bt_trades WHERE run_id=? ORDER BY open_time",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def decisions(self, run_id: str, symbol: Optional[str] = None) -> list[dict]:
        with self._conn() as conn:
            if symbol:
                rows = conn.execute(
                    "SELECT * FROM bt_decisions WHERE run_id=? AND symbol=? ORDER BY ts",
                    (run_id, symbol),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bt_decisions WHERE run_id=? ORDER BY ts",
                    (run_id,),
                ).fetchall()
        return [dict(r) for r in rows]

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT run_id,date_from,date_to,symbols,initial_balance,"
                "trades_closed,wins,losses,total_pnl,max_drawdown,errors,finished_at "
                "FROM bt_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def _max_drawdown(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    peak, max_dd = equity_curve[0], 0.0
    for e in equity_curve:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ── Backtest engine ────────────────────────────────────────────────────────────

class BacktestEngine:
    """Orchestrates the full historical backtest."""

    def __init__(self, settings: Settings,
                 db_path: Optional[Path] = None) -> None:
        self.s = settings
        db = db_path or settings.state_db.parent / "backtest.sqlite"
        self._db = BacktestStore(db)

    def run(
        self,
        date_from: datetime,
        date_to: datetime,
        symbols: Optional[list[str]] = None,
        interval_days: float = 7.0,
        initial_balance: float = 10000.0,
    ) -> str:
        """Execute the backtest. Returns run_id."""
        symbols = symbols or list(self.s.symbols)

        # ── 1. Load historical data via MT5 ────────────────────────────────
        mt5 = MT5Broker()
        mt5.connect(self.s.mt5_login, self.s.mt5_password, self.s.mt5_server)
        try:
            store = HistoricalDataStore()
            # 60-day warm-up so indicators (SMA-50, etc.) are ready at date_from
            load_from = date_from - timedelta(days=60)
            log.info("Loading historical data  %s → %s", load_from.date(), date_to.date())
            store.load(mt5, symbols, load_from, date_to)
        finally:
            mt5.disconnect()

        # ── 2. Build agent using BacktestBroker ────────────────────────────
        broker = BacktestBroker(store, initial_balance=initial_balance)
        prompt = render_prompt(self.s.prompt_file.read_text(encoding="utf-8"), self.s)
        from agentcore.sdk_backend import build_client
        client = build_client(self.s)
        def _history(limit: int = 20) -> list[dict]:
            trades = broker._closed[-limit:]
            return [
                {
                    "symbol": t.symbol, "direction": t.direction,
                    "lots": t.lots, "open_price": t.open_price,
                    "close_price": t.close_price, "pnl": t.pnl,
                    "close_reason": t.close_reason,
                    "open_time": t.open_time.isoformat() if t.open_time else None,
                }
                for t in reversed(trades)
            ]

        toolbox = TradingToolbox(broker, history_fn=_history)  # type: ignore[arg-type]
        agent = MarketAgent(
            client=client,
            model=self.s.model,
            system_prompt=prompt,
            toolbox=toolbox,
            max_turns=self.s.max_agent_turns,
            max_tokens=self.s.max_tokens,
        )
        risk_mgr = RiskManager(client=client, model=self.s.model)

        run_id = self._db.start_run(date_from, date_to, symbols,
                                    interval_days, initial_balance)
        timestamps = _decision_timestamps(date_from, date_to, interval_days)
        errors = 0
        prev_ts: Optional[datetime] = None

        log.info("Backtest %s: %d symbols  %d timestamps  interval=%.0fd",
                 run_id, len(symbols), len(timestamps), interval_days)

        # ── 3. Walk timestamps ─────────────────────────────────────────────
        for ts in timestamps:
            if self.s.kill_switch_file.exists():
                log.warning("Kill switch active — stopping backtest at %s", ts)
                break

            # Simulate SL/TP hits between the previous and current decision
            if prev_ts is not None:
                hit = broker.simulate_sl_tp(prev_ts, ts)
                for trade in hit:
                    self._db.record_trade(run_id, trade)
                if hit:
                    log.info("  %d hit SL/TP before %s", len(hit), ts.date())

            broker.set_time(ts)
            account = broker.get_account()
            all_positions = broker.get_positions()
            daily_pnl = sum(
                t.pnl for t in broker._closed
                if t.close_time and t.close_time.date() == ts.date()
            )

            log.info("── %s  balance=%.2f  equity=%.2f  open=%d ──",
                     ts.strftime("%Y-%m-%d %H:%M"),
                     broker._balance, account.equity, len(broker._open))
            self._db.record_equity(run_id, ts, broker._balance, account.equity)

            # ── Phase 1: Analyst ──────────────────────────────────────────────
            proposals_ts: list[tuple[str, TradeResult, Any, Any]] = []
            sim_positions = list(all_positions)
            _pending_ticket = 90000
            total_open_risk = open_risk_pct(
                all_positions, account.balance if account else 0.0,
                broker.get_symbol_info, self.s.max_risk_pct,
            )

            for symbol in symbols:
                if self.s.kill_switch_file.exists():
                    break

                log.info("  %s ...", symbol)

                try:
                    result = agent.decide(symbol, account, all_positions)
                except AgentError as exc:
                    log.error("  %s: agent error: %s", symbol, exc)
                    errors += 1
                    self._db.record_decision(
                        run_id=run_id, ts=ts, symbol=symbol,
                        result=TradeResult(
                            decision=TradeDecision.fallback(str(exc))),
                        final=TradeDecision.fallback(str(exc)),
                        guard_notes=[], error=str(exc)[:500],
                        lots=0, entry_price=0, sl_price=0, tp_price=0,
                    )
                    continue

                symbol_info = broker.get_symbol_info(symbol)
                guard = apply_guardrails(
                    decision=result.decision,
                    account=account,
                    positions=sim_positions,
                    symbol_info=symbol_info,
                    max_risk_pct=self.s.max_risk_pct,
                    max_positions=self.s.max_positions,
                    daily_loss_limit_pct=self.s.daily_loss_limit_pct,
                    spread_max_pips=self.s.spread_max_pips,
                    confidence_threshold=self.s.confidence_threshold,
                    daily_pnl=daily_pnl,
                    open_risk_pct=total_open_risk,
                    max_total_risk_pct=self.s.max_total_risk_pct,
                    opens_this_scan=len(sim_positions) - len(all_positions),
                    max_new_opens=self.s.max_new_opens_per_scan,
                    max_same_direction=self.s.max_same_direction,
                    atr_price=(atr_for(broker, symbol, result.decision.stop_timeframe or "H1")
                               if result.decision.action == OPEN else None),
                    min_stop_atr=self.s.min_stop_atr,
                    analysed_timeframes=frozenset(result.evidence.timeframes_used),
                )
                proposals_ts.append((symbol, result, guard, symbol_info))

                if guard.final.action == OPEN and not guard.blocked:
                    total_open_risk += guard.final.risk_pct
                    sim_positions.append(Position(
                        ticket=_pending_ticket, symbol=symbol,
                        direction=guard.final.direction, lots=0.01,
                        open_price=0.0, sl=0.0, tp=0.0, profit=0.0,
                        open_time=ts,
                    ))
                    _pending_ticket += 1

            # ── Phase 2: Risk Manager ─────────────────────────────────────────
            rm_verdicts: dict[str, Any] = {}
            open_props = [g.final for _, _, g, _ in proposals_ts
                          if g.final.action == OPEN and not g.blocked]
            if open_props:
                rm_verdicts = risk_mgr.review(open_props, account, all_positions)

            # ── Phase 3: Execute ──────────────────────────────────────────────
            for symbol, result, guard, symbol_info in proposals_ts:
                decision = result.decision
                final = guard.final

                if guard.blocked:
                    log.info("  %s blocked → %s: %s",
                             symbol, decision.action, "; ".join(guard.notes))

                if final.action == OPEN and symbol in rm_verdicts:
                    verdict = rm_verdicts[symbol]
                    final = verdict.apply(final)
                    if final.action == WAIT:
                        log.info("  %s RM vetoed: %s", symbol, verdict.reason)

                lots = sl_price = tp_price = entry_price = 0.0
                error_str: Optional[str] = None

                if final.action == OPEN and symbol_info:
                    try:
                        lots = broker.calculate_lots(
                            symbol, broker._balance,
                            final.risk_pct, final.sl_pips)
                        pip = symbol_info.pip_size
                        if final.direction == "buy":
                            entry_price = symbol_info.ask
                            sl_price = round(entry_price - final.sl_pips * pip,
                                             symbol_info.digits)
                            tp_price = round(entry_price + final.tp_pips * pip,
                                             symbol_info.digits)
                        else:
                            entry_price = symbol_info.bid
                            sl_price = round(entry_price + final.sl_pips * pip,
                                             symbol_info.digits)
                            tp_price = round(entry_price - final.tp_pips * pip,
                                             symbol_info.digits)
                        broker.place_order(
                            symbol, final.direction, lots, sl_price, tp_price,
                            comment=f"bt:{final.setup or 'open'}")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("  open failed: %s", exc)
                        error_str = str(exc)

                elif final.action == CLOSE:
                    try:
                        broker.close_position(final.position_ticket)
                        t = next((x for x in broker._closed
                                  if x.ticket == final.position_ticket), None)
                        if t:
                            self._db.record_trade(run_id, t)
                    except Exception as exc:  # noqa: BLE001
                        error_str = str(exc)

                elif final.action == MODIFY:
                    pos = next((p for p in all_positions
                                if p.ticket == final.position_ticket), None)
                    if pos and symbol_info:
                        pip = symbol_info.pip_size
                        if pos.direction == "buy":
                            new_sl = pos.open_price - final.sl_pips * pip
                            new_tp = pos.open_price + final.tp_pips * pip
                        else:
                            new_sl = pos.open_price + final.sl_pips * pip
                            new_tp = pos.open_price - final.tp_pips * pip
                        broker.modify_position(final.position_ticket,
                                               round(new_sl, symbol_info.digits),
                                               round(new_tp, symbol_info.digits))

                self._db.record_decision(
                    run_id=run_id, ts=ts, symbol=symbol,
                    result=result, final=final,
                    guard_notes=guard.notes, error=error_str,
                    lots=lots, entry_price=entry_price,
                    sl_price=sl_price, tp_price=tp_price,
                )
                log.info("  %s → %s conf=%.0f%%  %s",
                         symbol, final.action,
                         (decision.confidence or 0) * 100,
                         decision.setup or "")

                all_positions = broker.get_positions()

            prev_ts = ts

        # ── 4. Close remaining positions at end-date price ─────────────────
        broker.set_time(date_to)
        for trade in broker.close_all_at_market("end"):
            self._db.record_trade(run_id, trade)

        final_acct = broker.get_account()
        self._db.record_equity(run_id, date_to, broker._balance, final_acct.equity)
        self._db.finish_run(run_id, broker, errors)

        summary = self._db.summary(run_id)
        closed = summary.get("trades_closed") or 0
        wins = summary.get("wins") or 0
        win_rate = f"{wins}/{closed} ({wins/closed:.0%})" if closed else "0/0"
        log.info("Backtest done  %s", run_id)
        log.info("  P&L=%.2f  Trades=%s  MaxDD=%.2f  Errors=%d",
                 summary.get("total_pnl", 0), win_rate,
                 summary.get("max_drawdown", 0), errors)
        return run_id


# ── Helpers ────────────────────────────────────────────────────────────────────

def _decision_timestamps(date_from: datetime, date_to: datetime,
                          interval_days: float) -> list[datetime]:
    """Regular decision timestamps at `interval_days` intervals."""
    ts, delta = date_from, timedelta(days=interval_days)
    result = []
    while ts <= date_to:
        result.append(ts)
        ts += delta
    return result
