"""The console for every desk: routing to each desk's API, the desk list, pausing every desk, health and HTTP."""
from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer

import pytest

from agentcore.audit import AuditLog
from agentcore.portal.server import HttpError, make_handler
from agentcore.portal.users import UserStore
from ecocash.config import Settings as EcoSettings
from nmbz.console import Console
from sms_alerts.config import Settings as SmsSettings


@pytest.fixture
def console(tmp_path):
    users = UserStore(tmp_path / "users.json")
    users.set("dobe", "correct horse battery", "admin")
    users.set("viewer1", "another long password", "viewer")
    sms = replace(SmsSettings.from_env(env_file=tmp_path / "none.env"), state_db=tmp_path / "sms" / "agent.sqlite",
                  kill_switch_file=tmp_path / "sms" / "STOP", mode_override_file=tmp_path / "sms" / "HOLD",
                  portal_users_file=tmp_path / "users.json")
    eco = replace(EcoSettings.from_env(env_file=tmp_path / "none.env"), state_db=tmp_path / "eco" / "agent.sqlite",
                  kill_switch_file=tmp_path / "eco" / "STOP", mode_override_file=tmp_path / "eco" / "HOLD",
                  portal_users_file=tmp_path / "users.json")
    return Console({"sms_alerts": sms, "ecocash": eco}, users, stop_file=tmp_path / "STOP")


def test_service_lists_each_desk_with_its_wording(console):
    svc = console.get("/api/service", {}, "dobe", "admin")
    assert [d["key"] for d in svc["desks"]] == ["sms_alerts", "ecocash"]
    eco = svc["desks"][1]
    assert eco["title"] == "EcoCash" and eco["vocab"]["chart"]["key"] == "outcomes_by_day"
    assert "already_linked" in eco["vocab"]["status"] and svc["paused"] is False


def test_desk_paths_reach_that_desks_log_only(console):
    AuditLog(console.portals["ecocash"].s.state_db).record(
        run_id="r1", mode="shadow", ticket_id=1, ticket_number="100001", last_entry_id=1, last_message_id=1,
        proposed_action="escalate", final_action="escalate", confidence=0.9, category="x", reason="y")
    assert console.get("/api/d/ecocash/decisions", {}, "dobe", "admin")["total"] == 1
    assert console.get("/api/d/sms_alerts/decisions", {}, "dobe", "admin")["total"] == 0
    assert "outcomes_by_day" in console.get("/api/d/ecocash/overview", {}, "dobe", "admin")
    with pytest.raises(HttpError) as err:
        console.get("/api/d/payroll/overview", {}, "dobe", "admin")
    assert err.value.status == 404
    text, name = console.export("/api/d/ecocash/export/decisions.csv", {})
    assert name == "ecocash-decisions.csv" and "100001" in text


def test_pausing_every_desk_is_for_admins_and_logged_on_each_desk(console):
    with pytest.raises(HttpError) as err:
        console.post("/api/service/controls", {"pause": True}, "viewer1", "viewer")
    assert err.value.status == 403
    svc = console.post("/api/service/controls", {"pause": True, "note": "core banking maintenance"}, "dobe", "admin")
    assert svc["paused"] and console.stop_file.exists()
    for key in ("sms_alerts", "ecocash"):
        events = console.get(f"/api/d/{key}/controls", {}, "dobe", "admin")["events"]
        assert events[0]["action"] == "pause every desk" and events[0]["detail"] == "core banking maintenance"
    status, body = console.healthz()
    assert status == 200 and body["paused"] and set(body["desks"]) == {"sms_alerts", "ecocash"}
    assert not console.post("/api/service/controls", {"pause": False}, "dobe", "admin")["paused"]


def test_over_http_with_one_sign_in(console):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(console))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    auth = "Basic " + base64.b64encode(b"dobe:correct horse battery").decode()

    def get(path, **headers):
        req = urllib.request.Request(base + path, headers={"Authorization": auth, **headers})
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, res.read(), res.headers

    try:
        status, body, _ = get("/api/service")
        assert status == 200 and len(json.loads(body)["desks"]) == 2
        status, body, _ = get("/")
        assert b'meta name="portal" content="served"' in body and b'id="desks"' in body
        _, _, headers = get("/api/d/sms_alerts/export/decisions.csv")
        assert "sms_alerts-decisions.csv" in headers["Content-Disposition"]
        req = urllib.request.Request(base + "/api/d/ecocash/controls", data=b'{"kill_switch": true}', method="POST",
                                     headers={"Authorization": auth, "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as err:  # no CSRF header
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
