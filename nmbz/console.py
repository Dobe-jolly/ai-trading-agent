"""The console for every desk: one page, one sign-in, one server.

Each desk keeps its own decision log and its own console API (agentcore.portal.server.Portal subclassed by
the desk). The console mounts them under /api/d/<desk>/ and adds what spans desks:

    GET  /api/service             the desks, with each one's mode, pause, approvals waiting and anomaly
    POST /api/service/controls    pause or resume every desk (admins)
    GET  /healthz                 healthy only if every desk is (for the load balancer or monitoring)

Standard library only. Bind it to localhost or an internal interface and put TLS in front.
"""
from __future__ import annotations

import logging
import re
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from agentcore.audit import AuditLog
from agentcore.controls import kill_switch_on, set_kill_switch
from agentcore.portal.server import STATIC, HttpError, make_handler, page_html
from agentcore.portal.users import UserStore

from . import desks as registry

log = logging.getLogger(__name__)
DESK_PATH = re.compile(r"/api/d/([a-z][a-z0-9_]*)(/.*)")


class Console:
    realm = "NMBZ helpdesk agents"
    server_version = "nmbz-console"
    csv_name = "decisions.csv"

    def __init__(self, desk_settings: dict[str, Any], users: Optional[UserStore] = None,
                 stop_file: Optional[Path] = None) -> None:
        """desk_settings: each enabled desk's Settings, in display order."""
        if not desk_settings:
            raise ValueError("no desk is switched on (DESKS)")
        first = next(iter(desk_settings.values()))
        self.users = users or UserStore(first.portal_users_file)
        self.desks = [registry.get(key) for key in desk_settings]
        self.portals = {d.key: d.module("portal.server").Portal(desk_settings[d.key], self.users) for d in self.desks}
        self.vocab = {d.key: getattr(d.module("portal.vocab"), "VOCAB", {}) for d in self.desks}
        self.first = self.portals[self.desks[0].key]
        self.s = first
        self.stop_file = stop_file or registry.service_stop_file()

    @classmethod
    def from_env(cls, env_file: Optional[str] = None) -> "Console":
        return cls({d.key: d.settings(env_file) for d in registry.enabled()})

    # ---- what the shared handler calls -------------------------------------------------------
    def authenticate(self, header: Optional[str], client: str) -> tuple[str, str]:
        return self.first.authenticate(header, client)  # one users file and one lockout for every desk

    def index_html(self) -> bytes:
        return page_html(STATIC)

    def healthz(self) -> tuple[int, dict[str, Any]]:
        bodies: dict[str, Any] = {}
        ok = True
        for key, portal in self.portals.items():
            status, body = portal.healthz()
            bodies[key] = body
            ok = ok and status == 200
        paused = kill_switch_on(self.stop_file)
        ok = ok or paused
        return (200 if ok else 503), {"ok": ok, "paused": paused, "desks": bodies}

    def export(self, path: str, params: dict[str, str]) -> tuple[str, str]:
        key, rest = self._split(path)
        text, _ = self.portals[key].export("/api" + rest, params)
        return text, f"{key}-decisions.csv"

    def get(self, path: str, params: dict[str, str], user: str, role: str) -> Any:
        if path == "/api/service":
            return self.service(user, role)
        key, rest = self._split(path)
        return self.portals[key].get("/api" + rest, params, user, role)

    def post(self, path: str, body: dict[str, Any], user: str, role: str) -> Any:
        if path == "/api/service/controls":
            return self.pause_all(body, user, role)
        key, rest = self._split(path)
        return self.portals[key].post("/api" + rest, body, user, role)

    # ---- spanning desks ----------------------------------------------------------------------
    def _split(self, path: str) -> tuple[str, str]:
        match = DESK_PATH.fullmatch(path)
        if not match:
            raise HttpError(404, "Not found.")
        if match.group(1) not in self.portals:
            raise HttpError(404, f"No desk called {match.group(1)!r} is switched on.")
        return match.group(1), match.group(2)

    def service(self, user: str, role: str) -> dict[str, Any]:
        desks = []
        for desk in self.desks:
            session = self.portals[desk.key].session(user, role)
            anomaly = session.get("anomaly") or {}
            desks.append({
                "key": desk.key, "title": desk.title, "blurb": desk.blurb, "vocab": self.vocab[desk.key],
                "configured_mode": session.get("configured_mode"), "effective_mode": session.get("effective_mode"),
                "kill_switch": session.get("kill_switch"), "approvals_pending": session.get("approvals_pending") or 0,
                "anomaly": {k: anomaly.get(k) for k in ("severity", "headline", "created_at")} if anomaly else None,
            })
        return {"user": user, "role": role, "paused": kill_switch_on(self.stop_file), "can_edit": role == "admin",
                "desks": desks}

    def pause_all(self, body: dict[str, Any], user: str, role: str) -> dict[str, Any]:
        if role != "admin":
            raise HttpError(403, "Only admins can pause or resume the service.")
        if "pause" not in body:
            raise HttpError(400, "Say pause: true or pause: false.")
        on = bool(body["pause"])
        note = str(body.get("note") or "")[:300]
        if on != kill_switch_on(self.stop_file):
            set_kill_switch(self.stop_file, on, f"every desk paused from the console by {user}\n")
            for portal in self.portals.values():  # each desk's control log shows it
                audit = AuditLog(portal.s.state_db)
                try:
                    audit.add_control_event(user, "pause every desk" if on else "resume every desk", note)
                finally:
                    audit.close()
        return self.service(user, role)


def serve(env_file: Optional[str] = None, host: Optional[str] = None, port: Optional[int] = None) -> int:
    console = Console.from_env(env_file)
    if not console.users.users():
        raise SystemExit("No console users yet. Create one first: python -m nmbz portal-user add <name> --role admin\n"
                         f"(users file: {console.s.portal_users_file})")
    host = host or console.s.portal_host
    port = port or console.s.portal_port
    server = ThreadingHTTPServer((host, port), make_handler(console))
    server.daemon_threads = True
    log.info("Console on http://%s:%d for %s", host, port, ", ".join(d.title for d in console.desks))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
