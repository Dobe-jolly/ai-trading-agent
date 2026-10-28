"""Paper trading engine — simulates order execution without touching real funds.

Trades are stored in SQLite. Each open trade has a ticket number, entry details,
and SL/TP prices. Closing a trade records the exit price and P&L.

This module is used when TRADING_MODE=paper (the default). In live mode the
MT5Broker.place_order / close_position methods are called instead.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .broker.metatrader import ClosedTrade

log = logging.getLogger(__name__)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket      INTEGER UNIQUE NOT NULL,
    symbol      TEXT NOT NULL,
    direction   TEXT NOT NULL,
    lots        REAL NOT NULL,
    open_price  REAL NOT NULL,
    sl          REAL NOT NULL,
    tp          REAL NOT NULL,
    open_time   TEXT NOT NULL,
    close_price REAL,
    close_time  TEXT,
    profit      REAL,
    status      TEXT NOT NULL DEFAULT 'open',   -- open | closed
    comment     TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS paper_daily (
    day         TEXT PRIMARY KEY,
    realised_pnl REAL NOT NULL DEFAULT 0.0,
    trades_closed INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class PaperPosition:
    ticket: int
    symbol: str
    direction: str
    lots: float
    open_price: float
    sl: float
    tp: float
    open_time: datetime
    comment: str = ""

    def as_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "direction": self.direction,
            "lots": self.lots,
            "open_price": self.open_price,
            "sl": self.sl,
            "tp": self.tp,
            "open_time": self.open_time.strftime("%Y-%m-%d %H:%M"),
            "profit": None,   # computed on demand from current price
        }


class PaperEngine:
    """SQLite-backed paper trading engine."""

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

    # ---- next ticket number -----------------------------------------------------

    def _next_ticket(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT MAX(ticket) AS m FROM paper_trades").fetchone()
        return (row["m"] or 10000000) + 1

    # ---- order execution --------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        comment: str = "paper",
    ) -> int:
        """Open a simulated trade. Returns the ticket number."""
        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        with self._conn() as conn:
            ticket = self._next_ticket(conn)
            conn.execute(
                "INSERT INTO paper_trades (ticket, symbol, direction, lots, open_price, sl, tp, open_time, comment) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ticket, symbol, direction, lots, entry_price, sl_price, tp_price, now, comment),
            )
        log.info("PAPER %s %s %.2f lots @ %.5f (ticket=%d, SL=%.5f, TP=%.5f)",
                 direction, symbol, lots, entry_price, ticket, sl_price, tp_price)
        return ticket

    def get_closed_trade(self, ticket: int) -> Optional[ClosedTrade]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT close_price, close_time, profit, open_price, open_time FROM paper_trades "
                "WHERE ticket=? AND status='closed'", (ticket,)
            ).fetchone()
        if row is None:
            return None
        return ClosedTrade(ticket, float(row["profit"] or 0.0), float(row["close_price"] or 0.0),
                           datetime.fromisoformat(row["close_time"]),
                           open_price=float(row["open_price"] or 0.0),
                           opened_at=datetime.fromisoformat(row["open_time"]))

    def close_position(self, ticket: int, exit_price: float) -> Optional[float]:
        """Close a paper trade at exit_price. Returns realised P&L or None if not found."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE ticket=? AND status='open'", (ticket,)
            ).fetchone()
            if row is None:
                log.warning("PAPER close: ticket %d not found or already closed", ticket)
                return None
            direction = row["direction"]
            lots = row["lots"]
            open_price = row["open_price"]
            symbol = row["symbol"]
            # Simplified P&L: (exit - entry) * lots * contract_size
            # Without live symbol info we approximate: 1 lot = 100 000 units, pip = 0.0001
            # For gold (XAUUSD) 1 lot = 100 oz
            # This is approximate — the live broker gives exact values
            if direction == "buy":
                pnl = (exit_price - open_price) * lots * 100000
            else:
                pnl = (open_price - exit_price) * lots * 100000
            now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
            day = now[:10]
            conn.execute(
                "UPDATE paper_trades SET status='closed', close_price=?, close_time=?, profit=? WHERE ticket=?",
                (exit_price, now, round(pnl, 2), ticket),
            )
            conn.execute(
                "INSERT INTO paper_daily (day, realised_pnl, trades_closed) VALUES (?, ?, 1) "
                "ON CONFLICT(day) DO UPDATE SET realised_pnl=realised_pnl+excluded.realised_pnl, "
                "trades_closed=trades_closed+1",
                (day, round(pnl, 2)),
            )
        log.info("PAPER closed ticket=%d %s @ %.5f, P&L=%.2f", ticket, symbol, exit_price, pnl)
        return round(pnl, 2)

    def modify_position(self, ticket: int, sl_price: float, tp_price: float) -> bool:
        with self._conn() as conn:
            rows = conn.execute(
                "UPDATE paper_trades SET sl=?, tp=? WHERE ticket=? AND status='open'",
                (sl_price, tp_price, ticket),
            ).rowcount
        if rows == 0:
            log.warning("PAPER modify: ticket %d not found", ticket)
            return False
        log.info("PAPER modified ticket=%d SL=%.5f TP=%.5f", ticket, sl_price, tp_price)
        return True

    # ---- queries ----------------------------------------------------------------

    def get_positions(self) -> list[PaperPosition]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE status='open' ORDER BY id"
            ).fetchall()
        result = []
        for r in rows:
            result.append(PaperPosition(
                ticket=r["ticket"],
                symbol=r["symbol"],
                direction=r["direction"],
                lots=r["lots"],
                open_price=r["open_price"],
                sl=r["sl"],
                tp=r["tp"],
                open_time=datetime.fromisoformat(r["open_time"]),
                comment=r["comment"] or "",
            ))
        return result

    def get_daily_pnl(self, day: Optional[str] = None) -> float:
        if day is None:
            day = datetime.now(tz=timezone.utc).date().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT realised_pnl FROM paper_daily WHERE day=?", (day,)
            ).fetchone()
        return float(row["realised_pnl"]) if row else 0.0

    def get_history(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE status='closed' ORDER BY close_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def summary(self) -> dict:
        with self._conn() as conn:
            open_count = conn.execute(
                "SELECT COUNT(*) AS n FROM paper_trades WHERE status='open'"
            ).fetchone()["n"]
            total = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(profit),0) AS pnl FROM paper_trades WHERE status='closed'"
            ).fetchone()
            wins = conn.execute(
                "SELECT COUNT(*) AS n FROM paper_trades WHERE status='closed' AND profit>0"
            ).fetchone()["n"]
        closed = total["n"] or 0
        win_rate = round(wins / closed * 100, 1) if closed else 0.0
        return {
            "open_positions": open_count,
            "trades_closed": closed,
            "total_pnl": round(float(total["pnl"]), 2),
            "win_rate_pct": win_rate,
            "today_pnl": self.get_daily_pnl(),
        }
