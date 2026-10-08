"""The SMS Alerts commands on the shared command line: check and check-account against the fakes."""
from __future__ import annotations

import json

from sms_alerts import __main__ as cli
from tests.sms_alerts.conftest import FakeMetabase


def test_check_prints_settings_databases_and_tickets(settings, mb, monkeypatch, capsys):
    mb.close = lambda: None
    mb.add_ticket(1, "482913", {}, ["Client 0772123456 not getting alerts"])
    monkeypatch.setattr(cli.CLI, "build_metabase", lambda s: mb)
    monkeypatch.setattr(cli.CLI.settings_cls, "from_env", classmethod(lambda cls, env_file=None: settings))
    assert cli.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "[notify] branch emails: off" in out and "[metabase] ticket    'OSTICKETS_NEW'" in out
    assert "[osticket] 1 open ticket(s) under" in out and "[standby] NMBPRODIDC.TRAN" in out
    assert out.rstrip().endswith("All good.")


def test_check_fails_when_a_required_database_is_missing(settings, monkeypatch, capsys):
    mb = FakeMetabase()
    mb.close = lambda: None
    mb.try_resolve = lambda name: None if name == "STANDBY DB" else FakeMetabase.try_resolve(mb, name)
    monkeypatch.setattr(cli.CLI, "build_metabase", lambda s: mb)
    monkeypatch.setattr(cli.CLI.settings_cls, "from_env", classmethod(lambda cls, env_file=None: settings))
    assert cli.main(["check"]) == 1
    assert "[metabase] standby   'STANDBY DB': NOT FOUND" in capsys.readouterr().out


def test_check_account_runs_the_production_check(settings, mb, monkeypatch, capsys):
    mb.close = lambda: None
    monkeypatch.setattr(cli.CLI, "build_metabase", lambda s: mb)
    monkeypatch.setattr(cli.CLI.settings_cls, "from_env", classmethod(lambda cls, env_file=None: settings))
    assert cli.main(["check-account", "--account", "abc"]) == 1
    assert cli.main(["check-account", "--account", "86000013101001", "--phone", "0772123456"]) == 0
    out = capsys.readouterr().out
    assert "--account must contain digits" in out and json.loads(out.split("\n", 1)[1])
