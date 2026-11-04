from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from trading.audit import TradeAudit


def seeded(tmp_path, rows):
    a = TradeAudit(tmp_path / "a.sqlite")
    for sym, r, pnl, when in rows:
        rid = a.record(run_id="r", mode="live", symbol=sym, proposed_action="open", final_action="open",
                       created_at=datetime.fromisoformat(when).replace(tzinfo=timezone.utc))
        with sqlite3.connect(tmp_path / "a.sqlite") as c:
            c.execute("UPDATE trade_decisions SET outcome=?, final_r=?, profit=? WHERE id=?",
                      ("win" if r > 0 else "loss", r, pnl, rid))
    return a


ROWS = [("EURUSD", 1.0, 10, "2026-11-01"), ("XAUUSD", -1.0, -10, "2026-11-01"), ("EURUSD", 2.0, 20, "2026-11-05"),
        ("Volatility 75 Index", -1.0, -50, "2026-11-05"), ("Crash 1000 Index", -1.0, -5, "2026-11-05")]


def test_forex_metals_cohort_excludes_synthetics(tmp_path):
    c = seeded(tmp_path, ROWS).cohort_stats("forex-metals", mode="live")
    assert (c["closed"], c["wins"], c["pnl"]) == (3, 2, 20)
    assert c["avg_r"] == pytest.approx(0.667, abs=1e-3)


def test_synthetic_cohort_has_vol_and_boom_crash(tmp_path):
    c = seeded(tmp_path, ROWS).cohort_stats("synthetic", mode="live")
    assert (c["closed"], c["wins"], c["pnl"]) == (2, 0, -55)


def test_since_filters_by_open_date(tmp_path):
    c = seeded(tmp_path, ROWS).cohort_stats("forex-metals", mode="live", since="2026-11-04")
    assert c["closed"] == 1 and c["avg_r"] == 2.0 and c["t_stat"] is None


def test_empty_cohort_and_bad_name(tmp_path):
    a = seeded(tmp_path, [])
    assert a.cohort_stats("all")["avg_r"] is None
    with pytest.raises(ValueError):
        a.cohort_stats("nope")
