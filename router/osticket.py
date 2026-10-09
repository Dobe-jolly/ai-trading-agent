"""osTicket (OSTICKETS_NEW) through Metabase for the router: tickets filed under catch-all topics (or none), their
form fields, and closed tickets to compare with. Reads and writes are the shared ones, plus the help topic change
(agentcore.osticket.OsTicketBase.set_topic)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, ClassVar, Optional

from agentcore import sql
from agentcore.metabase import MetabaseClient
from agentcore.osticket import (  # noqa: F401 - shared, re-exported for this desk's modules and tests
    OPTIONAL_TOPIC_JOINS,
    TOPIC_JOINS,
    TOPIC_PATH,
    OsTicketBase,
    ThreadEntry,
    TicketState,
    TicketThread,
    FIELDS_SQL,
    SKIP_FIELDS,
    closed_date,
    field_value,
    html_to_text,
    topic_condition,
    topic_matches,
)
from .normalize import now_local, trim_to_none

log = logging.getLogger(__name__)

ESCALATION_NOTE_MARKER = "Routing agent did not move this ticket."
RECOMMENDATION_NOTE_MARKER = "Routing agent recommendation (nothing was changed on the ticket)."
MOVE_NOTE_MARKER = "Routing agent moved this ticket to another help topic."
# Closed tickets compared with: a sample is enough, and it keeps the queries light.
SIMILAR_LIMIT = 200
EXAMPLES_LIMIT = 6
FIRST_MESSAGE = """(SELECT te.body FROM ost_thread th JOIN ost_thread_entry te ON te.thread_id = th.id
      WHERE th.object_id = t.ticket_id AND th.object_type = 'T' AND te.type = 'M' ORDER BY te.id LIMIT 1)"""


def tickets_sql(state: str, topics: list[str], include_no_topic: bool, exclude_topics: list[str],
                since: Optional[str] = None, closed_since: Optional[str] = None) -> str:
    where = [topic_condition(topics)] if topics else []
    if include_no_topic:
        where.append("(t.topic_id = 0 OR ht.topic_id IS NULL)")
    scope = "(" + " OR ".join(where) + ")" if where else "(1 = 0)"
    excluded = f"\n  AND NOT {topic_condition(exclude_topics)}" if exclude_topics else ""
    opened = f"\n  AND t.created >= '{sql.mysql_text(since)}'" if since else ""
    closed = f"\n  AND t.closed >= '{sql.mysql_text(closed_since)} 00:00:00'" if closed_since else ""
    return f"""SELECT t.ticket_id, t.number AS ticket_num, t.created, t.closed AS closed_on, t.topic_id,
    t.staff_id AS assignee_id, cd.subject, {TOPIC_PATH} AS help_topic, o.name AS org_name, 1 AS route_scope
FROM ost_ticket t
JOIN ost_ticket_status s ON (t.status_id = s.id)
LEFT JOIN ost_ticket__cdata cd ON (cd.ticket_id = t.ticket_id)
{OPTIONAL_TOPIC_JOINS}
LEFT JOIN ost_user u ON (u.id = t.user_id)
LEFT JOIN ost_organization o ON (o.id = u.org_id)
WHERE s.state = '{"closed" if state == "closed" else "open"}'
  AND {scope}{excluded}{opened}{closed}
ORDER BY t.number DESC"""


def similar_sql(words: list[str], since: str) -> str:
    """Closed tickets whose subject or first message has every word: how people filed tickets like this one."""
    conditions = []
    for word in words:
        w = sql.like_fragment(sql.mysql_text(word.lower()))
        conditions.append(f"(LOWER(cd.subject) LIKE '%{w}%' OR LOWER({FIRST_MESSAGE}) LIKE '%{w}%')")
    return f"""SELECT t.ticket_id, t.number AS ticket_num, cd.subject, {TOPIC_PATH} AS help_topic, t.closed
FROM ost_ticket t
JOIN ost_ticket_status s ON (t.status_id = s.id)
LEFT JOIN ost_ticket__cdata cd ON (cd.ticket_id = t.ticket_id)
{TOPIC_JOINS}
WHERE s.state = 'closed' AND t.closed >= '{sql.mysql_text(since)}'
  AND {" AND ".join(conditions)}
ORDER BY t.closed DESC
LIMIT {SIMILAR_LIMIT}"""


def examples_sql(topic_id: int, since: str) -> str:
    """Recent tickets filed under one topic, with the start of the first message."""
    return f"""SELECT t.ticket_id, t.number AS ticket_num, cd.subject, t.created, {FIRST_MESSAGE} AS first_message
FROM ost_ticket t
LEFT JOIN ost_ticket__cdata cd ON (cd.ticket_id = t.ticket_id)
WHERE t.topic_id = {sql.integer(topic_id)} AND t.created >= '{sql.mysql_text(since)}'
ORDER BY t.ticket_id DESC
LIMIT {EXAMPLES_LIMIT}"""


@dataclass
class Ticket(TicketThread):
    escalation_marker: ClassVar[str] = ESCALATION_NOTE_MARKER
    ticket_id: int
    number: str
    help_topic: str
    created: str
    topic_id: int = 0
    assignee_id: Optional[int] = None  # the staff member it is assigned to, if any
    subject: Optional[str] = None
    org_name: Optional[str] = None
    topic_match: bool = True
    fields: dict = field(default_factory=dict)
    closed_on: Optional[str] = None
    thread_id: Optional[int] = None
    entries: list[ThreadEntry] = field(default_factory=list)

    def owner_text(self) -> str:
        parts = [self.subject or ""] + [f"{k}: {v}" for k, v in self.fields.items()]
        for e in self.entries:
            if e.type == "M":
                parts.append(e.body)
                parts += [a.summary for a in e.attachments if a.status == "read"]
        return "\n".join(p for p in parts if p)

    def all_text(self) -> str:
        return "\n".join([self.owner_text()] + [e.body for e in self.entries if e.type != "M"])

    def moved_before(self) -> bool:
        """The router has moved this ticket once already (a person may have moved it back)."""
        return any(e.type == "N" and MOVE_NOTE_MARKER in e.body for e in self.entries)

    def has_issue_text(self) -> bool:
        """The owner wrote more than a few words about the problem (subject and fields aside)."""
        words = re.findall(r"[A-Za-z]{2,}", " ".join(e.body for e in self.entries if e.type == "M"))
        return len(words) >= 4


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ticket_from_row(row: dict[str, Any]) -> Ticket:
    assignee = _int_or_none(row.get("assignee_id"))
    return Ticket(
        ticket_id=int(row["ticket_id"]),
        number=str(row.get("ticket_num") or "").strip(),
        help_topic=str(row.get("help_topic") or ""),
        created=str(row.get("created") or ""),
        topic_id=_int_or_none(row.get("topic_id")) or 0,
        assignee_id=assignee or None,
        subject=trim_to_none(row.get("subject")),
        org_name=trim_to_none(row.get("org_name")),
        closed_on=trim_to_none(row.get("closed_on")),
    )


class OsTicket(OsTicketBase):
    escalation_marker: ClassVar[str] = ESCALATION_NOTE_MARKER

    def __init__(self, metabase: MetabaseClient, database_id: int, *, topics: list[str], include_no_topic: bool = True,
                 exclude_topics: Optional[list[str]] = None, days: int = 30) -> None:
        super().__init__(metabase, database_id)
        self.topics = topics
        self.include_no_topic = include_no_topic
        self.exclude_topics = exclude_topics or []
        self.days = days

    def open_tickets(self) -> list[Ticket]:
        since = (now_local() - timedelta(days=self.days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = self.mb.query(self.db, tickets_sql("open", self.topics, self.include_no_topic, self.exclude_topics, since))
        return self._with_fields([_ticket_from_row(r) for r in rows])

    def closed_tickets(self, since: date) -> list[Ticket]:
        rows = self.mb.query(self.db, tickets_sql("closed", self.topics, self.include_no_topic, self.exclude_topics,
                                                  closed_since=since.isoformat()))
        return self._with_fields([_ticket_from_row(r) for r in rows])

    def similar(self, words: list[str], days: int) -> list[dict[str, Any]]:
        since = (now_local() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
        return [{"number": str(r.get("ticket_num") or ""), "subject": trim_to_none(r.get("subject")) or "",
                 "help_topic": str(r.get("help_topic") or ""), "closed": str(r.get("closed") or "")}
                for r in self.mb.query(self.db, similar_sql(words, since))]

    def examples(self, topic_id: int, days: int) -> list[dict[str, Any]]:
        since = (now_local() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
        return [{"number": str(r.get("ticket_num") or ""), "subject": trim_to_none(r.get("subject")) or "",
                 "opened": str(r.get("created") or ""), "message": html_to_text(r.get("first_message"))[:300]}
                for r in self.mb.query(self.db, examples_sql(topic_id, since))]

    def _with_fields(self, tickets: list[Ticket]) -> list[Ticket]:
        if not tickets:
            return tickets
        by_id = {t.ticket_id: t for t in tickets}
        for start in range(0, len(tickets), 200):
            ids = ", ".join(sql.integer(t.ticket_id) for t in tickets[start:start + 200])
            for row in self.mb.query(self.db, FIELDS_SQL.replace("{ticket_ids}", ids)):
                ticket = by_id.get(int(row["ticket_id"]))
                label = trim_to_none(row.get("label")) or trim_to_none(row.get("name"))
                value = field_value(row.get("value"))
                if ticket is None or label is None or value is None:
                    continue
                if (trim_to_none(row.get("name")) or "").lower() in SKIP_FIELDS:
                    continue
                ticket.fields[label] = value
        return tickets
