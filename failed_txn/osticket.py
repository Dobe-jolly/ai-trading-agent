"""osTicket (OSTICKETS_NEW) through Metabase for the failed-transactions desk: which tickets, their form fields,
and what the ticket owner gave (accounts, mobiles, references, amounts). Reads and writes are the shared ones."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar, Optional

from agentcore import sql
from agentcore.metabase import MetabaseClient
from agentcore.osticket import (  # noqa: F401 - shared, re-exported for this desk's modules and tests
    FIELDS_SQL,
    SKIP_FIELDS,
    OPTIONAL_TOPIC_JOINS,
    TOPIC_PATH,
    OsTicketBase,
    ThreadEntry,
    TicketState,
    TicketThread,
    closed_date,
    field_value,
    topic_condition,
    topic_matches,
)
from .normalize import (
    ACCOUNT_RE,
    MOBILE_RE,
    REFERENCE_RE,
    account_key,
    find_amounts,
    mobile_key,
    parse_day,
    reference_key,
    trim_to_none,
)

log = logging.getLogger(__name__)
TOPIC_JOINS = OPTIONAL_TOPIC_JOINS

ESCALATION_NOTE_MARKER = "Failed-transactions agent did not auto-close this ticket."
RECOMMENDATION_NOTE_MARKER = "Failed-transactions AI agent recommendation (not posted to the ticket owner)."

DATE_IN_TEXT = re.compile(r"\b(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|"
                          r"aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{4})\b", re.I)


def tickets_sql(state: str, topics: list[str], exclude_topics: list[str], closed_since: Optional[str] = None) -> str:
    by_topic = topic_condition(topics)
    excluded = f" AND NOT {topic_condition(exclude_topics)}" if exclude_topics else ""
    closed = f"\n  AND t.closed >= '{sql.mysql_text(closed_since)} 00:00:00'" if closed_since else ""
    return f"""SELECT t.ticket_id, t.number AS ticket_num, t.created, t.closed AS closed_on, cd.subject,
    {TOPIC_PATH} AS help_topic, o.name AS org_name, 1 AS topic_match
FROM ost_ticket t
JOIN ost_ticket_status s ON (t.status_id = s.id)
LEFT JOIN ost_ticket__cdata cd ON (cd.ticket_id = t.ticket_id)
{TOPIC_JOINS}
LEFT JOIN ost_user u ON (u.id = t.user_id)
LEFT JOIN ost_organization o ON (o.id = u.org_id)
WHERE s.state = '{"closed" if state == "closed" else "open"}'
  AND {by_topic}{excluded}{closed}
ORDER BY t.number DESC"""


@dataclass
class Details:
    """What the ticket owner gave: keys as the lookups compare them."""
    accounts: set = field(default_factory=set)    # account_key values
    mobiles: set = field(default_factory=set)     # mobile_key values
    references: set = field(default_factory=set)  # reference_key values
    amounts: set = field(default_factory=set)     # Decimal
    dates: set = field(default_factory=set)       # date

    def update(self, other: "Details") -> None:
        for name in ("accounts", "mobiles", "references", "amounts", "dates"):
            getattr(self, name).update(getattr(other, name))

    def __bool__(self) -> bool:
        return bool(self.accounts or self.mobiles or self.references)


def find_details(text: str) -> Details:
    found = Details()
    text = text or ""
    for m in REFERENCE_RE.finditer(text):
        key = reference_key(m.group(1))
        if key and any(c.isdigit() for c in key):
            found.references.add(key)
    for m in DATE_IN_TEXT.finditer(text):
        day = parse_day(m.group(1))
        if day:
            found.dates.add(day)
    found.amounts |= find_amounts(text)
    for m in MOBILE_RE.finditer(text):
        key = mobile_key(m.group(0))
        if key:
            found.mobiles.add(key)
    no_mobiles = MOBILE_RE.sub(" ", text)
    no_dates = re.sub(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b|\b\d{1,2}:\d{2}(?::\d{2})?\b", " ", no_mobiles)
    for m in ACCOUNT_RE.finditer(no_dates):
        raw = re.sub(r"[\s-]", "", m.group(0))
        key = account_key(raw)
        if key and len(key) >= 9:
            found.accounts.add(key)
        if len(raw) == 12:  # a 12-digit number may be an RRN as well as an account
            found.references.add(raw)
    return found


@dataclass
class Ticket(TicketThread):
    escalation_marker: ClassVar[str] = ESCALATION_NOTE_MARKER
    ticket_id: int
    number: str
    help_topic: str
    created: str
    subject: Optional[str] = None
    org_name: Optional[str] = None
    topic_match: bool = True
    fields: dict = field(default_factory=dict)  # form label -> value
    closed_on: Optional[str] = None
    thread_id: Optional[int] = None
    entries: list[ThreadEntry] = field(default_factory=list)

    def owner_text(self) -> str:
        """The form, the subject, the ticket owner's messages and what was read from their files."""
        parts = [self.subject or ""] + [f"{k}: {v}" for k, v in self.fields.items()]
        for e in self.entries:
            if e.type == "M":
                parts.append(e.body)
                parts += [" ".join(a.values) + " " + a.summary for a in e.attachments if a.status == "read"]
        return "\n".join(p for p in parts if p)

    def all_text(self) -> str:
        return "\n".join([self.owner_text()] + [e.body for e in self.entries if e.type != "M"])

    def owner_details(self) -> Details:
        found = find_details(self.owner_text())
        for e in self.entries:  # digits read from the owner's files may be unformatted
            if e.type == "M":
                for a in e.attachments:
                    if a.status == "read":
                        for v in a.values:
                            found.update(find_details(" " + v + " "))
        return found

    def details(self) -> Details:
        """Everything on the ticket, staff notes included."""
        found = find_details(self.all_text())
        found.update(self.owner_details())
        return found


def _ticket_from_row(row: dict[str, Any]) -> Ticket:
    return Ticket(
        ticket_id=int(row["ticket_id"]),
        number=str(row.get("ticket_num") or "").strip(),
        help_topic=str(row.get("help_topic") or ""),
        created=str(row.get("created") or ""),
        subject=trim_to_none(row.get("subject")),
        org_name=trim_to_none(row.get("org_name")),
        topic_match=True,
        closed_on=trim_to_none(row.get("closed_on")),
    )


class OsTicket(OsTicketBase):
    escalation_marker: ClassVar[str] = ESCALATION_NOTE_MARKER

    def __init__(self, metabase: MetabaseClient, database_id: int, *, topics: list[str],
                 exclude_topics: Optional[list[str]] = None) -> None:
        super().__init__(metabase, database_id)
        self.topics = topics
        self.exclude_topics = exclude_topics or []

    def open_tickets(self) -> list[Ticket]:
        rows = self.mb.query(self.db, tickets_sql("open", self.topics, self.exclude_topics))
        return self._with_fields([_ticket_from_row(r) for r in rows])

    def closed_tickets(self, since: date) -> list[Ticket]:
        rows = self.mb.query(self.db, tickets_sql("closed", self.topics, self.exclude_topics, since.isoformat()))
        return self._with_fields([_ticket_from_row(r) for r in rows])

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
