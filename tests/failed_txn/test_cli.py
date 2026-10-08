"""The desk's own commands against the fakes: check (databases, tickets per topic) and find-transactions."""
from __future__ import annotations

import argparse

from failed_txn.__main__ import FailedTxnCli
from tests.failed_txn.conftest import ACCOUNT, FakeMetabase


class Cli(FailedTxnCli):
    def __init__(self, mb):
        self.mb = mb

    def build_metabase(self, s):
        return self.mb


def fake(mb):
    mb.try_resolve = lambda name: mb.resolve(name) if name != "IB" else None
    mb.close = lambda: None
    return mb


def test_check_counts_tickets_per_topic_and_names_quiet_topics(settings, capsys):
    mb = fake(FakeMetabase())
    mb.add_ticket(1, "200001", ["acc 86000013101001 ZIPIT failed"])
    mb.add_ticket(2, "200002", ["acc 86000013101001 ZIPIT failed"])
    mb.add_ticket(3, "200003", ["acc 86000013101001 ZESA"], topic="FAILED ZESA")
    assert Cli(mb).check(settings, argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "[WARN] Metabase database 'IB' not found" in out and "3 open failed-transaction ticket(s)" in out
    assert "   2  Failed ZIPIT Transaction" in out and "   1  FAILED ZESA" in out
    assert "no open tickets under:" in out and "FAILED AIRTIME PURCHASE" in out
    assert "Failed ZIPIT Transaction," not in out.split("no open tickets under:")[1]


def test_find_transactions_prints_every_source_with_real_values(settings, capsys):
    mb = fake(FakeMetabase())
    mb.add_switch(20, rsp="51")
    args = argparse.Namespace(account=ACCOUNT, reference=None, date=None, amount="20", kind="any")
    assert Cli(mb).cmd_find(settings, args) == 0
    out = capsys.readouterr().out
    assert "== find_switch_transactions" in out and ACCOUNT in out and "insufficient funds" in out
    assert "== find_internet_banking_transfers: not available" in out and '"T1": "declined"' in out
    assert Cli(mb).cmd_find(settings, argparse.Namespace(account=None, reference=None, date=None, amount=None,
                                                         kind="any")) == 1
