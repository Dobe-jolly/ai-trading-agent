"""osTicket (OSTICKETS_NEW) through Metabase for the NMB Connect desk: which tickets, their form fields, and what
the ticket owner gave (CIFs, accounts, mobiles, national IDs). Reads and writes are the shared ones."""
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
    OPTIONAL_TOPIC_JOINS,
    SKIP_FIELDS,
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
    CIF_RE,
    ID_RE,
    MOBILE_RE,
    account_key,
    cif_key,
    id_key,
    mobile_key,
    parse_timestamp,
    trim_to_none,
)

log = logging.getLogger(__name__)
TOPIC_JOINS = OPTIONAL_TOPIC_JOINS

ESCALATION_NOTE_MARKER = "NMB Connect agent did not auto-close this ticket."
RECOMMENDATION_NOTE_MARKER = "NMB Connect AI agent recommendation (not posted to the ticket owner)."
CIF_LABEL = re.compile(r"(?i)\b(cif|customer\s*(no|number))\b")


def tickets_sql(state: str, topics: list[str], exclude_topics: list[str], closed_since: Optional[str] = None) -> str:
    by_topic = topic_condition(topics)
    excluded = f" AND NOT {topic_condition(exclude_topics)}" if exclude_topics else ""
    closed = f"\n  AND t.closed >= '{sql.mysql_text(closed_since)} 00:00:00'" if closed_since else ""
    return f"""SELECT t.ticket_id, t.number AS ticket_num, t.created, t.closed AS closed_on, cd.subject,
    {TOPIC_PATH} AS help_topic, o.name AS org_name, 1 AS connect_scope
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
    cifs: set = field(default_factory=set)      # cif_key values
    accounts: set = field(default_factory=set)  # account_key values
    mobiles: set = field(default_factory=set)   # mobile_key values (last 9 digits)
    ids: set = field(default_factory=set)       # id_key values

    def update(self, other: "Details") -> None:
        for name in ("cifs", "accounts", "mobiles", "ids"):
            getattr(self, name).update(getattr(other, name))

    def __bool__(self) -> bool:
        return bool(self.cifs or self.accounts or self.mobiles or self.ids)


def find_details(text: str) -> Details:
    found = Details()
    text = text or ""
    for m in CIF_RE.finditer(text):
        key = cif_key(m.group(1))
        if key:
            found.cifs.add(key)
    for m in ID_RE.finditer(text):
        key = id_key(m.group(0))
        if key:
            found.ids.add(key)
    no_ids = ID_RE.sub(" ", text)
    for m in MOBILE_RE.finditer(no_ids):
        key = mobile_key(m.group(0))
        if key:
            found.mobiles.add(key)
    no_mobiles = MOBILE_RE.sub(" ", no_ids)
    no_dates = re.sub(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b|\b\d{1,2}:\d{2}(?::\d{2})?\b", " ", no_mobiles)
    for m in ACCOUNT_RE.finditer(no_dates):
        key = account_key(re.sub(r"[\s-]", "", m.group(0)))
        if key and len(key) >= 9:
            found.accounts.add(key)
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

    @property
    def opened(self) -> Any:
        return parse_timestamp(self.created)

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
        for label, value in self.fields.items():  # a "CIF" field holds a bare number
            if CIF_LABEL.search(label):
                key = cif_key(value)
                if key:
                    found.cifs.add(key)
        for e in self.entries:  # digits read from the owner's files may be unformatted
            if e.type == "M":
                for a in e.attachments:
                    if a.status == "read":
                        for v in a.values:
                            found.update(find_details(" " + v + " "))
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
