"""Structured chart reads: the model's map of a chart, stored as data and scored against later price.

The model fills `chart_read` in on every submit_trade, trade or not. A read says what it sees:
a bias, the levels it marks (liquidity, fair value gaps, order blocks, major levels), a sweep it
claims has happened, and an entry idea with invalidation and target. Nothing here tells it which
strategy to use. Scoring (below) uses only later candles, never a model call, and every check is
compared with a placebo built from the same read so luck and drift are not mistaken for skill.

Checks, fixed before any result (see TODO.md):
  level   touched within 24 H1 bars? on first touch, held (no close beyond it within 3 bars)?
          placebo: a level at the same distance on the other side of price (both sides averaged)
  sweep   did price trade through the level and close back inside within 3 bars (a fact check)
  bias    did the close 12 bars on agree? expected hit rate is the symbol's own base rate
  entry   price reached the entry within 24 bars, then target before invalidation within 48 bars
          (stop first when both are in one bar; one spread paid). Placebo: random market entries on
          the same symbol with the same direction, stop distance and target distance.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .playbooks.base import atr_series

LEVEL_TYPES = ("liquidity_high", "liquidity_low", "fvg", "order_block", "major_level")
BIASES = ("up", "down", "range", "unclear")

TOUCH_BARS = 24
HOLD_BARS = 3
BIAS_BARS = 12
RANGE_ATR = 1.0
REACH_BARS = 24
TRADE_BARS = 48
MIN_AFTER = REACH_BARS + TRADE_BARS     # a read is scored only when this many bars exist after it
SWEEP_LOOKBACK = 24
PLACEBO_ENTRIES = 60
MIN_N = 30
T_ALL = 2.0
T_OOS = 1.65


# ---- the tool schema and lenient parsing ------------------------------------------------------

CHART_READ_SCHEMA: dict = {
    "type": "object",
    "description": (
        "Your map of this chart, from what you actually see, whether or not you trade. Be honest: "
        "bias 'unclear' with no levels is a valid read. Every level and idea is scored later against "
        "what price did, so mark only what you believe."
    ),
    "properties": {
        "bias": {"type": "string", "enum": list(BIASES), "description": "Where price is more likely to be over the next ~12 hours."},
        "timeframe": {"type": "string", "enum": ["H1", "H4", "D1"], "description": "The timeframe this read is mainly from."},
        "levels": {
            "type": "array",
            "description": "Levels you would mark on the chart (at most 8).",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(LEVEL_TYPES)},
                    "price": {"type": "number", "description": "The price. For an fvg or order_block, the lower edge."},
                    "price_to": {"type": "number", "description": "fvg / order_block only: the upper edge."},
                    "note": {"type": "string"},
                },
                "required": ["type", "price"],
            },
        },
        "sweep": {
            "type": "object",
            "description": "A liquidity sweep you believe has just happened (omit if none).",
            "properties": {
                "level_price": {"type": "number"},
                "direction": {"type": "string", "enum": ["up", "down"], "description": "up = highs swept, down = lows swept."},
                "bars_ago": {"type": "integer", "description": "H1 bars ago the sweep happened."},
            },
            "required": ["level_price", "direction"],
        },
        "entry_idea": {
            "type": "object",
            "description": "The trade you would take on this chart, even if you pass on it (omit if none).",
            "properties": {
                "direction": {"type": "string", "enum": ["buy", "sell"]},
                "price": {"type": "number", "description": "The price you want to be filled at."},
                "invalidation": {"type": "number", "description": "Where the idea is wrong (the stop)."},
                "target": {"type": "number"},
            },
            "required": ["direction", "price", "invalidation", "target"],
        },
    },
    "required": ["bias"],
}


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def clean_read(raw: Any) -> Optional[dict]:
    """Keep what is valid in a model-supplied read and drop the rest. Never raises: a bad read must
    not block a trade decision. Returns None when nothing usable is left."""
    if not isinstance(raw, dict):
        return None
    bias = str(raw.get("bias", "")).lower()
    out: dict = {"bias": bias if bias in BIASES else "unclear"}
    tf = str(raw.get("timeframe", "")).upper()
    if tf in ("H1", "H4", "D1"):
        out["timeframe"] = tf
    levels = []
    for lv in (raw.get("levels") or [])[:8] if isinstance(raw.get("levels"), list) else []:
        if not isinstance(lv, dict) or lv.get("type") not in LEVEL_TYPES:
            continue
        p = _num(lv.get("price"))
        if p is None:
            continue
        item = {"type": lv["type"], "price": p, "note": str(lv.get("note", ""))[:200]}
        p2 = _num(lv.get("price_to"))
        if p2 is not None and lv["type"] in ("fvg", "order_block"):
            item["price"], item["price_to"] = min(p, p2), max(p, p2)
        levels.append(item)
    if levels:
        out["levels"] = levels
    sw = raw.get("sweep")
    if isinstance(sw, dict) and sw.get("direction") in ("up", "down") and _num(sw.get("level_price")) is not None:
        try:
            ago = max(0, min(int(sw.get("bars_ago", 0)), SWEEP_LOOKBACK))
        except (TypeError, ValueError):
            ago = 0
        out["sweep"] = {"level_price": _num(sw["level_price"]), "direction": sw["direction"], "bars_ago": ago}
    ei = raw.get("entry_idea")
    if isinstance(ei, dict) and ei.get("direction") in ("buy", "sell"):
        p, inv, tgt = _num(ei.get("price")), _num(ei.get("invalidation")), _num(ei.get("target"))
        if None not in (p, inv, tgt):
            ok = (inv < p < tgt) if ei["direction"] == "buy" else (tgt < p < inv)
            if ok:
                out["entry_idea"] = {"direction": ei["direction"], "price": p, "invalidation": inv, "target": tgt}
    if out["bias"] == "unclear" and len(out) == 1:
        return out          # an honest "nothing clear" is still a read
    return out


# ---- storage ---------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chart_reads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT,
    mode       TEXT,
    symbol     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    action     TEXT,
    read_json  TEXT NOT NULL,
    scored_at  TEXT,
    score_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_chart_reads_symbol ON chart_reads (symbol, created_at);
"""


class ReadStore:
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

    def record(self, run_id: str, mode: str, symbol: str, read: dict, action: str = "",
               created_at: Optional[datetime] = None) -> int:
        when = (created_at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO chart_reads (run_id, mode, symbol, created_at, action, read_json) VALUES (?,?,?,?,?,?)",
                (run_id, mode, symbol, when, action, json.dumps(read)),
            )
            return int(cur.lastrowid)

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM chart_reads").fetchone()[0]

    def unscored(self, older_than: timedelta, now: Optional[datetime] = None) -> list[dict]:
        cutoff = ((now or datetime.now(timezone.utc)) - older_than).isoformat(timespec="seconds")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM chart_reads WHERE score_json IS NULL AND created_at <= ? ORDER BY created_at", (cutoff,)
            ).fetchall()
        return [dict(r) for r in rows]

    def save_score(self, read_id: int, score: dict) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE chart_reads SET scored_at=?, score_json=? WHERE id=?",
                         (datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(score), read_id))

    def scored(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM chart_reads WHERE score_json IS NOT NULL ORDER BY created_at").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["read"] = json.loads(d.pop("read_json"))
            d["score"] = json.loads(d.pop("score_json"))
            out.append(d)
        return out


# ---- scoring ---------------------------------------------------------------------------------


def base_rates(candles: list, atr: list) -> dict:
    """The symbol's own odds, so a read is judged against what chance alone would give."""
    ups = flat = n = 0
    for i in range(20, len(candles) - BIAS_BARS):
        if atr[i] is None or atr[i] <= 0:
            continue
        move = (candles[i + BIAS_BARS].close - candles[i].close) / atr[i]
        n += 1
        ups += move > 0
        flat += abs(move) < RANGE_ATR
    return {"up": ups / n if n else 0.5, "range": flat / n if n else 0.5, "n": n}


def _zone(level: dict) -> tuple[float, float]:
    return level["price"], level.get("price_to", level["price"])


def _touch_and_hold(bars: list, lo: float, hi: float, side: int) -> tuple[bool, Optional[bool]]:
    """Does price reach the zone within TOUCH_BARS, and if so, does it hold? `side` is +1 when the
    level is below the reference price (approached from above), -1 when above."""
    for k, b in enumerate(bars[:TOUCH_BARS]):
        if b.low <= hi and b.high >= lo:
            window = bars[k:k + HOLD_BARS]
            broken = any((x.close < lo) if side > 0 else (x.close > hi) for x in window)
            return True, not broken
    return False, None


def score_level(level: dict, ref: float, bars: list) -> Optional[dict]:
    lo, hi = _zone(level)
    if lo <= ref <= hi:
        return None                                  # price is already inside it: nothing to approach
    side = 1 if ref > hi else -1
    touched, held = _touch_and_hold(bars, lo, hi, side)
    gap = (ref - hi) if side > 0 else (lo - ref)
    width = hi - lo
    # Placebo: the mirror image of the level, the same distance from price on the other side.
    if side > 0:
        plo = ref + gap
        phi = plo + width
    else:
        phi = ref - gap
        plo = phi - width
    p_touched, p_held = _touch_and_hold(bars, plo, phi, -side)
    return {"type": level["type"], "touched": touched, "held": held, "placebo_touch": 1.0 if p_touched else 0.0,
            "placebo_touched_n": 1 if p_touched else 0, "placebo_held_n": 1 if p_held else 0}


def score_sweep(sweep: dict, candles: list, start: int) -> dict:
    """Fact check of the claim: price traded through the level and closed back inside within HOLD_BARS."""
    lvl, up = sweep["level_price"], sweep["direction"] == "up"
    first = max(0, start - 1 - sweep.get("bars_ago", 0) - 2)
    for j in range(first, start):
        through = candles[j].high > lvl if up else candles[j].low < lvl
        if not through:
            continue
        for m in range(j, min(j + HOLD_BARS + 1, start)):
            if (candles[m].close < lvl) if up else (candles[m].close > lvl):
                return {"verified": True}
    return {"verified": False}


def score_bias(bias: str, ref: float, atr: float, bars: list, base: dict) -> Optional[dict]:
    if bias == "unclear" or len(bars) < BIAS_BARS or atr <= 0:
        return None
    move = (bars[BIAS_BARS - 1].close - ref) / atr
    if bias == "up":
        hit, expected = move > 0, base["up"]
    elif bias == "down":
        hit, expected = move < 0, 1 - base["up"]
    else:
        hit, expected = abs(move) < RANGE_ATR, base["range"]
    return {"bias": bias, "hit": bool(hit), "expected": expected, "move_atr": round(move, 3)}


def run_trade(bars: list, first: int, last: int, direction: int, entry: float, stop: float, target: float,
              cost: float, target_from: int) -> float:
    """R of one trade. Stop wins a tie inside a bar. The target is ignored before bar `target_from`."""
    risk = abs(entry - stop)
    end = min(last, len(bars) - 1)
    for j in range(first, end + 1):
        b = bars[j]
        if (b.low <= stop) if direction > 0 else (b.high >= stop):
            return -1.0 - cost / risk
        if j >= target_from and ((b.high >= target) if direction > 0 else (b.low <= target)):
            return abs(target - entry) / risk - cost / risk
    return direction * (bars[end].close - entry) / risk - cost / risk


def score_entry(idea: dict, bars: list, spread: float, history: list, seed: int) -> Optional[dict]:
    d = 1 if idea["direction"] == "buy" else -1
    entry, stop, target = idea["price"], idea["invalidation"], idea["target"]
    risk, reward = abs(entry - stop), abs(target - entry)
    if risk <= 0:
        return None
    reached = next((k for k, b in enumerate(bars[:REACH_BARS]) if b.low <= entry <= b.high), None)
    if reached is None:
        return {"reached": False}
    r = run_trade(bars, reached, reached + TRADE_BARS, d, entry, stop, target, spread, target_from=reached + 1)
    rng = random.Random(seed)
    starts = list(range(100, len(history) - TRADE_BARS - 1))
    if len(starts) < 10:
        return {"reached": True, "r": round(r, 3), "placebo_r": None}
    picks = rng.sample(starts, min(PLACEBO_ENTRIES, len(starts)))
    pl = []
    for j in picks:
        e = history[j].open
        pl.append(run_trade(history, j, j + TRADE_BARS, d, e, e - d * risk, e + d * reward, spread, target_from=j))
    return {"reached": True, "r": round(r, 3), "placebo_r": round(statistics.fmean(pl), 3)}


def score_read(read: dict, candles: list, created_at: datetime, spread: float, base: dict, seed: int = 0) -> dict:
    """Score one read against H1 candles. Only bars at or after `created_at` are the future."""
    start = next((i for i, c in enumerate(candles) if c.time >= created_at), None)
    if start is None or start < 30:
        return {"ok": False, "reason": "no candles at the read time"}
    if len(candles) - start < MIN_AFTER:
        return {"ok": False, "reason": "not enough later candles yet"}
    atr = atr_series([c.high for c in candles], [c.low for c in candles], [c.close for c in candles])
    a = atr[start - 1]
    if a is None or a <= 0:
        return {"ok": False, "reason": "no ATR"}
    ref, bars = candles[start - 1].close, candles[start:]
    out: dict = {"ok": True, "levels": [], "sweep": None, "bias": None, "entry": None, "atr": a}
    for lv in read.get("levels", []):
        s = score_level(lv, ref, bars)
        if s is not None:
            out["levels"].append(s)
    if read.get("sweep"):
        out["sweep"] = score_sweep(read["sweep"], candles, start)
    out["bias"] = score_bias(read.get("bias", "unclear"), ref, a, bars, base)
    if read.get("entry_idea"):
        out["entry"] = score_entry(read["entry_idea"], bars, spread, candles[:start], seed)
    return out


# ---- the report ------------------------------------------------------------------------------


def _t(vals: list[float]) -> Optional[float]:
    if len(vals) < 2:
        return None
    sd = statistics.stdev(vals)
    return statistics.fmean(vals) / (sd / math.sqrt(len(vals))) if sd > 0 else None


def _oos(rows: list, key) -> list:
    """Values from the last 40% of reads by time."""
    cut = int(len(rows) * 0.6)
    return [v for v in (key(r) for r in rows[cut:]) if v is not None]


def _verdict(n: int, mean: float, t: Optional[float], t_oos: Optional[float]) -> str:
    ok = n >= MIN_N and mean > 0 and t is not None and t >= T_ALL and t_oos is not None and t_oos >= T_OOS
    return "SKILL (beats placebo)" if ok else "not shown"


def _fmt(v: Optional[float], spec: str = "+.2f") -> str:
    return "n/a" if v is None else format(v, spec)


def report(rows: list[dict]) -> str:
    """Summarise scored reads (from ReadStore.scored(), each with 'symbol', 'read', 'score')."""
    ok = [r for r in rows if r["score"].get("ok")]
    lines = [f"Scored reads: {len(ok)} of {len(rows)} (the rest could not be scored)."]
    if not ok:
        return "\n".join(lines)

    # levels, by type: touch rate vs placebo, hold rate vs placebo
    lines.append("\nLevels (touched within 24 H1 bars; held = no close beyond it within 3 bars of first touch):")
    lines.append(f"  {'type':<16}{'n':>5}{'touch':>8}{'placebo':>9}{'diff t':>8}{'held':>7}{'plac.':>7}   verdict")
    for ty in LEVEL_TYPES:
        items = [(r, lv) for r in ok for lv in r["score"]["levels"] if lv["type"] == ty]
        if not items:
            continue
        diffs = [(1.0 if lv["touched"] else 0.0) - lv["placebo_touch"] for _, lv in items]
        diffs_oos = _oos(items, lambda it: (1.0 if it[1]["touched"] else 0.0) - it[1]["placebo_touch"])
        held = [lv["held"] for _, lv in items if lv["touched"]]
        ph_n = sum(lv["placebo_touched_n"] for _, lv in items)
        ph = sum(lv["placebo_held_n"] for _, lv in items) / ph_n if ph_n else None
        mean_d = statistics.fmean(diffs)
        lines.append(
            f"  {ty:<16}{len(items):>5}{statistics.fmean(1.0 if lv['touched'] else 0.0 for _, lv in items):>8.0%}"
            f"{statistics.fmean(lv['placebo_touch'] for _, lv in items):>9.0%}{_fmt(_t(diffs)):>8}"
            f"{(sum(held) / len(held) if held else float('nan')):>7.0%}{_fmt(ph, '.0%'):>7}   "
            f"touch: {_verdict(len(items), mean_d, _t(diffs), _t(diffs_oos))}")

    sweeps = [r["score"]["sweep"]["verified"] for r in ok if r["score"].get("sweep")]
    lines.append(f"\nClaimed sweeps: {len(sweeps)}; the sweep really happened in {sum(sweeps)} ({_fmt(sum(sweeps) / len(sweeps) if sweeps else None, '.0%')}).")

    for bias in ("up", "down", "range"):
        rs = [r for r in ok if r["score"].get("bias") and r["score"]["bias"]["bias"] == bias]
        if not rs:
            continue
        ex = [(1.0 if r["score"]["bias"]["hit"] else 0.0) - r["score"]["bias"]["expected"] for r in rs]
        ex_oos = _oos(rs, lambda r: (1.0 if r["score"]["bias"]["hit"] else 0.0) - r["score"]["bias"]["expected"])
        hit = statistics.fmean(1.0 if r["score"]["bias"]["hit"] else 0.0 for r in rs)
        lines.append(f"Bias {bias:<5} n={len(rs):>3}  hit {hit:.0%} vs expected "
                     f"{statistics.fmean(r['score']['bias']['expected'] for r in rs):.0%}  excess t {_fmt(_t(ex))}  "
                     f"{_verdict(len(rs), statistics.fmean(ex), _t(ex), _t(ex_oos))}")

    ideas = [r for r in ok if r["score"].get("entry")]
    reached = [r for r in ideas if r["score"]["entry"].get("reached") and r["score"]["entry"].get("placebo_r") is not None]
    if ideas:
        lines.append(f"\nEntry ideas: {len(ideas)}; price reached the entry in {len(reached)} "
                     f"({len(reached) / len(ideas):.0%}).")
    if reached:
        diff = [r["score"]["entry"]["r"] - r["score"]["entry"]["placebo_r"] for r in reached]
        diff_oos = _oos(reached, lambda r: r["score"]["entry"]["r"] - r["score"]["entry"]["placebo_r"])
        lines.append(f"  avg R of reached ideas {statistics.fmean(r['score']['entry']['r'] for r in reached):+.2f}  "
                     f"placebo {statistics.fmean(r['score']['entry']['placebo_r'] for r in reached):+.2f}  "
                     f"diff {statistics.fmean(diff):+.2f} (t {_fmt(_t(diff))}, last 40%: t {_fmt(_t(diff_oos))})  "
                     f"{_verdict(len(reached), statistics.fmean(diff), _t(diff), _t(diff_oos))}")
    lines.append(f"\nA read type counts as skill only with n >= {MIN_N}, a positive difference over placebo, "
                 f"t >= {T_ALL:g}, and t >= {T_OOS:g} on the last 40% of reads.")
    return "\n".join(lines)
