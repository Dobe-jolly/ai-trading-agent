"""Trade decision log — every decision Claude makes is recorded here.

Schema mirrors agentcore/audit.py but adapted for trading:
no tickets, no replies; instead we record symbol, direction, prices, lots, and outcome.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    mode            TEXT NOT NULL,             -- paper | live
    symbol          TEXT NOT NULL,
    proposed_action TEXT NOT NULL,             -- open | close | modify | wait
    final_action    TEXT NOT NULL,
    direction       TEXT,                      -- buy | sell
    risk_pct        REAL,
    sl_pips         INTEGER,
    tp_pips         INTEGER,
    lots            REAL,
    entry_price     REAL,
    sl_price        REAL,
    tp_price        REAL,
    confidence      REAL,
    setup           TEXT,
    reason          TEXT,
    evidence        TEXT,                      -- JSON list
    guardrail_notes TEXT,                      -- JSON list
    position_ticket INTEGER,
    executed        TEXT,                      -- JSON list of steps taken
    error           TEXT,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    turns           INTEGER DEFAULT 0,
    transcript      TEXT,                      -- JSON (masked)
    -- outcome (filled when position closes)
    closed_at       TEXT,
    close_price     REAL,
    profit          REAL,
    outcome         TEXT                       -- win | loss | breakeven | open
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT,
    symbols     TEXT,
    decided     INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    actions     TEXT                           -- JSON counter
);
"""


# Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an existing table
# alone, so these are added to it (and to a fresh one) by name.
_ADDED_COLUMNS = (
    ("mfe_r", "REAL"),                      # best the trade got, in multiples of its initial risk
    ("mae_r", "REAL"),                      # worst it got
    ("final_r", "REAL"),                    # where it ended, same units
    ("excursion_tried", "INTEGER DEFAULT 0"),
    ("stop_timeframe", "TEXT"),             # H1 | H4 | D1: the timeframe the model anchored its stop to
    ("decided_at", "TEXT"),                 # when the model answered (created_at is when the order went out, up to ~2h later)
    ("decision_bid", "REAL"),               # the quote at that moment, to compare with the fill
    ("decision_ask", "REAL"),
)


class TradeAudit:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            have = {r["name"] for r in conn.execute("PRAGMA table_info(trade_decisions)")}
            for name, ddl in _ADDED_COLUMNS:
                if name not in have:
                    conn.execute(f"ALTER TABLE trade_decisions ADD COLUMN {name} {ddl}")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def start_run(self, mode: str, symbols: list[str]) -> str:
        run_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO trade_runs (run_id, started_at, mode, symbols) VALUES (?, ?, ?, ?)",
                (run_id, datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                 mode, json.dumps(symbols)),
            )
        return run_id

    def finish_run(self, run_id: str, decided: int, errors: int, actions: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE trade_runs SET finished_at=?, decided=?, errors=?, actions=? WHERE run_id=?",
                (datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                 decided, errors, json.dumps(actions), run_id),
            )

    def record(
        self,
        run_id: str,
        mode: str,
        symbol: str,
        proposed_action: str,
        final_action: str,
        direction: str = "",
        risk_pct: float = 0.0,
        sl_pips: int = 0,
        tp_pips: int = 0,
        lots: float = 0.0,
        entry_price: float = 0.0,
        sl_price: float = 0.0,
        tp_price: float = 0.0,
        confidence: float = 0.0,
        setup: str = "",
        reason: str = "",
        evidence: Optional[list] = None,
        guardrail_notes: Optional[list] = None,
        position_ticket: Optional[int] = None,
        executed: Optional[list] = None,
        error: Optional[str] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        turns: int = 0,
        transcript: Optional[Any] = None,
        created_at: Optional[datetime] = None,
        stop_timeframe: str = "",
        decided_at: Optional[datetime] = None,
        decision_bid: float = 0.0,
        decision_ask: float = 0.0,
    ) -> int:
        now = (created_at or datetime.now(tz=timezone.utc)).isoformat(timespec="seconds")
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO trade_decisions
                   (run_id, created_at, mode, symbol, proposed_action, final_action,
                    direction, risk_pct, sl_pips, tp_pips, lots, entry_price, sl_price, tp_price,
                    confidence, setup, reason, evidence, guardrail_notes, position_ticket,
                    executed, error, input_tokens, output_tokens, turns, transcript, outcome,
                    stop_timeframe, decided_at, decision_bid, decision_ask)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, now, mode, symbol, proposed_action, final_action,
                 direction, risk_pct, sl_pips, tp_pips, lots, entry_price, sl_price, tp_price,
                 confidence, setup, reason,
                 json.dumps(evidence or []),
                 json.dumps(guardrail_notes or []),
                 position_ticket,
                 json.dumps(executed or []),
                 error, input_tokens, output_tokens, turns,
                 json.dumps(transcript) if transcript else None,
                 "open" if final_action == "open" else "n/a",
                 stop_timeframe or None,
                 decided_at.isoformat(timespec="seconds") if decided_at else None,
                 decision_bid or None, decision_ask or None),
            )
            return int(cursor.lastrowid)

    def record_outcome(
        self, decision_id: int, close_price: float, profit: float,
        closed_at: Optional[datetime] = None,
    ) -> None:
        outcome = "win" if profit > 0 else ("loss" if profit < 0 else "breakeven")
        closed_at = closed_at or datetime.now(tz=timezone.utc)
        with self._conn() as conn:
            conn.execute(
                "UPDATE trade_decisions SET closed_at=?, close_price=?, profit=?, outcome=? WHERE id=?",
                (closed_at.isoformat(timespec="seconds"), close_price, profit, outcome, decision_id),
            )

    def get_meta(self, key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def known_open_tickets(self, mode: str) -> set[int]:
        """Tickets that already have an 'open' row. Close and modify rows reuse a ticket, so they don't count."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT position_ticket FROM trade_decisions "
                "WHERE mode=? AND final_action='open' AND position_ticket IS NOT NULL",
                (mode,),
            ).fetchall()
        return {int(r["position_ticket"]) for r in rows}

    def unresolved_opens(self, mode: str) -> list[dict]:
        """Opens whose result is not recorded yet and that we can look up by ticket."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, symbol, position_ticket FROM trade_decisions "
                "WHERE mode=? AND final_action='open' AND outcome='open' AND position_ticket IS NOT NULL",
                (mode,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_legacy_opens(self, mode: str) -> int:
        """Tag opens that have no ticket and no result as 'legacy', so they stop counting as pending.

        These were opened before tickets were recorded: they can never be matched to a broker result.
        The rows stay in place. Returns how many were tagged.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE trade_decisions SET outcome='legacy' "
                "WHERE mode=? AND final_action='open' AND outcome='open' AND position_ticket IS NULL",
                (mode,),
            )
            return cur.rowcount

    def closed_needing_excursion(self, mode: str) -> list[dict]:
        """Closed trades with a ticket that we have not yet tried to measure."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, symbol, direction, sl_price, position_ticket FROM trade_decisions "
                "WHERE mode=? AND final_action='open' AND outcome IN ('win','loss','breakeven') "
                "AND position_ticket IS NOT NULL AND excursion_tried=0",
                (mode,),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_excursion(
        self, decision_id: int, mfe_r: Optional[float] = None, mae_r: Optional[float] = None,
        final_r: Optional[float] = None,
    ) -> None:
        """Store the measurement, or None values when it could not be made. Either way the trade is
        marked tried so a missing price history is not re-fetched on every scan."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE trade_decisions SET mfe_r=?, mae_r=?, final_r=?, excursion_tried=1 WHERE id=?",
                (mfe_r, mae_r, final_r, decision_id),
            )

    def exit_stats(self, mode: Optional[str] = None) -> dict:
        """How closed trades used their room, in multiples of each trade's initial risk (R).

        `kept_pct` is the share of the best-ever open profit that ended up banked, over trades that
        were ever in profit. `gave_back` counts trades that were at least 1R ahead and still lost.
        """
        where = "mfe_r IS NOT NULL" + (" AND mode=?" if mode else "")
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT mfe_r, mae_r, final_r FROM trade_decisions WHERE {where}",
                (mode,) if mode else (),
            ).fetchall()
        if not rows:
            return {"measured": 0}
        mfe, mae, fin = ([r[k] for r in rows] for k in ("mfe_r", "mae_r", "final_r"))
        ahead = [(m, f) for m, f in zip(mfe, fin) if m > 0]
        sd = statistics.stdev(fin) if len(fin) >= 2 else 0.0
        return {
            "measured": len(rows),
            # t-stat of the average final R. Unlike dollar P&L it doesn't depend on how large each
            # position happened to be, so it is the better yardstick for "does the model have an edge".
            "final_r_t_stat": round(statistics.fmean(fin) / (sd / math.sqrt(len(fin))), 2) if sd > 0 else None,
            "avg_best_r": round(statistics.fmean(mfe), 2),
            "avg_worst_r": round(-statistics.fmean(mae), 2),
            "avg_final_r": round(statistics.fmean(fin), 2),
            "kept_pct": round(sum(f for _, f in ahead) / sum(m for m, _ in ahead) * 100, 1) if ahead else None,
            "ever_1r": sum(m >= 1 for m in mfe),
            "gave_back": sum(1 for m, f in zip(mfe, fin) if m >= 1 and f < 0),
        }

    COHORTS = {
        "forex-metals": ("forex", "metals"),
        "synthetic": ("vol_index", "step", "boom_crash"),
        "all": ("forex", "metals", "vol_index", "step", "boom_crash"),
    }

    def cohort_stats(self, cohort: str, mode: Optional[str] = None, since: Optional[str] = None) -> dict:
        """Result in R of closed trades for one group of asset classes, optionally only trades opened
        on/after `since` (ISO date). The group is judged on its own: pooling classes hides which one has an edge."""
        from .study import asset_class
        if cohort not in self.COHORTS:
            raise ValueError(f"cohort must be one of {', '.join(self.COHORTS)}")
        where = "outcome IN ('win','loss','breakeven') AND final_r IS NOT NULL"
        args: list = []
        if mode:
            where += " AND mode=?"
            args.append(mode)
        if since:
            where += " AND created_at >= ?"
            args.append(since)
        with self._conn() as conn:
            rows = conn.execute(f"SELECT symbol, final_r, profit FROM trade_decisions WHERE {where}", args).fetchall()
        fin = [r["final_r"] for r in rows if asset_class(r["symbol"]) in self.COHORTS[cohort]]
        pnl = sum(r["profit"] or 0 for r in rows if asset_class(r["symbol"]) in self.COHORTS[cohort])
        n = len(fin)
        sd = statistics.stdev(fin) if n >= 2 else 0.0
        return {
            "cohort": cohort, "closed": n, "wins": sum(1 for f in fin if f > 0.05),
            "avg_r": round(statistics.fmean(fin), 3) if n else None,
            "t_stat": round(statistics.fmean(fin) / (sd / math.sqrt(n)), 2) if sd > 0 else None,
            "pnl": round(pnl, 2),
        }

    def breakdown(self, by: str, mode: Optional[str] = None) -> list[dict]:
        """Closed-trade results grouped by 'symbol', 'setup' or 'stop_timeframe', best P&L first.
        Trades with no value for the column (older ones) are grouped as '(none)'."""
        if by not in ("symbol", "setup", "stop_timeframe"):
            raise ValueError("by must be 'symbol', 'setup' or 'stop_timeframe'")
        where = "outcome IN ('win','loss','breakeven') AND COALESCE(setup,'')!='adopted'" + (" AND mode=?" if mode else "")
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT COALESCE({by}, '(none)') AS key, COUNT(*) AS closed, "
                "SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins, "
                "ROUND(SUM(profit), 2) AS pnl, ROUND(AVG(profit), 2) AS avg_pnl "
                f"FROM trade_decisions WHERE {where} GROUP BY {by} ORDER BY pnl DESC",
                (mode,) if mode else (),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent(self, limit: int = 20, mode: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if mode:
                rows = conn.execute(
                    "SELECT * FROM trade_decisions WHERE mode = ? ORDER BY id DESC LIMIT ?",
                    (mode, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trade_decisions ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def runs(self, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trade_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self, mode: Optional[str] = None) -> dict:
        where = "final_action='open'" + (" AND mode=?" if mode else "")
        args = (mode,) if mode else ()
        with self._conn() as conn:
            # Positions the bot found on the account but did not open (setup 'adopted') are not its trades:
            # they are kept in the table but left out of every count and P&L figure below.
            adopted = conn.execute(
                f"SELECT COUNT(*) FROM trade_decisions WHERE {where} AND setup='adopted'", args
            ).fetchone()[0]
            where += " AND COALESCE(setup,'')!='adopted'"
            totals = conn.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(CASE WHEN outcome='open' THEN 1 ELSE 0 END) AS still_open, "
                "SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins, "
                "SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses, "
                "SUM(CASE WHEN outcome='breakeven' THEN 1 ELSE 0 END) AS flat, "
                "COALESCE(SUM(profit),0) AS total_pnl "
                f"FROM trade_decisions WHERE {where} AND outcome!='legacy'", args
            ).fetchone()
            legacy = conn.execute(
                f"SELECT COUNT(*) FROM trade_decisions WHERE {where} AND outcome='legacy'", args
            ).fetchone()[0]
            actions = conn.execute(
                "SELECT final_action, COUNT(*) AS n FROM trade_decisions GROUP BY final_action"
            ).fetchall()
            profits = [r["profit"] for r in conn.execute(
                f"SELECT profit FROM trade_decisions WHERE {where} AND outcome IN ('win','loss','breakeven')", args
            )]
        avg_pnl, pnl_t = None, None
        if profits:
            avg_pnl = statistics.fmean(profits)
            if len(profits) >= 2 and statistics.stdev(profits) > 0:
                pnl_t = avg_pnl / (statistics.stdev(profits) / math.sqrt(len(profits)))
        wins, losses, flat = totals["wins"] or 0, totals["losses"] or 0, totals["flat"] or 0
        closed = wins + losses + flat
        no_result = totals["still_open"] or 0
        return {
            "opens": totals["n"] or 0,
            "closed": closed,
            "no_result_yet": no_result,          # still open (opens with no ticket are tagged legacy, see mark_legacy_opens)
            "adopted_not_counted": adopted,      # found on the account, not opened by the bot
            "legacy_unresolvable": legacy,       # opened before tickets were recorded; excluded from the counts above
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / closed * 100, 1) if closed else None,
            "total_pnl": round(float(totals["total_pnl"]), 2),
            "avg_pnl_per_trade": round(avg_pnl, 2) if avg_pnl is not None else None,
            "pnl_t_stat": round(pnl_t, 2) if pnl_t is not None else None,   # |t| >= 2 needed before trusting an edge
            "actions": {r["final_action"]: r["n"] for r in actions},
        }
