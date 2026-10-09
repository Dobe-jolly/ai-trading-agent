"""Test doubles for the router: the scripted model, and a fake Metabase that answers the router's osTicket SQL
(help topics, catch-all tickets, similar closed tickets, the topic change). All data here is invented."""
from __future__ import annotations

import html
import re
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pytest

from agentcore.osticket import fold_topic, topic_matches
from router.config import BASE_DIR, Settings
from router.normalize import now_local
from tests.ecocash.conftest import (  # noqa: F401 - the shared doubles, re-exported for these tests
    AGENT_STAFF,
    TICKET_DB,
    FakeMetabase as _TicketFake,
    ScriptedModel,
    response,
    text_block,
    tool_block,
)

# (topic_id, topic_pid, topic, flags, notes): flags bit 2 = active, bit 4 = archived
TOPICS = [
    (19, 0, "SMS Alerts", 2, ""),
    (172, 19, "Transactional", 2, ""),
    (122, 0, "HelpDesk / Logging Ticket", 2, ""),
    (123, 0, "HelpDesk / Login", 2, "<p>Staff cannot log in to a bank system</p>"),
    (155, 0, "Failed ZIPIT Transaction", 2, ""),
    (94, 0, "Ecocash / Failed Transactions / ", 2, ""),
    (102, 0, "NMB Connect / OTP", 2, ""),
    (160, 0, "Escalated to Vendor", 2, ""),
    (1, 0, "zzGeneral Inquiry", 0, ""),
    (68, 0, "Security and Loss Control", 0, ""),
    (73, 68, "Access Control", 2, ""),
    (200, 0, "Old Archived Topic", 6, ""),
]
# The desks the router knows of in these tests: (title, topics it works, subtopics it leaves alone)
OWNERS = [("SMS Alerts", ["SMS Alerts"], []),
          ("Failed transactions", ["Failed ZIPIT Transaction", "Ecocash / Failed Transactions"], [])]


def route(action: str, target: str = "", conf: float = 0.95, reply: str = "", alternatives: Optional[list] = None,
          standard: Optional[bool] = None, id_: str = "d") -> Any:
    args = {"action": action, "confidence": conf, "category": "x", "reason": f"Test reason for {action}.",
            "evidence": ["test evidence"], "target_topic": target, "alternatives": alternatives or [],
            "reply_text": reply}
    if standard is not None:
        args["follows_standard_outcome"] = standard
    return response(tool_block("submit_decision", args, id_))


def call(name: str, args: dict[str, Any], id_: str = "c") -> Any:
    return response(tool_block(name, args, id_))


class FakeMetabase(_TicketFake):
    def __init__(self) -> None:
        super().__init__()
        self.topic_rows = {tid: {"topic_id": tid, "topic_pid": pid, "topic": name, "flags": flags, "notes": notes}
                           for tid, pid, name, flags, notes in TOPICS}
        self.staff[AGENT_STAFF] = {"staff_id": AGENT_STAFF, "username": "router", "firstname": "Routing", "lastname": "Agent"}

    def path(self, topic_id: int) -> str:
        parts, row = [], self.topic_rows.get(topic_id)
        while row:
            parts.append(row["topic"])
            row = self.topic_rows.get(row["topic_pid"])
        return " / ".join(reversed(parts))

    def topic_id(self, path: Optional[str]) -> int:
        if not path:
            return 0
        return next(tid for tid in self.topic_rows if fold_topic(self.path(tid)) == fold_topic(path))

    def add_ticket(self, ticket_id: int, number: str, messages: list[str], *, topic: Optional[str] = "HelpDesk / Logging Ticket",
                   subject: str = "Assist", assignee: Optional[int] = None, first_message: Optional[str] = None,
                   **kwargs: Any) -> None:
        tid = self.topic_id(topic)
        super().add_ticket(ticket_id, number, messages, topic=self.path(tid) if tid else "", subject=subject, **kwargs)
        self.tickets[ticket_id].update(topic_id=tid, staff_id=assignee or 0)

    def add_closed(self, ticket_id: int, number: str, subject: str, topic: str, message: str = "",
                   days_ago: int = 20) -> None:
        when = now_local() - timedelta(days=days_ago)
        self.add_ticket(ticket_id, number, [message] if message else [], topic=topic, subject=subject, state="closed",
                        created=when)

    # ---- the router's SQL ----------------------------------------------------------------
    def _ticket_query(self, sql: str) -> list[dict[str, Any]]:
        if sql.startswith("SELECT topic_id, topic_pid, topic, flags, notes FROM ost_help_topic"):
            return [dict(r) for r in self.topic_rows.values()]
        if "GROUP BY t.topic_id" in sql:
            since = re.search(r"t\.created >= '([^']+)'", sql).group(1)
            counts: dict[int, int] = {}
            for t in self.tickets.values():
                if t["created"] >= since:
                    counts[t["topic_id"]] = counts.get(t["topic_id"], 0) + 1
            return [{"topic_id": k, "n": v} for k, v in counts.items()]
        if "1 AS route_scope" in sql:
            return self._route_tickets(sql)
        if "LIMIT 200" in sql and "LOWER(cd.subject) LIKE" in sql:
            words = re.findall(r"LOWER\(cd\.subject\) LIKE '%([^%]+)%'", sql)
            rows = []
            for t in sorted(self.tickets.values(), key=lambda x: x["ticket_id"], reverse=True):
                text = (t["subject"] + " " + self._first_message(t)).lower()
                if t["state"] == "closed" and t["topic_id"] and all(w in text for w in words):
                    rows.append({"ticket_id": t["ticket_id"], "ticket_num": t["ticket_num"], "subject": t["subject"],
                                 "help_topic": t["help_topic"], "closed": t["closed"]})
            return rows
        m = re.search(r"WHERE t\.topic_id = (\d+) AND t\.created >= '([^']+)'", sql)
        if m:
            rows = [t for t in self.tickets.values() if t["topic_id"] == int(m.group(1)) and t["created"] >= m.group(2)]
            rows.sort(key=lambda x: x["ticket_id"], reverse=True)
            return [{"ticket_id": t["ticket_id"], "ticket_num": t["ticket_num"], "subject": t["subject"],
                     "created": t["created"], "first_message": self._first_message(t, raw=True)} for t in rows[:6]]
        if sql.startswith("SELECT topic_id, staff_id FROM ost_ticket WHERE ticket_id"):
            t = self.tickets[int(re.search(r"ticket_id = (\d+)", sql).group(1))]
            return [{"topic_id": t["topic_id"], "staff_id": t["staff_id"]}]
        return super()._ticket_query(sql)

    def _first_message(self, t: dict[str, Any], raw: bool = False) -> str:
        first = next((e["body"] for e in self.thread_entries(t["ticket_id"]) if e["type"] == "M"), "")
        return first if raw else html.unescape(re.sub(r"<[^>]+>", "", first))

    def _route_tickets(self, sql: str) -> list[dict[str, Any]]:
        state = "closed" if "s.state = 'closed'" in sql else "open"
        where = sql.split("WHERE", 1)[1]
        scope, _, rest = where.partition("AND NOT")
        topics = re.findall(r"\) = '([^']+)' OR UPPER", scope)
        excluded = re.findall(r"\) = '([^']+)' OR UPPER", rest)
        no_topic = "t.topic_id = 0 OR ht.topic_id IS NULL" in sql
        since = re.search(r"t\.created >= '([^']+)'", sql)
        rows = []
        for t in sorted(self.tickets.values(), key=lambda x: x["ticket_num"], reverse=True):
            if t["state"] != state:
                continue
            in_scope = (no_topic and not t["topic_id"]) or (t["topic_id"] and topic_matches(t["help_topic"], topics))
            if not in_scope or (excluded and topic_matches(t["help_topic"], excluded)):
                continue
            if since and t["created"] < since.group(1):
                continue
            rows.append({"ticket_id": t["ticket_id"], "ticket_num": t["ticket_num"], "created": t["created"],
                         "closed_on": t["closed"], "topic_id": t["topic_id"], "assignee_id": t["staff_id"],
                         "subject": t["subject"], "help_topic": t["help_topic"], "org_name": t["org_name"],
                         "route_scope": 1})
        return rows

    def write(self, database_id: int, sql: str) -> None:
        m = re.search(r"UPDATE ost_ticket\nSET topic_id = (\d+),.*WHERE ticket_id = (\d+) AND topic_id = (\d+) "
                      r"AND COALESCE\(staff_id, 0\) IN \(([\d, ]+)\)", sql, re.S)
        if m:
            self.writes.append(sql)
            to_id, ticket_id, from_id = (int(x) for x in m.groups()[:3])
            allowed = {int(x) for x in m.group(4).split(",")}
            t = self.tickets[ticket_id]
            if t["topic_id"] == from_id and t["staff_id"] in allowed:
                t.update(topic_id=to_id, help_topic=self.path(to_id))
            return
        super().write(database_id, sql)


@pytest.fixture
def mb() -> FakeMetabase:
    return FakeMetabase()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    base = Settings.from_env(env_file=tmp_path / "missing.env")
    return replace(
        base, anthropic_api_key="test", model="test-model", mode="shadow", confidence_threshold=0.85,
        max_tickets_per_run=25, allow_request_info=True, reply_source="model", kill_switch_file=tmp_path / "STOP",
        state_db=tmp_path / "router.sqlite", reply_staff_id=AGENT_STAFF, close_status_id=3,
        metabase_host="http://mb.test", metabase_api_key="k", recheck_after_hours=24.0, max_info_requests=2,
        attachment_mode="local", portal_users_file=tmp_path / "users.json", max_moves_per_run=20,
        topics=["HelpDesk / Logging Ticket"], include_no_topic=True, never_to=["Escalated to Vendor"], move_to=[],
        topic_descriptions_file=BASE_DIR / "router/topic_descriptions.json",
    )


def days_ago(n: int) -> datetime:
    return now_local() - timedelta(days=n)
