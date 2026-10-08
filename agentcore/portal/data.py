"""Read models for the console, computed from the agent's SQLite log (no Metabase, no model calls). Shared by
both agents; each one says how its check statuses group into the overview's by-day chart."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..normalize import LOCAL_ZONE

ACTIONS = ("reply_and_close", "request_info", "escalate", "no_action")
LOW_CONFIDENCE = 0.9
# Never the body: it holds real account and phone numbers until it is sent (then it is erased).
NOTIFICATION_COLUMNS = ("id, created_at, decision_id, ticket_id, ticket_number, kind, channel, recipient, cc, subject, "
                        "status, attempts, last_error, sent_at")
REVIEW_SQL = "(SELECT verdict FROM reviews r WHERE r.decision_id = d.id ORDER BY r.id DESC LIMIT 1)"
SUMMARY_COLUMNS = (
    "d.id, d.created_at, d.mode, d.run_id, d.ticket_id, d.ticket_number, d.proposed_action, d.final_action, "
    "d.confidence, d.category, d.account_statuses, d.guardrail_notes, d.executed, d.deferred, d.error, "
    f"{REVIEW_SQL} AS review_verdict"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def since_iso(days: int, now: Optional[datetime] = None) -> str:
    return ((now or utc_now()) - timedelta(days=days)).isoformat(timespec="seconds")


def _json(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def local_day(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(LOCAL_ZONE).date().isoformat()
    except ValueError:
        return iso[:10]


def summary(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    notes = _json(row["guardrail_notes"], [])
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "mode": row["mode"],
        "run_id": row["run_id"],
        "ticket_id": row["ticket_id"],
        "ticket_number": row["ticket_number"],
        "proposed_action": row["proposed_action"],
        "final_action": row["final_action"],
        "confidence": row["confidence"],
        "category": row["category"],
        "statuses": _json(row["account_statuses"], {}),
        "guardrail_notes": notes,
        "guardrail_changed": bool(row["proposed_action"] and row["final_action"]
                                  and row["proposed_action"] != row["final_action"]),
        "executed": _json(row["executed"], []),
        "deferred": bool(row["deferred"]),
        "error": row["error"],
        "review": row["review_verdict"] if "review_verdict" in keys else None,
    }


def _note_reason(note: str) -> str:
    """Group guardrail notes by their kind, without the ticket-specific details."""
    text = re.sub(r"\(.*?\)", "", note)
    text = re.sub(r"<[A-Z_]+_\d+>", "…", text)
    text = re.sub(r"\d+(\.\d+)?", "N", text)
    return text.split(":")[0].strip()[:90]


# ---- overview ---------------------------------------------------------------------
def last_run(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM runs WHERE mode != 'audit' ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def health(conn: sqlite3.Connection, stale_minutes: int, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or utc_now()
    run = last_run(conn)
    minutes = None
    if run:
        stamp = run.get("finished_at") or run.get("started_at")
        minutes = round((now - datetime.fromisoformat(stamp)).total_seconds() / 60, 1)
    return {
        "last_run": run,
        "minutes_since_last_run": minutes,
        "stale": minutes is None or minutes > stale_minutes,
        "stale_after_minutes": stale_minutes,
        "running": bool(run and not run.get("finished_at")),
    }


def overview(conn: sqlite3.Connection, days: int, stale_minutes: int,
             price_in: Optional[float] = None, price_out: Optional[float] = None, *,
             groups: Optional[dict[str, str]] = None, group_keys: tuple = (),
             by_day_key: str = "groups_by_day") -> dict[str, Any]:
    """groups maps a check status to a series of the by-day chart (group_keys, in order)."""
    groups = groups or {}
    since = since_iso(days)
    rows = conn.execute(
        f"SELECT {SUMMARY_COLUMNS}, d.input_tokens, d.output_tokens FROM decisions d "
        "WHERE d.created_at >= ? AND d.mode != 'audit' ORDER BY d.id DESC", (since,)).fetchall()
    decided = [r for r in rows if not r["error"] and not r["deferred"]]
    actions = Counter(r["final_action"] for r in decided)
    tokens_in = sum(r["input_tokens"] or 0 for r in rows)
    tokens_out = sum(r["output_tokens"] or 0 for r in rows)
    confidences = [r["confidence"] for r in decided if r["confidence"] is not None and r["category"] != "rules_fallback"]

    today = utc_now().astimezone(LOCAL_ZONE).date()
    day_keys = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    by_day = {d: {"day": d, **{a: 0 for a in ACTIONS}} for d in day_keys}
    by_group = {d: {"day": d, **{k: 0 for k in group_keys}} for d in day_keys}
    statuses: Counter = Counter()
    reasons: Counter = Counter()
    for r in decided:
        day = local_day(r["created_at"])
        if day in by_day and r["final_action"] in ACTIONS:
            by_day[day][r["final_action"]] += 1
        for status in _json(r["account_statuses"], {}).values():
            statuses[status] += 1
            group = groups.get(status)
            if group and day in by_group:
                by_group[day][group] += 1
        if r["proposed_action"] != r["final_action"]:
            for note in _json(r["guardrail_notes"], [])[:1]:
                reasons[_note_reason(note)] += 1

    other_topics = 0
    try:
        triaged = conn.execute("SELECT kind, input_tokens, output_tokens FROM triage WHERE created_at >= ?",
                               (since,)).fetchall()
        tokens_in += sum(r["input_tokens"] or 0 for r in triaged)
        tokens_out += sum(r["output_tokens"] or 0 for r in triaged)
        other_topics = conn.execute(
            "SELECT COUNT(DISTINCT ticket_id) FROM triage WHERE created_at >= ? AND kind IN ('sms_alerts', 'mixed')",
            (since,)).fetchone()[0]
    except sqlite3.OperationalError:  # no triage table yet (other topics never scanned)
        pass
    mail = Counter(r["status"] for r in conn.execute(
        "SELECT status FROM notifications WHERE created_at >= ? AND kind NOT LIKE 'admin_%'", (since,)).fetchall())
    cost = None
    if price_in or price_out:
        cost = round(tokens_in / 1e6 * (price_in or 0) + tokens_out / 1e6 * (price_out or 0), 2)
    return {
        "days": days,
        "health": health(conn, stale_minutes),
        "kpis": {
            "decided": len(decided),
            "closed": actions.get("reply_and_close", 0),
            "escalated": actions.get("escalate", 0),
            "request_info": actions.get("request_info", 0),
            "no_action": actions.get("no_action", 0),
            "deferred": sum(1 for r in rows if r["deferred"]),
            "errors": sum(1 for r in rows if r["error"]),
            "guardrail_changed": sum(1 for r in decided if r["proposed_action"] != r["final_action"]),
            "fallback": sum(1 for r in decided if r["category"] == "rules_fallback"),
            "other_topics": other_topics,
            "approvals_pending": pending_approvals(conn),
            "avg_confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "est_cost": cost,
            "emails_sent": mail.get("sent", 0),
            "emails_pending": mail.get("pending", 0),
            "emails_failed": mail.get("failed", 0),
            "emails_skipped": mail.get("skipped", 0),
        },
        "by_day": list(by_day.values()),
        by_day_key: list(by_group.values()),
        "top_statuses": [{"status": s, "count": c} for s, c in statuses.most_common(12)],
        "guardrail_reasons": [{"reason": s, "count": c} for s, c in reasons.most_common(8)],
        "recent": [summary(r) for r in rows[:12]],
    }


# ---- decisions -------------------------------------------------------------------------
def _filters(params: dict[str, str]) -> tuple[str, list[Any]]:
    where = ["d.created_at >= ?"]
    args: list[Any] = [since_iso(int(params.get("days") or 14))]
    if params.get("action") in ACTIONS:
        where.append("d.final_action = ?")
        args.append(params["action"])
    if params.get("mode") in ("shadow", "notes", "live", "audit"):
        where.append("d.mode = ?")
        args.append(params["mode"])
    else:
        where.append("d.mode != 'audit'")
    status = params.get("status", "")
    if re.fullmatch(r"[a-z_]{3,40}", status):
        where.append("d.account_statuses LIKE ?")
        args.append(f'%"{status}"%')
    q = (params.get("q") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9-]{1,20}", q):
        where.append("d.ticket_number LIKE ?")
        args.append(f"%{q}%")
    flag = params.get("flag")
    if flag == "guardrail":
        where.append("d.proposed_action != d.final_action")
    elif flag == "fallback":
        where.append("d.category = 'rules_fallback'")
    elif flag == "error":
        where.append("d.error IS NOT NULL")
    review = params.get("review")
    if review == "unreviewed":
        where.append(f"{REVIEW_SQL} IS NULL")
    elif review in ("correct", "incorrect"):
        where.append(f"{REVIEW_SQL} = ?")
        args.append(review)
    return " AND ".join(where), args


def decisions(conn: sqlite3.Connection, params: dict[str, str]) -> dict[str, Any]:
    where, args = _filters(params)
    limit = max(1, min(int(params.get("limit") or 50), 500))
    offset = max(0, int(params.get("offset") or 0))
    total = conn.execute(f"SELECT COUNT(*) FROM decisions d WHERE {where}", args).fetchone()[0]
    rows = conn.execute(f"SELECT {SUMMARY_COLUMNS} FROM decisions d WHERE {where} ORDER BY d.id DESC LIMIT ? OFFSET ?",
                        [*args, limit, offset]).fetchall()
    return {"total": total, "limit": limit, "offset": offset, "items": [summary(r) for r in rows]}


def decisions_csv_rows(conn: sqlite3.Connection, params: dict[str, str]) -> tuple[list[str], list[list[Any]]]:
    where, args = _filters(params)
    cols = ["id", "created_at", "mode", "ticket_number", "proposed_action", "final_action", "confidence", "category",
            "account_statuses", "guardrail_notes", "reason", "reply_text", "template_reply", "actual_reply", "executed",
            "deferred", "error", "input_tokens", "output_tokens"]
    rows = conn.execute(
        f"SELECT {', '.join('d.' + c for c in cols)}, {REVIEW_SQL} AS review FROM decisions d WHERE {where} ORDER BY d.id DESC LIMIT 20000",
        args).fetchall()
    return cols + ["review"], [list(r) for r in rows]


def decision_detail(conn: sqlite3.Connection, decision_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute(f"SELECT {SUMMARY_COLUMNS}, d.* FROM decisions d WHERE d.id = ?", (decision_id,)).fetchone()
    if row is None:
        return None
    detail = summary(row)
    detail.update({
        "reason": row["reason"],
        "evidence": _json(row["evidence"], []),
        "reply_text": row["reply_text"],
        "template_reply": row["template_reply"],
        "actual_reply": row["actual_reply"],
        "tools_used": _json(row["tools_used"], []),
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "last_entry_id": row["last_entry_id"],
        "last_message_id": row["last_message_id"],
        "transcript": _json(row["transcript"], []),
        "attachments": _json(row["attachments"], []),
        "reviews": [dict(r) for r in conn.execute(
            "SELECT * FROM reviews WHERE decision_id = ? ORDER BY id DESC", (decision_id,)).fetchall()],
        "notifications": [dict(r) for r in conn.execute(
            f"SELECT {NOTIFICATION_COLUMNS} FROM notifications WHERE decision_id = ? ORDER BY id", (decision_id,)).fetchall()],
        "history": [summary(r) for r in conn.execute(
            f"SELECT {SUMMARY_COLUMNS} FROM decisions d WHERE d.ticket_id = ? AND d.id != ? ORDER BY d.id DESC LIMIT 10",
            (row["ticket_id"], decision_id)).fetchall()],
        "approval_note": row["approval"] if "approval" in row.keys() else None,
        "approval": _approval_for(conn, decision_id),
    })
    return detail


# ---- approvals and the live view ---------------------------------------------------------
APPROVAL_COLUMNS = ("a.id, a.decision_id, a.created_at, a.ticket_id, a.ticket_number, a.action, a.why, a.reply_masked, "
                    "a.status, a.decided_by, a.decided_at, a.comment, a.edited, a.finished_at, a.result")


def _approval_for(conn: sqlite3.Connection, decision_id: int) -> Optional[dict[str, Any]]:
    try:
        row = conn.execute(f"SELECT {APPROVAL_COLUMNS} FROM approvals a WHERE a.decision_id = ?", (decision_id,)).fetchone()
    except sqlite3.OperationalError:  # no approvals table yet
        return None
    return dict(row) if row else None


def approvals(conn: sqlite3.Connection, days: int = 7) -> dict[str, Any]:
    """Replies waiting for a person, and the ones decided recently. Never the real text or token map."""
    try:
        rows = conn.execute(
            f"SELECT {APPROVAL_COLUMNS}, d.confidence, d.category, d.reason, d.account_statuses, d.mode, d.proposed_action "
            "FROM approvals a JOIN decisions d ON d.id = a.decision_id "
            "WHERE a.status IN ('pending', 'approved', 'rejected') OR a.created_at >= ? ORDER BY a.id DESC",
            (since_iso(days),)).fetchall()
    except sqlite3.OperationalError:
        return {"pending": [], "decided": [], "counts": {}}
    items = []
    for r in rows:
        item = dict(r)
        item["statuses"] = _json(item.pop("account_statuses"), {})
        items.append(item)
    return {"pending": [i for i in reversed(items) if i["status"] == "pending"],  # oldest first
            "decided": [i for i in items if i["status"] != "pending"][:60],
            "counts": dict(Counter(i["status"] for i in items))}


def pending_approvals(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def live(conn: sqlite3.Connection, now: Optional[datetime] = None) -> dict[str, Any]:
    """What the agent is doing right now. A pass that stopped updating for 15 minutes counts as not running."""
    try:
        row = conn.execute("SELECT * FROM live WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None:
        return {"running": False}
    now = now or utc_now()
    fresh = row["updated_at"] and (now - datetime.fromisoformat(row["updated_at"])).total_seconds() < 900
    return {"running": row["state"] == "running" and bool(fresh), "run_id": row["run_id"], "started_at": row["started_at"],
            "updated_at": row["updated_at"], "ticket_number": row["ticket_number"], "help_topic": row["help_topic"],
            "steps": _json(row["steps"], [])}


# ---- queue, runs, quality ---------------------------------------------------------------
def queue(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT q.*, d.final_action, d.confidence, d.account_statuses FROM queue_state q "
        "LEFT JOIN decisions d ON d.id = q.decision_id ORDER BY q.ticket_number DESC").fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["statuses"] = _json(item.pop("account_statuses"), {})
        items.append(item)
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (rows[0]["run_id"],)).fetchone() if rows else None
    return {"as_of": dict(run) if run else None, "items": items,
            "counts": dict(Counter(i["state"] for i in items))}


def runs(conn: sqlite3.Connection, limit: int = 60) -> dict[str, Any]:
    items = []
    for r in conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall():
        item = dict(r)
        try:
            item["duration_s"] = round((datetime.fromisoformat(r["finished_at"]) -
                                        datetime.fromisoformat(r["started_at"])).total_seconds(), 1)
        except (TypeError, ValueError):
            item["duration_s"] = None
        items.append(item)
    return {"items": items}


def _same(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    return re.sub(r"\s+", " ", a).strip().lower() == re.sub(r"\s+", " ", b).strip().lower()


def quality(conn: sqlite3.Connection, days: int) -> dict[str, Any]:
    since = since_iso(days)
    reviewed = conn.execute(
        f"SELECT d.final_action, {REVIEW_SQL} AS verdict FROM decisions d "
        f"WHERE d.created_at >= ? AND {REVIEW_SQL} IS NOT NULL", (since,)).fetchall()
    by_action: dict[str, Counter] = defaultdict(Counter)
    for r in reviewed:
        by_action[r["final_action"]][r["verdict"]] += 1
    correct = sum(c["correct"] for c in by_action.values())
    wrong_expected = Counter(
        (r["final_action"], r["expected_action"]) for r in conn.execute(
            "SELECT d.final_action, r.expected_action FROM reviews r JOIN decisions d ON d.id = r.decision_id "
            "WHERE r.created_at >= ? AND r.verdict = 'incorrect' AND r.expected_action IS NOT NULL", (since,)).fetchall())

    needs = conn.execute(
        f"SELECT {SUMMARY_COLUMNS} FROM decisions d WHERE d.created_at >= ? AND d.mode != 'audit' "
        f"AND d.error IS NULL AND d.deferred = 0 AND {REVIEW_SQL} IS NULL AND ("
        "d.proposed_action != d.final_action OR d.confidence < ? OR d.category = 'rules_fallback' "
        "OR d.final_action IN ('request_info', 'escalate') OR d.category = 'request_info_timeout') "
        "ORDER BY (d.proposed_action != d.final_action) DESC, d.confidence ASC, d.id DESC LIMIT 30",
        (since, LOW_CONFIDENCE)).fetchall()

    audit_rows = conn.execute(
        "SELECT final_action, reply_text, template_reply, actual_reply FROM decisions "
        "WHERE mode = 'audit' AND created_at >= ? AND error IS NULL", (since,)).fetchall()
    audit = {
        "tickets": len(audit_rows),
        "actions": dict(Counter(r["final_action"] for r in audit_rows)),
        "would_also_close": sum(1 for r in audit_rows if r["final_action"] == "reply_and_close"),
        "template_same_as_actual": sum(1 for r in audit_rows if _same(r["template_reply"], r["actual_reply"])),
        "agent_same_as_actual": sum(1 for r in audit_rows if _same(r["reply_text"], r["actual_reply"])),
    }
    return {
        "days": days,
        "reviewed": len(reviewed),
        "correct": correct,
        "incorrect": len(reviewed) - correct,
        "accuracy": round(correct / len(reviewed), 3) if reviewed else None,
        "by_action": [{"action": a, "reviewed": sum(c.values()), "correct": c["correct"]}
                      for a, c in sorted(by_action.items())],
        "corrections": [{"agent": a, "expected": e, "count": n} for (a, e), n in wrong_expected.most_common(8)],
        "needs_review": [summary(r) for r in needs],
        "audit": audit,
    }


def control_events(conn: sqlite3.Connection, limit: int = 30) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM control_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def notifications(conn: sqlite3.Connection, params: dict[str, str]) -> dict[str, Any]:
    where = ["created_at >= ?"]
    args: list[Any] = [since_iso(int(params.get("days") or 30))]
    if params.get("status") in ("pending", "sent", "failed", "skipped"):
        where.append("status = ?")
        args.append(params["status"])
    clause = " AND ".join(where)
    rows = conn.execute(f"SELECT {NOTIFICATION_COLUMNS} FROM notifications WHERE {clause} ORDER BY id DESC LIMIT 300",
                        args).fetchall()
    counts = Counter(r["status"] for r in conn.execute(
        "SELECT status FROM notifications WHERE created_at >= ?", (args[0],)).fetchall())
    retryable = conn.execute("SELECT COUNT(*) FROM notifications WHERE status = 'failed' AND body IS NOT NULL").fetchone()[0]
    return {"items": [dict(r) for r in rows], "counts": dict(counts), "retryable": retryable}


# ---- anomaly watch -----------------------------------------------------------------
def _report(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "created_at": row["created_at"], "severity": row["severity"], "headline": row["headline"],
        "alert": row["alert"], "summary": _json(row["summary"], {}), "analysis": _json(row["analysis"], {}),
        "data": _json(row["data"], {}),
    }


def latest_anomaly(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    try:
        row = conn.execute("SELECT id, created_at, severity, headline FROM anomaly_reports ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def anomalies(conn: sqlite3.Connection, report_id: Optional[int] = None) -> dict[str, Any]:
    try:
        if report_id is None:
            row = conn.execute("SELECT * FROM anomaly_reports ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT * FROM anomaly_reports WHERE id = ?", (report_id,)).fetchone()
        history = conn.execute("SELECT id, created_at, severity, incident_key, headline, alert FROM anomaly_reports "
                               "ORDER BY id DESC LIMIT 168").fetchall()
        state = conn.execute("SELECT * FROM anomaly_state WHERE id = 1").fetchone()
    except sqlite3.OperationalError:  # the watch has never run
        return {"report": None, "history": [], "state": None}
    return {"report": _report(row) if row else None, "history": [dict(h) for h in history],
            "state": dict(state) if state else None}
