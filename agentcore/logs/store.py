"""The log store: masked events, signatures counted per minute, and the queries the agent's tools make.

SQLite for the first slice (one file, no server); the tables and queries are what a larger store would keep. Nothing
unmasked is written: ingest() masks every line before it is stored, and identifiers are kept only as keyed hashes."""
from __future__ import annotations

import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from .mask import KeyedMasker
from .parse import Record, read_records
from .profiles import Profile, scope_of
from .signature import signature, trim_detail

SCHEMA = """
CREATE TABLE IF NOT EXISTS signatures(sig TEXT PRIMARY KEY, service TEXT, level TEXT, logger TEXT, template TEXT,
  exception TEXT, frame TEXT, first_seen TEXT, last_seen TEXT, total INTEGER, example TEXT, example_detail TEXT,
  noise INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sig_notes(sig TEXT PRIMARY KEY, note TEXT, by TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS sig_buckets(sig TEXT, bucket TEXT, n INTEGER, PRIMARY KEY(sig, bucket)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS volume(service TEXT, bucket TEXT, lines INTEGER, warns INTEGER, errors INTEGER,
  PRIMARY KEY(service, bucket)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, service TEXT, ts TEXT, level TEXT, logger TEXT, sig TEXT,
  message TEXT, detail TEXT, corr TEXT);
CREATE TABLE IF NOT EXISTS event_keys(key TEXT, event_id INTEGER);
CREATE TABLE IF NOT EXISTS ingests(service TEXT, file TEXT, lines INTEGER, records INTEGER, first_ts TEXT, last_ts TEXT, at TEXT);
CREATE INDEX IF NOT EXISTS ev_service_ts ON events(service, ts);
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ek_key ON event_keys(key);
"""
BAD_LEVELS = ("ERROR", "WARN")
MAX_SEARCH_ROWS, DEFAULT_SEARCH_ROWS = 40, 25
RELATED_MINUTES = 10  # lines of the same request are looked for this far either side of the matching lines
MAX_TRAIL_EVENTS = 30  # an id on more lines than this is not one request's


def stamp(moment: Optional[datetime], end: bool = False) -> Optional[str]:
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else None


class LogStore:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- ingest --------------------------------------------------------------------------------------------
    def clear_service(self, service: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM event_keys WHERE event_id IN (SELECT id FROM events WHERE service = ?)", (service,))
            self.db.execute("DELETE FROM events WHERE service = ?", (service,))
            self.db.execute("DELETE FROM sig_buckets WHERE sig IN (SELECT sig FROM signatures WHERE service = ?)", (service,))
            self.db.execute("DELETE FROM signatures WHERE service = ?", (service,))
            self.db.execute("DELETE FROM volume WHERE service = ?", (service,))

    def ingest(self, path: Path | str, profile: Profile, masker: KeyedMasker, replace: bool = True,
               records: Optional[Iterable[Record]] = None) -> dict[str, Any]:
        """Read a log file (or the given records), mask it, and store events, signatures and counts. With replace the
        service's earlier data is dropped first (a re-ingest does not double the counts)."""
        service = profile.service
        if replace:
            self.clear_service(service)
        sigs: dict[str, dict[str, Any]] = {}
        buckets: Counter = Counter()
        vol: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        events: list[tuple] = []
        keys: list[tuple] = []
        next_id = (self.db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]) + 1
        stats = Counter()
        first = last = None

        def flush() -> None:
            nonlocal events, keys
            with self.db:
                self.db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", events)
                self.db.executemany("INSERT INTO event_keys VALUES (?,?)", keys)
            events, keys = [], []

        for rec in (records if records is not None else read_records(path, profile)):
            stats["records"] += 1
            minute = rec.ts[:16]
            v = vol[minute]
            v[0] += 1
            first, last = first or rec.ts, rec.ts
            bad = rec.level in BAD_LEVELS
            if rec.level == "WARN":
                v[1] += 1
            elif rec.level == "ERROR":
                v[2] += 1
            masked = Record(rec.ts, rec.level, rec.logger, rec.thread, masker.mask(rec.message),
                            [masker.mask(x) for x in rec.extra], masker.mask(rec.mdc))
            text = (masked.mdc + chr(10) if masked.mdc else "") + masked.text  # the bracketed ids count as part of the line
            corr = profile.correlations(text)
            hashes = masker.hashes(text)
            sig_id = None
            if bad:
                sig_id, tmpl, exc, frame = signature(service, masked, profile.app_packages)
                s = sigs.get(sig_id)
                if s is None:
                    s = sigs[sig_id] = {"level": rec.level, "logger": rec.logger.split(".")[-1], "template": tmpl,
                                       "exception": exc, "frame": frame, "total": 0, "first": rec.ts, "last": rec.ts,
                                       "example": masked.message[:600], "detail": trim_detail(masked, profile.app_packages),
                                       "noise": int(profile.is_noise(masked.message))}
                s["total"] += 1
                s["last"] = rec.ts
                buckets[(sig_id, minute)] += 1
            if bad or hashes or corr:
                detail = trim_detail(masked, profile.app_packages) if bad else "\n".join(masked.extra[:6])[:400]
                events.append((next_id, service, rec.ts, rec.level, rec.logger.split(".")[-1], sig_id,
                               masked.message[:600 if bad else 240], detail, "|".join(f"{n}:{v}" for n, v in corr)))
                for k in [f"h:{h}" for h in hashes] + [f"c:{v}" for _, v in corr]:
                    keys.append((k, next_id))
                next_id += 1
                stats["events"] += 1
                if len(events) >= 5000:
                    flush()
        flush()
        with self.db:
            for sig_id, s in sigs.items():
                self.db.execute(
                    "INSERT INTO signatures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sig) DO UPDATE SET "
                    "total = total + excluded.total, last_seen = MAX(last_seen, excluded.last_seen), "
                    "first_seen = MIN(first_seen, excluded.first_seen)",
                    (sig_id, service, s["level"], s["logger"], s["template"], s["exception"], s["frame"], s["first"],
                     s["last"], s["total"], s["example"], s["detail"], s["noise"]))
            self.db.executemany("INSERT INTO sig_buckets VALUES (?,?,?) ON CONFLICT(sig, bucket) DO UPDATE SET n = n + excluded.n",
                                [(sig, b, n) for (sig, b), n in buckets.items()])
            self.db.executemany("INSERT INTO volume VALUES (?,?,?,?,?) ON CONFLICT(service, bucket) DO UPDATE SET "
                                "lines = lines + excluded.lines, warns = warns + excluded.warns, errors = errors + excluded.errors",
                                [(service, b, *v) for b, v in vol.items()])
            self.db.execute("INSERT INTO ingests VALUES (?,?,?,?,?,?,?)",
                            (service, Path(str(path)).name, stats["records"], stats["records"], first, last,
                             datetime.now().isoformat(timespec="seconds")))
        return {"service": service, "records": stats["records"], "events": stats["events"], "signatures": len(sigs),
                "errors": sum(v[2] for v in vol.values()), "warns": sum(v[1] for v in vol.values()),
                "first": first, "last": last}

    # ---- notes -----------------------------------------------------------------------------------------------
    def set_note(self, sig: str, note: str, by: str = "") -> bool:
        if not self.db.execute("SELECT 1 FROM signatures WHERE sig = ?", (sig,)).fetchone():
            return False
        with self.db:
            self.db.execute("INSERT INTO sig_notes VALUES (?,?,?,?) ON CONFLICT(sig) DO UPDATE SET note = excluded.note, "
                            "by = excluded.by, at = excluded.at",
                            (sig, note.strip()[:600], by[:60], datetime.now().isoformat(timespec="seconds")))
        return True

    # ---- what the store holds ---------------------------------------------------------------------------------
    def services(self, since: Optional[str] = None, until: Optional[str] = None) -> list[dict[str, Any]]:
        where, args = ["1 = 1"], []
        if since:
            where.append("bucket >= ?")
            args.append(since[:16])
        if until:
            where.append("bucket < ?")
            args.append(until[:16])
        rows = self.db.execute(f"SELECT service, SUM(lines) lines, SUM(warns) warns, SUM(errors) errors, MIN(bucket) first, "
                               f"MAX(bucket) last FROM volume WHERE {' AND '.join(where)} GROUP BY service ORDER BY service", args)
        return [dict(r) for r in rows]

    def span(self) -> tuple[Optional[str], Optional[str]]:
        r = self.db.execute("SELECT MIN(bucket), MAX(bucket) FROM volume").fetchone()
        return r[0], r[1]

    # ---- log_errors ---------------------------------------------------------------------------------------------
    def errors(self, service: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None,
               mode: str = "top", levels: Iterable[str] = BAD_LEVELS, limit: int = 12) -> dict[str, Any]:
        lo, hi = self.span()
        if lo is None:
            return {"note": "No logs have been ingested yet.", "signatures": [], "services": []}
        since_b, until_b = (since or lo)[:16], (until or "9999-12-31 00:00")[:16]
        levels = [lv for lv in levels if lv in BAD_LEVELS] or list(BAD_LEVELS)
        args: list[Any] = [since_b, until_b, *levels]
        extra = ""
        if service:
            extra, args = " AND s.service = ?", args + [service]
        rows = self.db.execute(
            "SELECT s.sig, s.service, s.level, s.logger, s.template, s.exception, s.frame, s.example, s.example_detail, "
            "s.noise, s.first_seen, n.note, SUM(b.n) AS n, MIN(b.bucket) AS first_in, MAX(b.bucket) AS last_in "
            "FROM sig_buckets b JOIN signatures s ON s.sig = b.sig LEFT JOIN sig_notes n ON n.sig = s.sig "
            f"WHERE b.bucket >= ? AND b.bucket < ? AND s.level IN ({','.join('?' * len(levels))}){extra} "
            "GROUP BY s.sig ORDER BY SUM(b.n) DESC", args).fetchall()
        out = [dict(r) for r in rows]
        note = None
        if mode == "new":
            out = [r for r in out if (r["first_seen"] or "") >= since_b]
            note = (f"New relative to what has been ingested (from {lo}); with little history everything is new. "
                    "A signature first seen inside the window is shown.")
        elif mode == "spikes":
            out, clusters = self._spikes(out, service, since_b, until_b)
            if clusters:
                note = "clusters: spikes in more than one service within ten minutes of each other may share a cause"
        shown = [self._row(r) for r in out[:limit]]
        result: dict[str, Any] = {"mode": mode, "window": {"from": since_b, "to": until_b if until_b[0] != "9" else hi},
                                  "signatures": shown, "signatures_in_window": len(out),
                                  "services": self.services(since_b, until_b)}
        if mode == "spikes" and out:
            result["clusters"] = clusters
        if note:
            result["note"] = note
        if len(out) > limit:
            result["more"] = f"{len(out) - limit} more signatures: narrow the service, window or level"
        return result

    @staticmethod
    def _row(r: dict[str, Any]) -> dict[str, Any]:
        row = {"signature": r["sig"], "service": r["service"], "level": r["level"], "logger": r["logger"],
               "what": r["template"], "count": r["n"], "first_in_window": r["first_in"], "last_in_window": r["last_in"]}
        for key in ("exception", "frame"):
            if r.get(key):
                row[key] = r[key]
        if r.get("note"):
            row["known_cause"] = r["note"]
        if r.get("noise"):
            row["known_noise"] = True
        if r.get("example"):
            row["example"] = r["example"][:300]
        if r.get("example_detail"):
            row["example_detail"] = r["example_detail"][:400]
        for key in ("peak_at", "peak_count", "baseline_per_5min"):
            if key in r:
                row[key] = r[key]
        return row

    def _spikes(self, rows: list[dict[str, Any]], service: Optional[str], since_b: str, until_b: str):
        """Per signature, 5-minute bins over the window: a spike is a bin far above the signature's usual level."""
        spikes: list[dict[str, Any]] = []
        for r in rows[:80]:
            span = self.db.execute("SELECT MIN(bucket), MAX(bucket) FROM volume WHERE service = ? AND bucket >= ? AND bucket < ?",
                                   (r["service"], since_b, until_b)).fetchone()
            if not span[0]:
                continue
            start = datetime.strptime(span[0], "%Y-%m-%d %H:%M")
            end = datetime.strptime(span[1], "%Y-%m-%d %H:%M")
            nbins = max(1, int((end - start).total_seconds() // 300) + 1)
            bins = [0] * nbins
            for b, n in self.db.execute("SELECT bucket, n FROM sig_buckets WHERE sig = ? AND bucket >= ? AND bucket < ?",
                                        (r["sig"], since_b, until_b)):
                idx = int((datetime.strptime(b, "%Y-%m-%d %H:%M") - start).total_seconds() // 300)
                bins[min(max(idx, 0), nbins - 1)] += n
            base = statistics.median(bins)
            peak = max(bins)
            if peak >= max(8, 5 * max(base, 1)):
                at = (start + timedelta(minutes=5 * bins.index(peak))).strftime("%Y-%m-%d %H:%M")
                spikes.append({**r, "peak_at": at, "peak_count": peak, "baseline_per_5min": base,
                               "_ratio": peak / max(base, 1)})
        spikes.sort(key=lambda r: -r["_ratio"])
        clusters: list[dict[str, Any]] = []
        group: list[dict[str, Any]] = []
        for r in sorted(spikes, key=lambda r: r["peak_at"]):
            if group and (datetime.strptime(r["peak_at"], "%Y-%m-%d %H:%M")
                          - datetime.strptime(group[-1]["peak_at"], "%Y-%m-%d %H:%M")) > timedelta(minutes=10):
                clusters += self._cluster(group)
                group = []
            group.append(r)
        clusters += self._cluster(group)
        return spikes, clusters

    @staticmethod
    def _cluster(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
        services = sorted({g["service"] for g in group})
        if len(services) < 2:
            return []
        return [{"around": group[0]["peak_at"], "services": services, "signatures": [g["sig"] for g in group][:8]}]

    # ---- log_search ----------------------------------------------------------------------------------------------
    def search(self, hashes: Iterable[str] = (), correlation: Optional[str] = None, service: Optional[str] = None,
               since: Optional[str] = None, until: Optional[str] = None, levels: Optional[Iterable[str]] = None,
               limit: int = DEFAULT_SEARCH_ROWS, related: bool = True) -> dict[str, Any]:
        """Events matching identifier hashes (or one correlation id), and the other lines of the same requests."""
        limit = max(1, min(int(limit or DEFAULT_SEARCH_ROWS), MAX_SEARCH_ROWS))
        keys = [f"h:{h}" for h in hashes] + ([f"c:{correlation}"] if correlation else [])
        if not keys:
            return {"events": [], "note": "Nothing to search for."}
        # Found by the identifier (or correlation id) inside the time window only; the service and level filters apply to the
        # whole trail afterwards, so an ERROR on a line that does not itself mention the client is still found.
        cond, args = ["e.id IN (SELECT event_id FROM event_keys WHERE key IN (%s))" % ",".join("?" * len(keys))], list(keys)
        for clause, value in (("e.ts >= ?", since), ("e.ts < ?", until)):
            if value:
                cond.append(clause)
                args.append(value)
        direct = self.db.execute(f"SELECT e.* FROM events e WHERE {' AND '.join(cond)} ORDER BY e.ts, e.id", args).fetchall()
        rows = {e["id"]: (dict(e), "identifier" if not correlation else "correlation id") for e in direct}
        if related and direct:
            corr = []  # only request-scoped ids join lines into a trail: a session or customer uuid covers many requests
            for e in direct:
                for pair in (e["corr"] or "").split("|"):
                    name, _, value = pair.partition(":")
                    if value and scope_of(name) == "request":
                        corr.append(value)
            # an id that sits on dozens of lines (a caller's uuid reused across a whole session, whatever it is called) is
            # too broad to define one request: it stays searchable directly, but does not join lines into a trail
            corr = [v for v in list(dict.fromkeys(corr))[:40]
                    if self.db.execute("SELECT COUNT(*) FROM event_keys WHERE key = ?", (f"c:{v}",)).fetchone()[0]
                    <= MAX_TRAIL_EVENTS][:20]
            if corr:
                lo = (datetime.strptime(direct[0]["ts"], "%Y-%m-%d %H:%M:%S") - timedelta(minutes=RELATED_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
                hi = (datetime.strptime(direct[-1]["ts"], "%Y-%m-%d %H:%M:%S") + timedelta(minutes=RELATED_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
                for e in self.db.execute(
                        "SELECT e.* FROM events e WHERE e.id IN (SELECT event_id FROM event_keys WHERE key IN (%s)) "
                        "AND e.ts >= ? AND e.ts <= ? ORDER BY e.ts, e.id LIMIT 400" % ",".join("?" * len(corr)),
                        [*[f"c:{c}" for c in corr], lo, hi]):
                    rows.setdefault(e["id"], (dict(e), "same request"))
        wanted_levels = {x for x in (levels or []) if x in ("DEBUG", "INFO", "WARN", "ERROR")}
        ordered = sorted((r for r in rows.values()
                          if (not service or r[0]["service"] == service)
                          and (not wanted_levels or r[0]["level"] in wanted_levels)),
                         key=lambda x: (x[0]["ts"], x[0]["id"]))
        counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        direct_shown = [r for r in ordered if r[1] != "same request"]
        for e, _ in direct_shown:
            counts[e["service"]][e["level"]] += 1
        notes = {r[0]: r[1] for r in self.db.execute("SELECT sig, note FROM sig_notes")}
        if len(ordered) > limit:  # too many to show: the warnings and errors first, then the rest in time order
            bad_rows = [r for r in ordered if r[0]["level"] in BAD_LEVELS][:limit]
            chosen = {id(r) for r in bad_rows}
            rest = [r for r in ordered if id(r) not in chosen][:limit - len(bad_rows)]
            chosen |= {id(r) for r in rest}
            picked = [r for r in ordered if id(r) in chosen]
        else:
            picked = ordered
        shown = []
        for e, via in picked:
            item = {"service": e["service"], "ts": e["ts"], "level": e["level"], "logger": e["logger"],
                    "message": e["message"], "via": via}
            if e["detail"] and e["level"] in BAD_LEVELS:
                item["detail"] = e["detail"][:500]
            elif e["detail"]:
                item["detail"] = e["detail"][:200]
            if e["sig"]:
                item["signature"] = e["sig"]
                if notes.get(e["sig"]):
                    item["known_cause"] = notes[e["sig"]]
            if e["corr"]:
                item["correlation"] = [p.replace(":", "=", 1) for p in e["corr"].split("|")][:4]
            shown.append(item)
        out: dict[str, Any] = {"events": shown, "events_shown": len(shown), "matched_directly": len(direct_shown),
                               "by_service": {s: dict(c) for s, c in counts.items()}, "more_rows": len(ordered) > limit}
        if not direct:
            out["note"] = "Nothing in the ingested logs for this identifier and window."
        elif not ordered:
            out["note"] = "The identifier is in the logs, but nothing there matches the service or levels asked for."
        elif len(ordered) > limit:
            out["note"] = (f"More lines exist: showing the warnings and errors first, then the earliest others ({limit} in all, "
                           "in time order). Narrow the window, service or levels.")
        return out
