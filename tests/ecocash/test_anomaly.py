"""Anomaly watch for EcoCash linking: signals, the failed-vs-linked comparison, collection, alerts."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from ecocash.anomaly.collect import Collector
from ecocash.anomaly.detect import analyse
from ecocash.anomaly.features import attempt_features
from ecocash.anomaly.service import AnomalyService
from agentcore.audit import AuditLog
from ecocash.ecocash import AttemptStore
from ecocash.normalize import now_local
from tests.ecocash.conftest import ECOCASH_DB, TICKET_DB, ScriptedModel


def data(current, baseline, recent=None, days=10, tickets=None):
    return {"tickets": tickets or {"available": False}, "samples": {"available": False},
            "attempts": {"available": True, "current": current, "recent": recent or {}, "recent_since": "13:00",
                         "baseline_per_day": baseline, "baseline_days": days, "recording_since": "2026-09-20T08:00"}}


USUAL = {"linked": 150, "already_linked": 40, "kyc_mismatch": 8, "number_not_ecocash": 5, "wallet_closed": 5,
         "ecocash_system_error": 2}


def test_normal_day_is_ok_and_a_young_baseline_waits():
    assert analyse(data(dict(USUAL), USUAL))["severity"] == "ok"
    young = analyse(data(dict(USUAL), USUAL, days=2))
    assert young["severity"] == "ok" and "signals start after 3" in young["signals"][0]["detail"]


def test_ecocash_outage_shows_in_the_last_hours_and_by_error():
    current = dict(USUAL, ecocash_system_error=60, linked=90)
    recent = {"linked": 2, "ecocash_system_error": 30, "no_response": 6}
    a = analyse(data(current, USUAL, recent=recent))
    sev = {s["key"]: s["severity"] for s in a["signals"]}
    assert a["severity"] == "incident"
    assert sev["kind_ecocash_system_error"] == "incident" and sev["failure_rate_recent"] == "incident"
    assert sev["ecocash_errors_recent"] == "incident"


def test_new_error_and_volume_drop():
    a = analyse(data(dict(USUAL, unknown=7), USUAL))
    assert {s["key"]: s["severity"] for s in a["signals"]}["kind_unknown"] == "watch"
    low = analyse(data({"linked": 20, "already_linked": 5}, USUAL))
    assert {s["key"]: s["severity"] for s in low["signals"]}["volume"] == "incident"


def test_failed_vs_linked_comparison_finds_the_pattern():
    failed = [attempt_features({"via": "IAPPROVE", "channel": "IAPPROVE", "currency": "ZiG", "mobile_number": "263771234567",
                                "account_number": "86000013101001", "id_number": "587654321F09",
                                "requested_at": "2026-10-06 10:00:00"}) | {"error_type": "kyc_mismatch"}] * 30
    linked = [attempt_features({"via": "USSD", "channel": "NMBCONNECT", "currency": "USD", "mobile_number": "263771234567",
                                "account_number": "09412345310100", "id_number": "587654321F09",
                                "requested_at": "2026-10-06 10:00:00"})] * 60
    d = data(dict(USUAL), USUAL)
    d["samples"] = {"available": True, "failed": failed, "linked": linked}
    a = analyse(d)
    found = {(f["feature"], f["value"]) for f in a["comparisons"]["attempts"]["findings"]}
    assert ("via", "account opening (iApprove)") in found and ("currency", "ZiG") in found and ("cif_series", "86") in found
    assert a["comparisons"]["attempts"]["errors"][0]["key"] == "kyc_mismatch"


def test_collector_reads_the_attempts_table(mb, tmp_path):
    now = now_local()
    for day in range(1, 6):
        for i in range(10):
            mb.add_attempt("linked", now - timedelta(days=day, minutes=i))
        mb.add_attempt("already_linked", now - timedelta(days=day, minutes=30))
    for i in range(12):
        mb.add_attempt("ecocash_system_error", now - timedelta(minutes=5 + i))
    mb.add_ticket(1, "100001", ["help"], created=now - timedelta(hours=2))
    audit = AuditLog(tmp_path / "a.sqlite")
    collector = Collector(mb, audit.db, ticket_db=TICKET_DB, attempts=AttemptStore(mb, ECOCASH_DB), topics=["EcoCash"],
                          baseline_days=14, sample_size=100)
    collected = collector.collect(now)
    att = collected["attempts"]
    assert att["available"] and att["current"]["ecocash_system_error"] == 12 and att["baseline_days"] == 4
    assert att["baseline_per_day"]["linked"] == 10.0 and collected["tickets"]["current"] == 1
    assert len(collected["samples"]["failed"]) == 12 and collected["samples"]["failed"][0]["error_type"] == "ecocash_system_error"
    a = analyse(collected, min_attempts=10)
    assert a["severity"] == "incident"
    before = len(mb.sql_log)
    collector.collect(now)  # complete days come from the cache
    assert sum(1 for _, q in mb.sql_log[before:] if "date_trunc('day'" in q) == 0


def test_service_alerts_admins_and_briefs_the_agent(settings, tmp_path):
    class FakeCollector:
        def __init__(self, d):
            self.d = d

        def collect(self, now=None):
            return self.d

    class Sent:
        channel = "sms"

        def __init__(self):
            self.sent = []

        def send(self, **kw):
            self.sent.append(kw)

        def close(self):
            pass

    from ecocash.notify import Notifier

    s = replace(settings, admin_sms_recipients=("+263771000001",), anomaly_enabled=True)
    audit = AuditLog(tmp_path / "a.sqlite")
    sms = Sent()
    outage = data(dict(USUAL, ecocash_system_error=60), USUAL, recent={"ecocash_system_error": 20})
    model = ScriptedModel(SimpleNamespace(content=[SimpleNamespace(
        type="tool_use", name="submit_summary", id="s",
        input={"headline": "EcoCash system errors since 13:00", "summary": "x", "likely_causes": ["EcoCash outage"],
               "actions": ["Call EcoCash"]})], usage=SimpleNamespace(input_tokens=1, output_tokens=1)))
    service = AnomalyService(s, audit, FakeCollector(outage), model, Notifier(audit, s, None, sms))
    report = service.run()
    assert report["alert"] == "incident" and sms.sent[0]["body"].startswith("EcoCash INCIDENT: EcoCash system errors")
    assert service.context().startswith("INCIDENT")
