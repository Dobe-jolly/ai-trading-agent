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
from agentcore.portal import data
from agentcore.portal.server import HttpError, make_handler
from agentcore.portal.users import UserStore
from ecocash.config import Settings as EcoSettings
from failed_txn.config import Settings as TxnSettings
from nmbz.console import Console
from sms_alerts.config import Settings as SmsSettings


@pytest.fixture
def web(tmp_path):
    """A stand-in for the portal's build (agentcore/portal/web)."""
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text('<!doctype html><div id="root"></div><script src="/assets/index-abc.js"></script>')
    (root / "assets" / "index-abc.js").write_text("console.log('portal')")
    (root / "assets" / "plex-400.woff2").write_bytes(b"wOF2")
    return root


@pytest.fixture
def console(tmp_path, web):
    users = UserStore(tmp_path / "users.json")
    users.set("dobe", "correct horse battery", "admin")
    users.set("viewer1", "another long password", "viewer")
    sms = replace(SmsSettings.from_env(env_file=tmp_path / "none.env"), state_db=tmp_path / "sms" / "agent.sqlite",
                  kill_switch_file=tmp_path / "sms" / "STOP", mode_override_file=tmp_path / "sms" / "HOLD",
                  portal_users_file=tmp_path / "users.json")
    eco = replace(EcoSettings.from_env(env_file=tmp_path / "none.env"), state_db=tmp_path / "eco" / "agent.sqlite",
                  kill_switch_file=tmp_path / "eco" / "STOP", mode_override_file=tmp_path / "eco" / "HOLD",
                  portal_users_file=tmp_path / "users.json")
    txn = replace(TxnSettings.from_env(env_file=tmp_path / "none.env"), state_db=tmp_path / "txn" / "agent.sqlite",
                  kill_switch_file=tmp_path / "txn" / "STOP", mode_override_file=tmp_path / "txn" / "HOLD",
                  portal_users_file=tmp_path / "users.json")
    return Console({"sms_alerts": sms, "ecocash": eco, "failed_txn": txn}, users, stop_file=tmp_path / "STOP",
                   web_dir=web)


def test_service_lists_each_desk_with_its_wording(console):
    svc = console.get("/api/service", {}, "dobe", "admin")
    assert [d["key"] for d in svc["desks"]] == ["sms_alerts", "ecocash", "failed_txn"]
    eco = svc["desks"][1]
    assert eco["title"] == "EcoCash" and eco["vocab"]["chart"]["key"] == "outcomes_by_day"
    assert "already_linked" in eco["vocab"]["status"] and svc["paused"] is False
    txn = svc["desks"][2]
    assert txn["vocab"]["anomaly"]["unavailable"] and "reversal_failed" in txn["vocab"]["status"]
    assert "outcomes_by_day" in console.get("/api/d/failed_txn/overview", {}, "dobe", "admin")
    assert console.get("/api/d/failed_txn/anomalies", {}, "dobe", "admin")["report"] is None


def test_service_says_how_fresh_each_desk_is_and_what_is_held(console, tmp_path):
    svc = console.get("/api/service", {}, "dobe", "admin")
    sms = svc["desks"][0]
    assert sms["health"]["stale"] is True and sms["health"]["minutes_since_last_run"] is None  # never ran
    assert sms["mode_override"] is None and sms["emails_failed"] == 0 and sms["approval_mode"] == "unscripted"
    assert {d["key"]: d["anomaly_watch"] for d in svc["desks"]} == {
        "sms_alerts": True, "ecocash": True, "failed_txn": False}

    audit = AuditLog(console.portals["sms_alerts"].s.state_db)
    run_id = audit.start_run("live", "model")
    audit.finish_run(run_id, 3, 2, 1)
    audit.db.execute("INSERT INTO notifications (uuid, created_at, kind, channel, subject, status) "
                     "VALUES ('u1', ?, 'reply_and_close', 'gateway', 's', 'failed')", (data.since_iso(0),))
    audit.db.commit()
    audit.close()
    console.post("/api/d/sms_alerts/controls", {"mode_override": "notes", "note": "testing"}, "dobe", "admin")
    sms = console.get("/api/service", {}, "dobe", "admin")["desks"][0]
    assert sms["health"]["stale"] is False and sms["health"]["minutes_since_last_run"] < 1
    assert sms["health"]["last_run_errors"] == 1 and sms["health"]["last_run_at"]
    assert sms["mode_override"] == "notes" and sms["emails_failed"] == 1


def test_classic_page_when_the_portal_is_not_built(console, tmp_path):
    console.web_dir = tmp_path / "no-build"
    assert b'id="desks"' in console.index_html()
    body, kind, _ = console.page("/approvals")
    assert b'id="desks"' in body and kind.startswith("text/html")
    assert console.page("/assets/index-abc.js") is None


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
    for key in ("sms_alerts", "ecocash", "failed_txn"):
        events = console.get(f"/api/d/{key}/controls", {}, "dobe", "admin")["events"]
        assert events[0]["action"] == "pause every desk" and events[0]["detail"] == "core banking maintenance"
    status, body = console.healthz()
    assert status == 200 and body["paused"] and set(body["desks"]) == {"sms_alerts", "ecocash", "failed_txn"}
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
        assert status == 200 and len(json.loads(body)["desks"]) == 3
        status, body, _ = get("/")
        assert b'<div id="root">' in body  # the portal
        status, body, _ = get("/tickets/600512?desk=ecocash")
        assert status == 200 and b'<div id="root">' in body  # its router reads the address
        status, body, _ = get("/classic")
        assert b'meta name="portal" content="served"' in body and b'id="desks"' in body
        status, body, headers = get("/assets/index-abc.js")
        assert body == b"console.log('portal')" and headers["Content-Type"].startswith("text/javascript")
        assert "immutable" in headers["Cache-Control"] and headers["X-Content-Type-Options"] == "nosniff"
        _, _, headers = get("/assets/plex-400.woff2")
        assert headers["Content-Type"] == "font/woff2"
        for path in ("/assets/missing.js", "/assets/notes.txt", "/assets/..%2findex.html", "/favicon.ico",
                     "/api/nothing"):
            with pytest.raises(urllib.error.HTTPError) as err:
                get(path)
            assert err.value.code == 404, path
        with pytest.raises(urllib.error.HTTPError) as err:  # pages are behind the sign-in too
            urllib.request.urlopen(base + "/approvals", timeout=5)
        assert err.value.code == 401
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


def test_teaching_a_desk_through_the_console(console):
    audit = AuditLog(console.portals["failed_txn"].s.state_db)
    cursor = audit.db.execute("INSERT INTO decisions (run_id, created_at, mode, ticket_id, ticket_number, final_action) "
                              "VALUES ('r', ?, 'live', 9, '700512', 'escalate')", (data.since_iso(0),))
    audit.db.commit()
    decision_id = cursor.lastrowid
    audit.close()
    note = {"decision_id": decision_id, "title": "Response 91 is a timeout",
            "body": "Postilion response 91 means the issuer did not answer in time; check core banking first."}

    taught = console.post("/api/d/failed_txn/knowledge", note, "viewer1", "viewer")
    assert taught["status"] == "proposed" and taught["source_ticket"] == "700512"
    svc = console.get("/api/service", {}, "dobe", "admin")
    assert {d["key"]: d["knowledge_pending"] for d in svc["desks"]}["failed_txn"] == 1
    listing = console.get("/api/d/failed_txn/knowledge", {}, "dobe", "admin")
    assert listing["pending"] == 1 and listing["can_approve"] and "POST OFFICE" in listing["databases"]
    assert set(listing["slot_kinds"]) >= {"account", "mobile", "date", "code"}

    own = console.post("/api/d/failed_txn/knowledge", {**note, "title": "Another note"}, "dobe", "admin")
    with pytest.raises(HttpError) as err:
        console.post(f"/api/d/failed_txn/knowledge/{own['id']}", {"verdict": "approve"}, "dobe", "admin")
    assert err.value.status == 403
    with pytest.raises(HttpError) as err:
        console.post(f"/api/d/failed_txn/knowledge/{taught['id']}", {"verdict": "approve"}, "viewer1", "viewer")
    assert err.value.status == 403
    on = console.post(f"/api/d/failed_txn/knowledge/{taught['id']}", {"verdict": "approve", "note": "ok"}, "dobe",
                      "admin")
    assert on["status"] == "active"
    events = console.get("/api/d/failed_txn/controls", {}, "dobe", "admin")["events"]
    assert any(e["action"] == f"switched on note K{taught['id']}" for e in events)
    with pytest.raises(HttpError) as err:
        console.post("/api/d/failed_txn/knowledge", {**note, "query": {"database": "POST OFFICE", "sql": "DELETE FROM x",
                                                                       "slots": {}, "about": "x" * 50}}, "dobe", "admin")
    assert err.value.status == 400 and "can only read" in err.value.message

    portal = console.portals["failed_txn"]
    portal.trials.execute = lambda number, item, original: {"final_action": "reply_and_close", "draft_used": True,
                                                            "was": original["final_action"]}
    trial = console.post("/api/d/failed_txn/knowledge/trials", {"decision_id": decision_id, "draft": note}, "viewer1",
                         "viewer")
    assert trial["ticket_number"] == "700512"
    for _ in range(50):
        got = console.get(f"/api/d/failed_txn/knowledge/trials/{trial['id']}", {}, "viewer1", "viewer")
        if got["status"] != "running":
            break
        threading.Event().wait(0.02)
    assert got["status"] == "done" and got["result"]["was"] == "escalate"
    listed = console.get("/api/d/failed_txn/knowledge/trials", {"decision": str(decision_id)}, "viewer1", "viewer")
    assert [t["id"] for t in listed["items"]] == [trial["id"]]
