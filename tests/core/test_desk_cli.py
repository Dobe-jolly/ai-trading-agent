"""The shared command line (agentcore.cli.DeskCli): every desk's command set, and the anomaly command."""
from __future__ import annotations

import argparse
import types

import pytest

from agentcore.cli import DeskCli

DESK_COMMANDS = {
    "router": {"topics"},
    "sms_alerts": {"check-account", "anomalies"},
    "ecocash": {"lookup", "monitor", "anomalies"},
    "failed_txn": {"find-transactions"},
    "nmb_connect": {"connect-lookup"},
}
SHARED = {"check", "run", "audit", "report", "compare", "portal", "portal-user", "notify-test", "notify-retry",
          "attachments", "knowledge-seed"}


@pytest.mark.parametrize("desk", sorted(DESK_COMMANDS))
def test_every_desk_has_the_shared_commands_and_its_own(desk):
    module = __import__(f"{desk}.__main__", fromlist=["CLI"])
    assert isinstance(module.CLI, DeskCli) and module.CLI.desk == desk
    _, commands = module.CLI.parser()
    assert set(commands) == SHARED | DESK_COMMANDS[desk]


class Stub:
    def close(self):
        pass


def test_anomalies_prints_the_report_in_the_desks_words(tmp_path, capsys):
    report = {
        "severity": "incident", "summary": {"headline": "Links failing", "summary": "Most links fail since 09:00."},
        "analysis": {
            "signals": [{"severity": "incident", "label": "Link failures", "value": "80%", "usual": "40%",
                         "detail": "TP40024"}],
            "comparisons": {"links": {"failed_n": 40, "linked_n": 10, "findings": [
                {"label": "channel", "value": "USSD", "failed_share": 0.9, "ok_share": 0.5}]}},
            "sources": {"tickets": {"available": False, "error": "timeout"}}},
        "alert": "SMS and e-mail sent",
    }

    class Cli(DeskCli):
        anomaly_noun = ("linked", "links")

        def build_metabase(self, s):
            return Stub()

        def notify_module(self):
            return types.SimpleNamespace(Notifier=lambda *a: Stub(), build_transport=lambda s: None,
                                         build_sms_transport=lambda s: None)

        def build_anomaly_service(self, s, mb, audit, client, notifier):
            assert client is None  # --no-model
            return types.SimpleNamespace(run=lambda alert, use_model: report)

    s = types.SimpleNamespace(state_db=tmp_path / "a.sqlite", anthropic_api_key="k")
    _, commands = Cli().parser()
    assert commands["anomalies"](s, argparse.Namespace(alert=True, no_model=True)) == 0
    out = capsys.readouterr().out
    assert out.startswith("INCIDENT: Links failing") and "Link failures: 80% (usual 40%)" in out
    assert "(40 failed vs 10 linked)" in out and "90% of failures vs 50% of links" in out
    assert "(could not read tickets: timeout)" in out and "Admin alert: SMS and e-mail sent" in out
