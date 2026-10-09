"""The desk's own commands against the fakes: check (databases, tickets per topic, the profile table) and
connect-lookup. All data invented."""
from __future__ import annotations

import argparse
from dataclasses import replace

from nmb_connect.__main__ import NmbConnectCli
from tests.nmb_connect.conftest import ACCOUNT, CIF, MOBILE, FakeMetabase


class Cli(NmbConnectCli):
    def __init__(self, mb):
        self.mb = mb

    def build_metabase(self, s):
        return self.mb


def fake(mb, missing=()):
    mb.try_resolve = lambda name: None if name in missing else mb.resolve(name)
    mb.close = lambda: None
    return mb


def test_check_lists_tickets_and_the_profile_table(settings, capsys):
    mb = fake(FakeMetabase(), missing=("SOCKET SMS DB",))
    mb.add_profile()
    mb.add_ticket(1, "300001", ["otp"], topic="NMB Connect / OTP")
    mb.add_ticket(2, "300002", ["otp"], topic="NMB Connect / USSD / OTP")
    mb.add_ticket(3, "300003", ["zipit"], topic="NMB Connect / USSD / Failed Transactions / ZIPIT")
    assert Cli(mb).cmd_check(settings, argparse.Namespace(mode=None, ping_model=False)) == 0
    out = capsys.readouterr().out
    assert "[metabase] socket    'SOCKET SMS DB': NOT FOUND (optional: its lookups are off)" in out
    assert "[osticket] 2 open NMB Connect ticket(s)" in out
    assert "[reg] 4 table(s) in CONECT 2.0 REG" in out and "[reg] customer: id, customer_number" in out
    assert "[reg] 'user_channels' not found" in out and out.rstrip().endswith("All good.")
    no_failures = replace(settings, reg_failures_db_name="")
    Cli(mb).cmd_check(no_failures, argparse.Namespace(mode=None, ping_model=False))
    assert "REG_FAILURES_DB_NAME is not set" in capsys.readouterr().out
    mb.try_resolve = lambda name: None if name == "CONECT 2.0 REG" else mb.resolve(name)
    assert Cli(mb).cmd_check(settings, argparse.Namespace(mode=None, ping_model=False)) == 1


def test_connect_lookup_shows_the_client_without_secrets(settings, capsys):
    mb = fake(FakeMetabase())
    mb.add_profile()
    mb.add_activity()
    args = argparse.Namespace(cif=None, account=ACCOUNT, mobile=MOBILE, id_number=None)
    assert Cli(mb).cmd_lookup(settings, args) == 0
    out = capsys.readouterr().out
    assert "== get_connect_profile" in out and "== check_core_banking" in out and "== get_sms_to_number" in out
    assert MOBILE in out and CIF in out  # the operator sees real values
    assert "tmoyo77" not in out and "notarealhash" not in out and "TENDAI" not in out
