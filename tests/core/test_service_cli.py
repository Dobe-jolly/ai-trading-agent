"""python -m nmbz: one pass visits every desk in DESKS; desk commands go to the right desk."""
from __future__ import annotations

import pytest

from nmbz import __main__ as cli
from nmbz import desks as registry


@pytest.fixture
def calls(monkeypatch, tmp_path):
    seen = []

    def fake_main(self, argv):
        seen.append((self.key, argv))
        return {"sms_alerts": 0, "ecocash": 3}.get(self.key, 0)

    monkeypatch.setattr(registry.Desk, "main", fake_main)
    monkeypatch.setenv("SERVICE_KILL_SWITCH_FILE", str(tmp_path / "STOP"))
    monkeypatch.setattr(cli, "_service_lock", lambda: open(tmp_path / "lock", "w"))
    monkeypatch.delenv("DESKS", raising=False)
    return seen


def test_run_visits_every_desk_in_order_and_reports_the_worst(calls):
    assert cli.main(["run", "--mode", "shadow", "--limit", "5"]) == 3
    assert calls == [("router", ["run", "--mode", "shadow", "--limit", "5"]),  # first: it files tickets for the rest
                     ("sms_alerts", ["run", "--mode", "shadow", "--limit", "5"]),
                     ("ecocash", ["run", "--mode", "shadow", "--limit", "5"]),
                     ("failed_txn", ["run", "--mode", "shadow", "--limit", "5"]),
                     ("nmb_connect", ["run", "--mode", "shadow", "--limit", "5"])]


def test_desks_setting_picks_and_orders_the_desks(calls, monkeypatch):
    monkeypatch.setenv("DESKS", "ecocash")
    cli.main(["run"])
    assert [k for k, _ in calls] == ["ecocash"]
    monkeypatch.setenv("DESKS", "ecocash,payroll")
    assert cli.main(["run"]) == 1  # unknown desk: refused, nothing runs
    assert len(calls) == 1


def test_service_pause_stops_every_desk(calls, tmp_path):
    (tmp_path / "STOP").write_text("paused")
    assert cli.main(["run"]) == 0 and calls == []


def test_one_ticket_needs_a_desk(calls):
    with pytest.raises(SystemExit, match="--ticket needs --desk"):
        cli.main(["run", "--ticket", "100001"])
    cli.main(["--desk", "ecocash", "run", "--ticket", "100001"])
    assert calls == [("ecocash", ["run", "--ticket", "100001"])]


def test_desk_commands_go_to_their_desk(calls):
    cli.main(["lookup", "--mobile", "0771234567"])
    cli.main(["check-account", "--account", "1"])
    cli.main(["find-transactions", "--account", "1"])
    cli.main(["topics"])
    cli.main(["connect-lookup", "--cif", "1234567"])
    with pytest.raises(SystemExit, match="Say which desk"):
        cli.main(["audit", "--since", "2026-10-01"])
    cli.main(["audit", "--desk", "sms_alerts", "--since", "2026-10-01"])
    with pytest.raises(SystemExit, match="has no 'monitor' command"):
        cli.main(["monitor", "--desk", "sms_alerts"])
    assert calls == [("ecocash", ["lookup", "--mobile", "0771234567"]),
                     ("sms_alerts", ["check-account", "--account", "1"]),
                     ("failed_txn", ["find-transactions", "--account", "1"]),
                     ("router", ["topics"]),
                     ("nmb_connect", ["connect-lookup", "--cif", "1234567"]),
                     ("sms_alerts", ["audit", "--since", "2026-10-01"])]
