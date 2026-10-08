"""The console web server both agents share: a JSON API over an agent's log plus its single-page console.
Each agent subclasses Portal with its name, its read models (data module) and its page.

Standard library only (ThreadingHTTPServer). Bind it to localhost or an internal interface
and put TLS in front (F5 or nginx); Basic auth credentials must not cross the network in clear.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from types import ModuleType

from ..approval import ApprovalStore, summary as approval_summary, token_map
from ..audit import AuditLog
from ..controls import effective_mode, kill_switch_on, read_mode_override, set_kill_switch, set_mode_override
from ..guardrails import reply_problems
from ..notify import OUTBOX_SCHEMA
from ..pii import Pseudonymizer
from . import data as core_data
from .users import UserStore

log = logging.getLogger(__name__)
SERVED_MARKER = "<!--PORTAL-SERVED-->"
STATIC = Path(__file__).resolve().parent / "static" / "index.html"  # the one console page, for every desk
MAX_BODY = 64 * 1024
AUTH_CACHE_SECONDS = 300
LOCKOUT_FAILURES = 10
LOCKOUT_SECONDS = 300
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' "
        "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Portal:
    """Request handling, independent of the socket server (so it can be tested directly)."""

    realm = "Agent"                            # the sign-in prompt's name
    server_version = "agent-portal"
    csv_name = "agent-decisions.csv"
    data: ModuleType = core_data               # the agent's read models
    static: Path = STATIC                      # the console page

    def __init__(self, settings: Any, users: Optional[UserStore] = None) -> None:
        self.s = settings
        self.users = users or UserStore(settings.portal_users_file)
        audit = AuditLog(settings.state_db)  # make sure the schema exists
        audit.db.executescript(OUTBOX_SCHEMA)
        audit.close()
        self._auth_cache: dict[str, tuple[str, float]] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # ---- auth ---------------------------------------------------------------------
    def authenticate(self, header: Optional[str], client: str) -> tuple[str, str]:
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if time.time() - t < LOCKOUT_SECONDS]
            self._failures[client] = recent
            if len(recent) >= LOCKOUT_FAILURES:
                raise HttpError(429, "Too many failed sign-ins. Try again in a few minutes.")
        if not header or not header.startswith("Basic "):
            raise HttpError(401, "Sign in required.")
        try:
            name, password = base64.b64decode(header[6:]).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeDecodeError):
            raise HttpError(401, "Sign in required.") from None
        key = hashlib.sha256(f"{name}\0{password}".encode()).hexdigest()
        with self._lock:
            cached = self._auth_cache.get(key)
            if cached and time.time() - cached[1] < AUTH_CACHE_SECONDS and name in self.users.users():
                return name, self.users.users()[name]
        role = self.users.authenticate(name, password)
        if role is None:
            with self._lock:
                self._failures.setdefault(client, []).append(time.time())
            time.sleep(0.5)
            raise HttpError(401, "Wrong user name or password.")
        with self._lock:
            self._auth_cache[key] = (role, time.time())
        return name, role

    # ---- helpers --------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.s.state_db, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def controls(self, conn: sqlite3.Connection, role: str) -> dict[str, Any]:
        override = read_mode_override(self.s.mode_override_file)
        return {
            "kill_switch": kill_switch_on(self.s.kill_switch_file),
            "mode_override": override,
            "configured_mode": self.s.mode,
            "effective_mode": effective_mode(self.s.mode, override),
            "can_edit": role == "admin",
            "events": self.data.control_events(conn),
        }

    def session(self, user: str, role: str) -> dict[str, Any]:
        override = read_mode_override(self.s.mode_override_file)
        conn = self.connect()
        try:
            anomaly = self.data.latest_anomaly(conn)
        finally:
            conn.close()
        conn = self.connect()
        try:
            waiting = self.data.pending_approvals(conn)
        finally:
            conn.close()
        return {
            "anomaly": anomaly,
            "approvals_pending": waiting,
            "approval_mode": self.s.approval_mode,
            "user": user, "role": role, "demo": False,
            "configured_mode": self.s.mode,
            "effective_mode": effective_mode(self.s.mode, override),
            "kill_switch": kill_switch_on(self.s.kill_switch_file),
            "model": self.s.model,
            "reply_staff_id": self.s.reply_staff_id,
            "osticket_ticket_url": self.s.osticket_ticket_url,
            "stale_after_minutes": self.s.portal_stale_minutes,
            "notify_transport": self.s.notify_transport,
        }

    # ---- routing ------------------------------------------------------------------------
    def healthz(self) -> tuple[int, dict[str, Any]]:
        conn = self.connect()
        try:
            h = self.data.health(conn, self.s.portal_stale_minutes)
        finally:
            conn.close()
        paused = kill_switch_on(self.s.kill_switch_file)
        run = h["last_run"] or {}
        ok = not h["stale"] or paused
        body = {"ok": ok, "paused": paused, "minutes_since_last_run": h["minutes_since_last_run"],
                "last_run_errors": run.get("errors"), "effective_mode": effective_mode(
                    self.s.mode, read_mode_override(self.s.mode_override_file))}
        return (200 if ok else 503), body

    def get(self, path: str, params: dict[str, str], user: str, role: str) -> Any:
        conn = self.connect()
        try:
            if path == "/api/session":
                return self.session(user, role)
            if path == "/api/overview":
                return self.data.overview(conn, _days(params, 14), self.s.portal_stale_minutes,
                                     self.s.price_input_per_mtok, self.s.price_output_per_mtok)
            if path == "/api/decisions":
                return self.data.decisions(conn, params)
            match = re.fullmatch(r"/api/decisions/(\d+)", path)
            if match:
                detail = self.data.decision_detail(conn, int(match.group(1)))
                if detail is None:
                    raise HttpError(404, "No decision with that id.")
                return detail
            if path == "/api/queue":
                return self.data.queue(conn)
            if path == "/api/runs":
                return self.data.runs(conn, int(params.get("limit") or 60))
            if path == "/api/quality":
                return self.data.quality(conn, _days(params, 30))
            if path == "/api/controls":
                return self.controls(conn, role)
            if path == "/api/anomalies":
                return self.data.anomalies(conn)
            match = re.fullmatch(r"/api/anomalies/(\d+)", path)
            if match:
                result = self.data.anomalies(conn, int(match.group(1)))
                if result["report"] is None:
                    raise HttpError(404, "No report with that id.")
                return result
            if path == "/api/approvals":
                result = self.data.approvals(conn, _days(params, 7))
                result["can_decide"] = role == "admin"
                result["approval_mode"] = self.s.approval_mode
                return result
            if path == "/api/live":
                return self.data.live(conn)
            if path == "/api/notifications":
                result = self.data.notifications(conn, params)
                result["can_retry"] = role == "admin"
                result["transport"] = self.s.notify_transport
                return result
            raise HttpError(404, "Not found.")
        finally:
            conn.close()

    def decide_approval(self, audit: AuditLog, approval_id: int, body: dict[str, Any], user: str, role: str) -> Any:
        if role != "admin":
            raise HttpError(403, "Only admins can approve or reject replies.")
        verdict = body.get("verdict")
        if verdict not in ("approve", "reject"):
            raise HttpError(400, "verdict must be approve or reject.")
        store = ApprovalStore(audit.db)
        row = store.get(approval_id)
        if row is None:
            raise HttpError(404, "No approval with that id.")
        edited = body.get("reply")
        if verdict == "approve" and edited is not None:
            edited = str(edited).strip()
            problems = reply_problems(edited, Pseudonymizer.from_export(token_map(row)))
            if problems:
                raise HttpError(400, "The edited reply can't be posted: " + "; ".join(problems) + ".")
        else:
            edited = None
        try:
            row = store.decide(approval_id, verdict, user, body.get("comment"), edited)
        except ValueError as exc:
            raise HttpError(409, str(exc)) from None
        audit.add_control_event(user, f"{verdict} reply for #{row['ticket_number']}",
                                (("edited; " if row["edited"] else "") + (row["comment"] or ""))[:300])
        return approval_summary(row)

    def export_csv(self, params: dict[str, str]) -> str:
        conn = self.connect()
        try:
            header, rows = self.data.decisions_csv_rows(conn, params)
        finally:
            conn.close()
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(header)
        writer.writerows(rows)
        return out.getvalue()

    def post(self, path: str, body: dict[str, Any], user: str, role: str) -> Any:
        audit = AuditLog(self.s.state_db)
        try:
            match = re.fullmatch(r"/api/decisions/(\d+)/reviews", path)
            if match:
                decision_id = int(match.group(1))
                if audit.db.execute("SELECT 1 FROM decisions WHERE id = ?", (decision_id,)).fetchone() is None:
                    raise HttpError(404, "No decision with that id.")
                verdict = body.get("verdict")
                if verdict not in ("correct", "incorrect"):
                    raise HttpError(400, "verdict must be correct or incorrect.")
                expected = body.get("expected_action") or None
                if expected is not None and expected not in self.data.ACTIONS:
                    raise HttpError(400, "expected_action is not a known action.")
                comment = str(body.get("comment") or "").strip()[:2000] or None
                return audit.add_review(decision_id, user, verdict, expected, comment)
            match = re.fullmatch(r"/api/approvals/(\d+)", path)
            if match:
                return self.decide_approval(audit, int(match.group(1)), body, user, role)
            if path == "/api/notifications/retry":
                if role != "admin":
                    raise HttpError(403, "Only admins can re-send emails.")
                cursor = audit.db.execute("UPDATE notifications SET status = 'pending', attempts = 0 "
                                          "WHERE status = 'failed' AND body IS NOT NULL")
                audit.db.commit()
                if cursor.rowcount:
                    audit.add_control_event(user, "re-send failed emails", f"{cursor.rowcount} queued")
                return {"requeued": cursor.rowcount}
            if path == "/api/controls":
                if role != "admin":
                    raise HttpError(403, "Only admins can change the agent's controls.")
                if "kill_switch" in body:
                    on = bool(body["kill_switch"])
                    if on != kill_switch_on(self.s.kill_switch_file):
                        set_kill_switch(self.s.kill_switch_file, on, f"paused from the portal by {user}\n")
                        audit.add_control_event(user, "pause" if on else "resume",
                                                str(body.get("note") or "")[:300])
                if "mode_override" in body:
                    mode = body["mode_override"] or None
                    if mode not in (None, "shadow", "notes"):
                        raise HttpError(400, "The portal can only hold the agent at shadow or notes.")
                    if mode != read_mode_override(self.s.mode_override_file):
                        set_mode_override(self.s.mode_override_file, mode)
                        audit.add_control_event(user, f"hold at {mode}" if mode else "release mode hold",
                                                str(body.get("note") or "")[:300])
                return self.controls(audit.db, role)
            raise HttpError(404, "Not found.")
        finally:
            audit.close()

    def index_html(self) -> bytes:
        return page_html(self.static)

    def export(self, path: str, params: dict[str, str]) -> tuple[str, str]:
        """(csv text, file name) for the decisions export."""
        if path != "/api/export/decisions.csv":
            raise HttpError(404, "Not found.")
        return self.export_csv(params), self.csv_name


def page_html(static: Path) -> bytes:
    """The console page. Its source is a body fragment (it doubles as the preview artifact), so wrap it."""
    body = Path(static).read_text(encoding="utf-8")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '<meta name="portal" content="served"></head><body>'
            + body.replace(SERVED_MARKER, "", 1) + "</body></html>").encode("utf-8")


def _days(params: dict[str, str], default: int) -> int:
    try:
        return max(1, min(int(params.get("days") or default), 365))
    except ValueError:
        return default


def make_handler(portal: Portal) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = portal.server_version
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # route access logs through logging
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, content_type: str, extra: Optional[dict[str, str]] = None) -> None:
            self.send_response(status)
            for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(key, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, err: HttpError) -> None:
            extra = ({"WWW-Authenticate": f'Basic realm="{portal.realm}", charset="UTF-8"'}
                     if err.status == 401 else None)
            self._send(err.status, json.dumps({"error": err.message}).encode(), "application/json; charset=utf-8", extra)

        def _user(self) -> tuple[str, str]:
            return portal.authenticate(self.headers.get("Authorization"), self.client_address[0])

        def do_GET(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/healthz":
                    status, body = portal.healthz()
                    return self._json(status, body)
                user, role = self._user()
                if url.path in ("/", "/index.html"):
                    return self._send(200, portal.index_html(), "text/html; charset=utf-8")
                if url.path.endswith("/export/decisions.csv"):
                    text, name = portal.export(url.path, params)
                    return self._send(200, text.encode("utf-8"), "text/csv; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="{name}"'})
                return self._json(200, portal.get(url.path, params, user, role))
            except HttpError as err:
                self._error(err)
            except Exception:  # noqa: BLE001
                log.exception("GET %s failed", url.path)
                self._error(HttpError(500, "Something went wrong reading the agent log. Check the portal's service log."))

        do_HEAD = do_GET

        def do_POST(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            try:
                user, role = self._user()
                # CSRF: browsers cannot add this header cross-site without a CORS preflight we never allow.
                if self.headers.get("X-Portal-Request") != "1":
                    raise HttpError(403, "Missing request header.")
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                    raise HttpError(403, "Cross-site request refused.")
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise HttpError(413, "Request too large.")
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    raise HttpError(400, "Body must be JSON.") from None
                if not isinstance(body, dict):
                    raise HttpError(400, "Body must be a JSON object.")
                self._json(200, portal.post(url.path, body, user, role))
            except HttpError as err:
                self._error(err)
            except Exception:  # noqa: BLE001
                log.exception("POST %s failed", url.path)
                self._error(HttpError(500, "Something went wrong saving that. Check the portal's service log."))

    return Handler


def serve(settings: Any, portal_class: type[Portal] = Portal) -> None:
    portal = portal_class(settings)
    if not portal.users.users():
        raise SystemExit(f"No portal users yet. Create one first: python -m nmbz portal-user add <name> --role admin\n"
                         f"(users file: {settings.portal_users_file})")
    server = ThreadingHTTPServer((settings.portal_host, settings.portal_port), make_handler(portal))
    server.daemon_threads = True
    log.info("Portal on http://%s:%d (log: %s)", settings.portal_host, settings.portal_port, settings.state_db)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
