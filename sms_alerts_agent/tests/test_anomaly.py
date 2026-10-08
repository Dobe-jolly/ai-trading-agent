"""Anomaly watch: features, comparison, detection, collection, alerts, and the ticket agent's briefing."""
from __future__ import annotations

import json
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from agent.anomaly.collect import Collector
from agent.anomaly.detect import analyse, incident_key
from agent.anomaly.features import compare, gateway_features, network_of
from agent.anomaly.service import AnomalyService
from agent.anomaly.summarize import fallback_summary, summarise
from agentcore.audit import AuditLog
from agent.normalize import LOCAL_ZONE
from agent.notify import GatewaySms, Notifier
from agent.portal.server import Portal
from agentcore.portal.users import UserStore
from tests.conftest import DISPATCH_DB, SOCKET_DB, STANDBY_DB, TICKET_DB, ScriptedModel, check, decision
from tests.test_agent_runner import build, seed
from tests.test_notify import RecordingTransport

NOW = datetime.now(LOCAL_ZONE).replace(microsecond=0)


def payload(phone: str, error: str = "", text: str = "Acc ****5678901 debited USD 15.00") -> str:
    err = f',"errorDescription":"{error}"' if error else ""
    return f'{{"to":"{phone}","message":"{text}"{err}}}'


# ---- features and comparison ------------------------------------------------------
def test_gateway_features():
    f = gateway_features({"time_stamp": "2026-10-07 09:15:00", "service_provider_name": "NETONE", "external_status": "FAILED_TO_DELIVER",
                          "logged_responses": payload("263712345678", "Absent subscriber 27")})
    assert f["network"] == "NetOne" and f["phone_format"] == "+263 / 263" and f["currency"] == "USD"
    assert f["amount_band"] == "10–99" and f["error"] == "Absent subscriber N" and f["parts"] == "1 SMS" and f["kind"] == "debit"
    long_unicode = gateway_features({"logged_responses": payload("2632637712345678", text="Credit ZiG 5 ✓ " + "x" * 80)})
    assert long_unicode["encoding"] == "unicode (UCS-2)" and long_unicode["parts"] == "multi-part"
    assert long_unicode["phone_format"] == "doubled 263" and long_unicode["kind"] == "credit"
    assert network_of("0772123456") == "Econet" and network_of("263731234567") == "Telecel" and network_of(None) == "unknown"


def test_compare_finds_what_failures_share():
    failed = [{"network": "NetOne"}] * 30 + [{"network": "Econet"}] * 5
    ok = [{"network": "Econet"}] * 90 + [{"network": "NetOne"}] * 10
    findings = compare(failed, ok, ["network"])
    assert findings[0]["value"] == "NetOne" and findings[0]["failed_share"] == pytest.approx(30 / 35, abs=0.01)
    assert findings[0]["ratio"] == pytest.approx((31 / 37) / (11 / 102), abs=0.1)
    # a value seen a few times among failures and never among a handful of deliveries does not outrank it
    few = compare([{"b": "x", "n": "N"}] * 3 + [{"b": "y", "n": "N"}] * 12, [{"b": "y", "n": "E"}] * 7, ["b", "n"], min_failed=3)
    assert few[0]["value"] == "N"
    assert all(f["value"] != "Econet" for f in findings)


# ---- detection -----------------------------------------------------------------------
def empty_data(**over):
    base = {"tickets": {"available": False}, "gateway": {"available": False}, "gateway_samples": {"available": False},
            "dispatch": {"available": False}, "cae": {"available": False},
            "ticket_checks": {"available": True, "current": {"delivered": [], "not_delivered": [], "other": 0},
                              "baseline": {"delivered": 0, "not_delivered": 0}}}
    base.update(over)
    return base


def tickets(current, baseline, by_branch=None):
    return {"available": True, "current": current, "baseline": baseline, "by_topic": {}, "by_topic_baseline": {},
            "by_branch": by_branch or {}, "by_branch_baseline": {"Gweru": 1.0}, "daily": []}


def test_ticket_volume_spike_is_an_incident_and_names_the_branch():
    a = analyse(empty_data(tickets=tickets(31, [8, 9, 10, 7, 9, 8, 11, 9, 8, 10, 9, 7, 8, 9], {"Gweru": 19, "Mutare": 3})))
    assert a["severity"] == "incident"
    assert a["signals"][0]["key"] == "ticket_volume" and a["signals"][0]["usual"] == 9
    assert "Gweru: 19" in a["signals"][1]["detail"]
    assert analyse(empty_data(tickets=tickets(11, [8, 9, 10, 7, 9, 8, 11, 9, 8, 10, 9, 7, 8, 9])))["severity"] == "ok"


def gateway_data(current, baseline, stuck=0):
    return {"available": True, "current": current, "baseline_per_day": baseline, "baseline_days": 14,
            "stuck_pending": stuck, "hourly": []}


def test_route_failure_and_volume_drop():
    base = {"ECONET": {"delivered": 3800, "failed": 60}, "NETONE": {"delivered": 1100, "failed": 20}}
    cur = {"ECONET": {"delivered": 3750, "failed": 70}, "NETONE": {"delivered": 500, "failed": 640}}
    a = analyse(empty_data(gateway=gateway_data(cur, base)))
    keys = {s["key"]: s["severity"] for s in a["signals"]}
    assert keys["gateway_route_NETONE"] == "incident" and "gateway_route_ECONET" not in keys
    assert a["severity"] == "incident"
    low = analyse(empty_data(gateway=gateway_data({"ECONET": {"delivered": 900, "failed": 10}}, base)))
    assert {s["key"]: s["severity"] for s in low["signals"]}["gateway_volume"] == "incident"
    sudden = gateway_data(base, base)
    sudden.update(recent={"NETONE": {"delivered": 40, "failed": 160}, "ECONET": {"delivered": 600, "failed": 9}}, recent_since="13:00")
    fast = {s["key"]: s for s in analyse(empty_data(gateway=sudden))["signals"]}
    assert fast["gateway_recent_NETONE"]["severity"] == "incident" and "since 13:00" in fast["gateway_recent_NETONE"]["label"]
    assert "gateway_recent_ECONET" not in fast and "gateway_route_NETONE" not in fast
    backlog = analyse(empty_data(gateway=gateway_data(base, base, stuck=400)))
    assert {s["key"]: s["severity"] for s in backlog["signals"]}["gateway_backlog"] == "incident"


def test_dispatch_cae_and_ticket_check_signals():
    d = {"available": True, "today": {"SENT": 500, "FAILED": 300}, "baseline_total": {"SENT": 9800, "FAILED": 200}}
    c = {"available": True, "today": {"SUCCESS": 900, "FAILED": 20}, "baseline_total": {"SUCCESS": 9900, "FAILED": 100}}
    checks = {"available": True, "current": {"delivered": [{}] * 3, "not_delivered": [{"email_on_file": "no"}] * 12, "other": 0},
              "baseline": {"delivered": 40, "not_delivered": 10}}
    a = analyse(empty_data(dispatch=d, cae=c, ticket_checks=checks))
    sev = {s["key"]: s["severity"] for s in a["signals"]}
    assert sev == {"dispatch_rate": "incident", "cae_rate": "ok", "ticket_not_delivered": "incident"}
    assert incident_key(a) == "dispatch_rate|ticket_not_delivered"


# ---- collection over the fake pipeline ---------------------------------------------------
def build_incident(mb):
    """14 normal days, then NetOne sends failing for the last 10 hours, and a ticket spike from Gweru."""
    rng = random.Random(4)
    for day in range(15, 0, -1):
        for _ in range(60):
            at = NOW - timedelta(days=day, minutes=rng.randint(0, 1439))
            phone, provider = rng.choice([("263772000111", "ECONET"), ("263712000222", "NETONE")])
            mb.sends.append({"time_stamp": at.isoformat(), "service_provider_name": provider,
                             "external_status": "DELIVERED_SUCCESSFULLY" if rng.random() > 0.03 else "FAILED_TO_DELIVER",
                             "logged_responses": payload(phone)})
    for _ in range(140):
        at = NOW - timedelta(minutes=rng.randint(1, 600))
        netone = rng.random() < 0.45
        phone, provider = ("263712000222", "NETONE") if netone else ("263772000111", "ECONET")
        failed = netone and rng.random() < 0.8
        mb.sends.append({"time_stamp": at.isoformat(), "service_provider_name": provider,
                         "external_status": "FAILED_TO_DELIVER" if failed else "DELIVERED_SUCCESSFULLY",
                         "logged_responses": payload(phone, "Absent subscriber" if failed else "")})
    for day in range(15, 0, -1):
        for i in range(8):
            mb.volume_rows.append({"ticket_id": day * 100 + i, "created": (NOW - timedelta(days=day, hours=i)).strftime("%Y-%m-%d %H:%M:%S"),
                                   "help_topic": "SMS Alerts / Transactional", "org_name": rng.choice(["Gweru", "Mutare", "Head Office"])})
    for i in range(30):
        mb.volume_rows.append({"ticket_id": 9000 + i, "created": (NOW - timedelta(minutes=10 + 20 * i)).strftime("%Y-%m-%d %H:%M:%S"),
                               "help_topic": "SMS Alerts / Transactional", "org_name": "Gweru" if i % 3 else "Mutare"})
    for day in range(15, -1, -1):
        d = (NOW - timedelta(days=day)).date().isoformat()
        mb.dispatch_daily[d] = {"SENT": 800, "FAILED": 5}
        mb.cae_daily[d] = {"SUCCESS": 900, "FAILED": 3}


def collector(mb, audit):
    return Collector(mb, audit.db, ticket_db=TICKET_DB, socket_db=SOCKET_DB, dispatch_db=DISPATCH_DB, standby_db=STANDBY_DB,
                     window_hours=24, baseline_days=14, sample_size=300)


def test_collect_and_analyse_finds_the_netone_incident(mb, tmp_path):
    build_incident(mb)
    audit = AuditLog(tmp_path / "a.sqlite")
    data = collector(mb, audit).collect(NOW)
    assert data["tickets"]["current"] == 30 and data["gateway"]["baseline_days"] == 14
    a = analyse(data, min_tickets=5, min_sends=100)
    sev = {s["key"]: s["severity"] for s in a["signals"]}
    assert a["severity"] == "incident"
    assert sev["ticket_volume"] == "incident" and sev["gateway_route_NETONE"] == "incident"
    top = {(f["feature"], f["value"]) for f in a["comparisons"]["gateway"]["findings"][:4]}
    assert ("network", "NetOne") in top and all(f["feature"] != "error" for f in a["comparisons"]["gateway"]["findings"])
    assert a["comparisons"]["gateway"]["errors"][0]["value"] == "Absent subscriber"
    assert "Gweru" in next(s["detail"] for s in a["signals"] if s["key"] == "ticket_volume_branch")
    # Complete days are cached: a second run only queries the current window for the gateway.
    before = len([q for db, q in mb.sql_log if db == SOCKET_DB])
    collector(mb, audit).collect(NOW)
    assert len([q for db, q in mb.sql_log if db == SOCKET_DB]) - before == 4  # hourly aggregate + 3 samples


def test_unavailable_sources_do_not_stop_the_analysis(mb, tmp_path):
    build_incident(mb)
    mb.fail_patterns = ["transaction_activity"]
    audit = AuditLog(tmp_path / "a.sqlite")
    data = collector(mb, audit).collect(NOW)
    a = analyse(data)
    assert a["sources"]["gateway"]["available"] is False and "simulated failure" in a["sources"]["gateway"]["error"]
    assert a["severity"] == "incident"  # tickets alone


# ---- summary, alerts, briefing --------------------------------------------------------
def summary_model(headline="NetOne SMS alerts failing since 08:00"):
    return ScriptedModel(SimpleNamespace(content=[SimpleNamespace(
        type="tool_use", name="submit_summary", id="s",
        input={"headline": headline, "summary": "80% of NetOne sends failed.", "likely_causes": ["NetOne route"],
               "actions": ["Call the NetOne SMSC"], "branch_message": "Some NetOne customers may not get alerts today."})],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1)))


def test_summary_from_model_and_fallback(tmp_path, settings):
    a = analyse(empty_data(tickets=tickets(31, [9] * 14)))
    out = summarise(summary_model(), "m", settings.anomaly_prompt_file, a, {})
    assert out["by"] == "model" and out["headline"].startswith("NetOne")

    class Broken:
        messages = SimpleNamespace(create=lambda **k: (_ for _ in ()).throw(RuntimeError("down")))

    fb = summarise(Broken(), "m", settings.anomaly_prompt_file, a, {})
    assert fb["by"] == "rules" and "SMS Alerts tickets" in fb["headline"]
    assert fallback_summary(analyse(empty_data()))["headline"] == "SMS alerts look normal"


class FakeCollector:
    def __init__(self, data):
        self.data = data

    def collect(self, now=None):
        return self.data


def test_alerts_on_incident_update_reminder_and_recovery(settings, tmp_path):
    s = replace(settings, admin_sms_recipients=("+263777000001",), admin_email_recipients=("ops@bank.example",),
                portal_url="https://portal.bank.example", anomaly_realert_hours=4.0)
    audit = AuditLog(tmp_path / "a.sqlite")
    sms, mail = RecordingTransport(), RecordingTransport()
    notifier = Notifier(audit, s, mail, sms)
    spike = empty_data(tickets=tickets(31, [9] * 14))
    service = AnomalyService(s, audit, FakeCollector(spike), summary_model(), notifier)

    first = service.run()
    assert first["alert"] == "incident" and len(sms.sent) == 1 and len(mail.sent) == 1
    assert sms.sent[0]["body"].startswith("SMS Alerts INCIDENT: NetOne SMS alerts failing")
    assert "https://portal.bank.example#anomalies" in sms.sent[0]["body"]
    assert "Suggested note to branches" in mail.sent[0]["body"]

    service.client = summary_model()
    assert service.run()["alert"] is None  # same incident, within the reminder period
    audit.db.execute("UPDATE anomaly_state SET last_alert_at = ?", ((datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),))
    audit.db.commit()
    service.client = summary_model()
    assert service.run()["alert"] == "reminder"

    worse = empty_data(tickets=tickets(31, [9] * 14), dispatch={"available": True, "today": {"SENT": 100, "FAILED": 400},
                                                                 "baseline_total": {"SENT": 9800, "FAILED": 200}})
    service.collector, service.client = FakeCollector(worse), summary_model()
    assert service.run()["alert"] == "update"

    eased = empty_data(tickets=tickets(17, [9] * 14))  # down to a watch: still the same incident
    service.collector, service.client = FakeCollector(eased), summary_model()
    assert analyse(eased)["severity"] == "watch" and service.run()["alert"] is None

    service.collector = FakeCollector(empty_data(tickets=tickets(9, [9] * 14)))
    recovered = service.run()
    assert recovered["severity"] == "ok" and recovered["alert"] == "resolved"
    assert sms.sent[-1]["body"].startswith("SMS Alerts RESOLVED")
    assert service.run()["alert"] is None


def test_gateway_sms_payload_matches_the_java_contract():
    seen = []

    def handler(request):
        seen.append((request.headers.get("authorization"), json.loads(request.content)))
        return httpx.Response(200, json={})

    GatewaySms("http://gw/sms", "sms-token-123", "dXNlcjpwYXNz", transport=httpx.MockTransport(handler)).send(
        to="+263777000001", cc=[], subject="", body="SMS Alerts INCIDENT: x", message_id="m")
    assert seen == [("Basic dXNlcjpwYXNz", {"mobile": "+263777000001", "message": "SMS Alerts INCIDENT: x",
                                             "originator": "NMB Bank", "access_token": "sms-token-123"})]


def test_ticket_agent_is_briefed_and_facts_are_recorded(settings, mb, tmp_path):
    seed(mb)
    runner, audit = build(settings, mb, ScriptedModel(check(), decision("escalate")), mode="live")
    service = AnomalyService(settings, audit, FakeCollector(empty_data(tickets=tickets(31, [9] * 14))), summary_model(), None)
    service.run(alert=False)
    runner.anomaly = service
    runner.run()
    prompt = runner.agent._client.calls[0]["messages"][0]["content"]
    assert "Operations context: INCIDENT" in prompt and "NetOne SMS alerts failing" in prompt
    note = mb.thread_entries(7)[-1]["body"]
    assert "Open SMS incident at the time" in note
    row = audit.last_for_ticket(7)
    facts = json.loads(row["facts"])
    fact = next(iter(facts.values()))
    assert fact["phone_network"] == "Econet" and fact["status"] == "delivered_check_spam" and fact["providers"] == ["ECONET"]
    assert "12345678901" not in row["facts"]


def test_portal_shows_reports(settings, tmp_path):
    audit = AuditLog(settings.state_db)
    service = AnomalyService(settings, audit, FakeCollector(empty_data(tickets=tickets(31, [9] * 14))), summary_model(), None)
    rid = service.run(alert=False)["id"]
    audit.close()
    users = UserStore(tmp_path / "u.json")
    users.set("dobe", "correct horse battery", "admin")
    portal = Portal(replace(settings, portal_users_file=tmp_path / "u.json"), users)
    latest = portal.get("/api/anomalies", {}, "dobe", "admin")
    assert latest["report"]["severity"] == "incident" and latest["report"]["summary"]["by"] == "model"
    assert latest["history"][0]["id"] == rid
    assert portal.get("/api/session", {}, "dobe", "admin")["anomaly"]["headline"].startswith("NetOne")
    assert portal.get(f"/api/anomalies/{rid}", {}, "dobe", "admin")["report"]["id"] == rid
