"""The router in the console (its outcomes and chart) and its commands (check, topics). All data invented."""
from __future__ import annotations

import argparse
from dataclasses import replace

from agentcore.audit import AuditLog
from agentcore.portal.users import UserStore
from nmbz.console import Console
from router import __main__ as cli
from tests.router.conftest import OWNERS


def test_console_words_the_routers_outcomes_and_groups_its_chart(settings, tmp_path):
    users = UserStore(tmp_path / "users.json")
    users.set("dobe", "correct horse battery", "admin")
    s = replace(settings, portal_users_file=tmp_path / "users.json", mode_override_file=tmp_path / "HOLD")
    console = Console({"router": s}, users, stop_file=tmp_path / "STOP")
    vocab = console.get("/api/service", {}, "dobe", "admin")["desks"][0]["vocab"]
    assert list(vocab["actions"]) == ["move", "escalate", "request_info", "no_action"]
    assert vocab["actions"]["escalate"]["label"] == "Suggested to staff"
    audit = AuditLog(s.state_db)
    for i, (action, group) in enumerate([("move", "desk_topic"), ("escalate", "staff_topic"), ("no_action", "stays")]):
        audit.record(run_id="r1", mode="live", ticket_id=i, ticket_number=f"20000{i}", proposed_action=action,
                     final_action=action, confidence=0.9, category=group, reason="r",
                     account_statuses={"topic": "Failed ZIPIT Transaction"} if action == "move" else {})
    o = console.get("/api/d/router/overview", {}, "dobe", "admin")
    assert o["kpis"]["moved"] == 1 and o["kpis"]["escalated"] == 1
    totals = {k: sum(day[k] for day in o["routes_by_day"]) for k in ("desk_topic", "staff_topic", "stays", "unclear")}
    assert totals == {"desk_topic": 1, "staff_topic": 1, "stays": 1, "unclear": 0}
    assert sum(day["move"] for day in o["by_day"]) == 1
    assert o["top_statuses"] == [{"status": "Failed ZIPIT Transaction", "count": 1}]
    assert console.get("/api/d/router/decisions", {"action": "move"}, "dobe", "admin")["total"] == 1


class Cli(cli.RouterCli):
    def __init__(self, mb):
        self.mb = mb

    def build_metabase(self, s):
        return self.mb


def test_check_and_topics(settings, mb, monkeypatch, capsys):
    mb.add_ticket(1, "200001", ["Client gets no OTP"])
    mb.add_ticket(2, "200002", ["Client gets no OTP"], topic=None)
    monkeypatch.setattr(cli, "desk_owners", lambda: OWNERS)
    assert Cli(mb).cmd_check(settings, argparse.Namespace(mode=None, ping_model=False)) == 0
    out = capsys.readouterr().out
    assert "[tickets] catch-alls HelpDesk / Logging Ticket; and tickets with no help topic" in out
    assert "[osticket] 2 open ticket(s) to route (1 with no help topic)" in out
    assert "worked by an agent" in out and out.rstrip().endswith("All good.")
    assert Cli(mb).cmd_check(replace(settings, topics=["HelpDesk / Logging Tickets"]),
                             argparse.Namespace(mode=None, ping_model=False)) == 1
    assert "catch-all not found" in capsys.readouterr().out
    assert Cli(mb).cmd_topics(settings, argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "catch-all         HelpDesk / Logging Ticket" in out
    assert "yes (SMS Alerts)  SMS Alerts / Transactional" in out and "suggested         NMB Connect / OTP" in out
    assert "never             Escalated to Vendor" in out
