"""Portal: API over the log, auth, CSRF, controls, health; plus the agent features the portal relies on."""
from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

from agentcore.audit import AuditLog
from agentcore.controls import effective_mode, read_mode_override, set_mode_override
from agent.portal import data
from agent.portal.server import HttpError, Portal, make_handler
from agentcore.portal.users import UserStore, verify_password
from tests.conftest import ScriptedModel, decision, find
from tests.test_runner import CLOSED_REPLY as CLOSE_REPLY, build

def seed(mb, phone="0771234567", kind="wallet_closed"):
    """Ticket 7 (#482913) under EcoCash, and a failed link attempt for its client."""
    mb.add_ticket(7, "482913", [f"Client {phone} cannot link the account to EcoCash"])
    mb.add_attempt(kind)



@pytest.fixture
def psettings(settings, tmp_path):
    return replace(settings, portal_users_file=tmp_path / "users.json", mode_override_file=tmp_path / "MODE_OVERRIDE",
                   mode="live")


@pytest.fixture
def portal(psettings):
    users = UserStore(psettings.portal_users_file)
    users.set("dobe", "correct horse battery", "admin")
    users.set("viewer1", "another long password", "viewer")
    return Portal(psettings, users)


def log_some(psettings, mb):
    """Run the real agent so the log holds real rows."""
    seed(mb)
    runner, audit = build(psettings, mb, ScriptedModel(find(), decision("reply_and_close", CLOSE_REPLY)), mode="shadow")
    runner.run()
    audit.close()


def test_users_hash_and_validate(tmp_path):
    store = UserStore(tmp_path / "u.json")
    store.set("dobe", "correct horse battery", "admin")
    raw = (tmp_path / "u.json").read_text()
    assert "correct horse" not in raw and "pbkdf2_sha256$" in raw
    assert store.authenticate("dobe", "correct horse battery") == "admin"
    assert store.authenticate("dobe", "wrong") is None and store.authenticate("nobody", "x") is None
    with pytest.raises(ValueError):
        store.set("Bad Name", "correct horse battery", "admin")
    with pytest.raises(ValueError):
        store.set("ok", "short", "admin")
    assert not verify_password("x", "garbage")


def test_mode_override_only_restricts(tmp_path):
    f = tmp_path / "MODE"
    assert effective_mode("live", None) == "live"
    set_mode_override(f, "shadow")
    assert read_mode_override(f) == "shadow" and effective_mode("live", "shadow") == "shadow"
    assert effective_mode("shadow", "notes") == "shadow"
    with pytest.raises(ValueError):
        set_mode_override(f, "live")
    set_mode_override(f, None)
    assert read_mode_override(f) is None


def test_api_reads_the_agent_log(portal, psettings, mb):
    log_some(psettings, mb)
    overview = portal.get("/api/overview", {"days": "7"}, "dobe", "admin")
    assert overview["kpis"]["decided"] == 1 and overview["kpis"]["closed"] == 1
    assert overview["top_statuses"] == [{"status": "wallet_closed", "count": 1}]
    assert sum(d["client"] for d in overview["outcomes_by_day"]) == 1
    assert overview["health"]["stale"] is False
    listing = portal.get("/api/decisions", {"days": "7", "status": "wallet_closed"}, "dobe", "admin")
    assert listing["total"] == 1
    detail = portal.get(f"/api/decisions/{listing['items'][0]['id']}", {}, "dobe", "admin")
    assert detail["transcript"] and "771234567" not in json.dumps(detail)
    queue = portal.get("/api/queue", {}, "dobe", "admin")
    assert queue["items"][0]["state"] == "reply_and_close" and queue["items"][0]["statuses"]
    assert portal.get("/api/runs", {}, "dobe", "admin")["items"][0]["decisions"] == 1
    with pytest.raises(HttpError):
        portal.get("/api/decisions/9999", {}, "dobe", "admin")


def test_reviews_feed_quality(portal, psettings, mb):
    log_some(psettings, mb)
    did = portal.get("/api/decisions", {}, "dobe", "admin")["items"][0]["id"]
    portal.post(f"/api/decisions/{did}/reviews", {"verdict": "incorrect", "expected_action": "escalate",
                                                   "comment": "gateway was down"}, "viewer1", "viewer")
    q = portal.get("/api/quality", {"days": "30"}, "dobe", "admin")
    assert q["reviewed"] == 1 and q["accuracy"] == 0 and q["corrections"][0]["expected"] == "escalate"
    assert portal.get("/api/decisions", {"review": "incorrect"}, "dobe", "admin")["total"] == 1
    with pytest.raises(HttpError):
        portal.post(f"/api/decisions/{did}/reviews", {"verdict": "maybe"}, "dobe", "admin")


def test_controls_admin_only_and_logged(portal, psettings):
    with pytest.raises(HttpError) as err:
        portal.post("/api/controls", {"kill_switch": True}, "viewer1", "viewer")
    assert err.value.status == 403
    c = portal.post("/api/controls", {"kill_switch": True, "note": "INC-1"}, "dobe", "admin")
    assert c["kill_switch"] and psettings.kill_switch_file.exists()
    c = portal.post("/api/controls", {"mode_override": "notes"}, "dobe", "admin")
    assert c["effective_mode"] == "notes"
    with pytest.raises(HttpError):
        portal.post("/api/controls", {"mode_override": "live"}, "dobe", "admin")
    c = portal.post("/api/controls", {"kill_switch": False, "mode_override": None}, "dobe", "admin")
    assert not c["kill_switch"] and c["effective_mode"] == "live"
    assert [e["action"] for e in c["events"]] == ["release mode hold", "resume", "hold at notes", "pause"]
    assert c["events"][-1]["actor"] == "dobe" and c["events"][-1]["detail"] == "INC-1"


def test_healthz(portal, psettings, mb):
    status, body = portal.healthz()
    assert status == 503 and body["ok"] is False  # never ran
    log_some(psettings, mb)
    assert portal.healthz()[0] == 200
    audit = AuditLog(psettings.state_db)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    audit.db.execute("UPDATE runs SET started_at = ?, finished_at = ?", (old, old))
    audit.db.commit()
    audit.close()
    assert portal.healthz()[0] == 503
    psettings.kill_switch_file.touch()
    assert portal.healthz()[0] == 200  # paused on purpose counts as healthy


def test_http_auth_csrf_and_page(portal, psettings, mb):
    log_some(psettings, mb)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(portal))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    good = "Basic " + base64.b64encode(b"dobe:correct horse battery").decode()

    def call(path, auth=None, method="GET", body=None, headers=None):
        req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={**({"Authorization": auth} if auth else {}), **(headers or {}),
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, dict(res.headers), res.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    try:
        status, headers, _ = call("/api/session")
        assert status == 401 and "Basic" in headers["WWW-Authenticate"]
        status, headers, body = call("/", good)
        assert status == 200 and body.startswith(b"<!doctype html>") and b'content="served"' in body
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        status, _, body = call("/api/session", good)
        assert json.loads(body)["role"] == "admin"
        assert call("/api/controls", good, "POST", {"kill_switch": True})[0] == 403  # no CSRF header
        assert call("/api/controls", good, "POST", {"kill_switch": True},
                    {"X-Portal-Request": "1", "Origin": "https://evil.example"})[0] == 403
        assert call("/api/controls", good, "POST", {"kill_switch": True}, {"X-Portal-Request": "1"})[0] == 200
        status, headers, body = call("/api/export/decisions.csv", good)
        assert status == 200 and body.decode().startswith("id,created_at,mode,ticket_number")
        assert call("/healthz")[0] == 200
    finally:
        server.shutdown()


def test_lockout_after_repeated_failures(portal):
    bad = "Basic " + base64.b64encode(b"dobe:nope").decode()
    portal_failures = 0
    for _ in range(10):
        with pytest.raises(HttpError):
            portal.authenticate(bad, "10.0.0.9")
        portal_failures += 1
    with pytest.raises(HttpError) as err:
        portal.authenticate("Basic " + base64.b64encode(b"dobe:correct horse battery").decode(), "10.0.0.9")
    assert err.value.status == 429
    assert portal.authenticate("Basic " + base64.b64encode(b"dobe:correct horse battery").decode(), "10.0.0.8")[1] == "admin"


# ---- agent features used by the portal -----------------------------------------
def test_unanswered_request_is_escalated_without_the_model(settings, mb):
    seed(mb, phone="07721234")
    ask = "The phone number on the ticket (<NUMBER_1>) is not valid. Kindly reply with the client's correct mobile number."
    runner, audit = build(settings, mb, ScriptedModel(find(), decision("request_info", ask)), mode="live")
    runner.run()
    # Age the agent's reply past the timeout.
    mb.thread_entries(7)[-1]["created"] = (datetime.now() - timedelta(hours=80)).strftime("%Y-%m-%d %H:%M:%S")
    runner2, audit2 = build(settings, mb, ScriptedModel(), mode="live")  # no model responses: must not be called
    summary = runner2.run()
    assert summary.actions == {"escalate": 1}
    note = mb.thread_entries(7)[-1]
    assert note["type"] == "N" and "no reply" in note["body"]
    runner3, _ = build(settings, mb, ScriptedModel(), mode="live")
    assert runner3.run().skipped == {"already escalated": 1}


def test_queue_snapshot_reflects_each_ticket(settings, mb):
    seed(mb)
    mb.add_ticket(8, "482914", ["Same issue, 0772123456"])
    mb.add_entry(8, "N", "EcoCash agent did not auto-close this ticket.\nold note", staff_id=525)
    runner, audit = build(settings, mb, ScriptedModel(find(), decision("escalate")))
    runner.run()
    rows = {r["ticket_number"]: dict(r) for r in audit.db.execute("SELECT * FROM queue_state").fetchall()}
    assert rows["482913"]["state"] == "escalate" and rows["482913"]["decision_id"]
    assert rows["482914"]["state"] == "skipped" and rows["482914"]["detail"] == "already escalated"
    assert data.queue(audit.db)["counts"] == {"escalate": 1, "skipped": 1}
