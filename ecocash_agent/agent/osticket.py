"""osTicket (OSTICKETS_NEW) through Metabase: finding EcoCash tickets, reading them, writing replies,
closes and internal notes. Writes are literal SQL, as in the SMS agent (Metabase runs native
queries through executeQuery, which MySQL refuses for parameterised INSERT/UPDATE)."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar, Iterable, Optional

from agentcore import sql
from agentcore.metabase import MetabaseClient
from agentcore.osticket import (  # noqa: F401 - shared, re-exported for this agent's modules and tests
    ATTACHMENTS_SQL,
    OPTIONAL_TOPIC_JOINS,
    TICKET_STATE_SQL,
    TOPIC_JOINS as _INNER_TOPIC_JOINS,
    TOPIC_PATH,
    Attachment,
    OsTicketBase,
    ThreadEntry,
    TicketState,
    TicketThread,
    closed_date,
    html_body,
    html_to_text,
    topic_condition,
    topic_matches,
)
TOPIC_JOINS = OPTIONAL_TOPIC_JOINS  # EcoCash tickets may have no topic
from .normalize import ACCOUNT_RE, ID_RE, MOBILE_RE, account_key, id_key, mobile_key, trim_to_none

log = logging.getLogger(__name__)

# First line of every escalation note: a thread is escalated at most once, and the agent leaves
# a ticket alone after escalating it until the ticket owner writes again.
ESCALATION_NOTE_MARKER = "EcoCash agent did not auto-close this ticket."
# First line of a notes-mode recommendation (the agent advises; staff act).
RECOMMENDATION_NOTE_MARKER = "EcoCash AI agent recommendation (not posted to the ticket owner)."

FIELDS_SQL = """SELECT e.object_id AS ticket_id, f.label, f.name, v.value
FROM ost_form_entry e
JOIN ost_form_entry_values v ON (v.entry_id = e.id)
JOIN ost_form_field f ON (f.id = v.field_id)
WHERE e.object_type = 'T' AND e.object_id IN ({ticket_ids})
ORDER BY e.object_id, e.id, f.sort"""
SKIP_FIELDS = {"subject", "priority", "message"}


def keyword_condition(keywords: Iterable[str]) -> str:
    """The subject, or a message from the ticket owner, mentions one of the keywords."""
    parts = []
    for keyword in keywords:
        k = sql.like_fragment(sql.mysql_text(keyword.strip().lower()))
        if k:
            parts.append(f"LOWER(cd.subject) LIKE '%{k}%'")
            parts.append("EXISTS (SELECT 1 FROM ost_thread th JOIN ost_thread_entry te ON (te.thread_id = th.id) "
                         f"WHERE th.object_id = t.ticket_id AND th.object_type = 'T' AND te.type = 'M' "
                         f"AND LOWER(te.body) LIKE '%{k}%')")
    return "(" + " OR ".join(parts) + ")" if parts else "(1 = 0)"


def tickets_sql(state: str, topics: list[str], keywords: list[str], exclude_topics: list[str],
                content_since: Optional[str], closed_since: Optional[str] = None) -> str:
    """Open (or recently closed) tickets under the EcoCash topics, plus (open only) tickets under other
    topics whose content mentions EcoCash, created after content_since."""
    by_topic = topic_condition(topics)
    wanted = by_topic
    if state == "open" and keywords and content_since:
        wanted = (f"({by_topic} OR (t.created >= '{sql.mysql_text(content_since)}' AND "
                  f"{keyword_condition(keywords)}))")
    excluded = f" AND NOT {topic_condition(exclude_topics)}" if exclude_topics else ""
    closed = f"\n  AND t.closed >= '{sql.mysql_text(closed_since)} 00:00:00'" if closed_since else ""
    return f"""SELECT t.ticket_id, t.number AS ticket_num, t.created, t.closed AS closed_on, cd.subject,
    {TOPIC_PATH} AS help_topic, o.name AS org_name,
    CASE WHEN {by_topic} THEN 1 ELSE 0 END AS topic_match
FROM ost_ticket t
JOIN ost_ticket_status s ON (t.status_id = s.id)
LEFT JOIN ost_ticket__cdata cd ON (cd.ticket_id = t.ticket_id)
{TOPIC_JOINS}
LEFT JOIN ost_user u ON (u.id = t.user_id)
LEFT JOIN ost_organization o ON (o.id = u.org_id)
WHERE s.state = '{"closed" if state == "closed" else "open"}'
  AND {wanted}{excluded}{closed}
ORDER BY t.number DESC"""


def field_value(raw: Any) -> Optional[str]:
    """osTicket stores choice fields as JSON ({"3": "Harare Main"} or a list); plain text as is."""
    text = trim_to_none(raw)
    if text is None:
        return None
    if text[:1] in "{[":
        try:
            value = json.loads(text)
        except ValueError:
            return text
        if isinstance(value, dict):
            return ", ".join(str(v) for v in value.values() if v not in (None, "")) or None
        if isinstance(value, list):
            return ", ".join(str(v) for v in value if v not in (None, "")) or None
    return text


@dataclass
class Identifiers:
    mobiles: set = field(default_factory=set)  # mobile_key values
    accounts: set = field(default_factory=set)  # account_key values
    ids: set = field(default_factory=set)  # id_key values

    def update(self, other: "Identifiers") -> None:
        self.mobiles |= other.mobiles
        self.accounts |= other.accounts
        self.ids |= other.ids

    def __bool__(self) -> bool:
        return bool(self.mobiles or self.accounts or self.ids)


def find_identifiers(text: str) -> Identifiers:
    """Mobiles, accounts and national IDs in free text. Dates and times are not mistaken for numbers."""
    found = Identifiers()
    text = text or ""
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
        key = account_key(m.group(0))
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
    org_name: Optional[str] = None  # the ticket owner's organisation (usually the branch)
    topic_match: bool = True  # False: picked up because the content mentions EcoCash
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
        parts = [self.owner_text()] + [e.body for e in self.entries if e.type != "M"]
        return "\n".join(parts)

    def owner_identifiers(self) -> Identifiers:
        found = find_identifiers(self.owner_text())
        for e in self.entries:  # digits read from the owner's files may be unformatted
            if e.type == "M":
                for a in e.attachments:
                    if a.status == "read":
                        for v in a.values:
                            found.update(find_identifiers(" " + v + " "))
        return found

    def identifiers(self) -> Identifiers:
        """Everything on the ticket, staff notes included."""
        found = find_identifiers(self.all_text())
        found.update(self.owner_identifiers())
        return found


def _ticket_from_row(row: dict[str, Any]) -> Ticket:
    return Ticket(
        ticket_id=int(row["ticket_id"]),
        number=str(row.get("ticket_num") or "").strip(),
        help_topic=str(row.get("help_topic") or ""),
        created=str(row.get("created") or ""),
        subject=trim_to_none(row.get("subject")),
        org_name=trim_to_none(row.get("org_name")),
        topic_match=str(row.get("topic_match") or "0") in ("1", "True", "true"),
        closed_on=trim_to_none(row.get("closed_on")),
    )


class OsTicket(OsTicketBase):
    escalation_marker: ClassVar[str] = ESCALATION_NOTE_MARKER
    def __init__(self, metabase: MetabaseClient, database_id: int, *, topics: Optional[list[str]] = None,
                 keywords: Optional[list[str]] = None, exclude_topics: Optional[list[str]] = None,
                 content_days: int = 30) -> None:
        super().__init__(metabase, database_id)
        self.topics = topics if topics is not None else ["EcoCash"]
        self.keywords = keywords if keywords is not None else ["ecocash", "eco cash"]
        self.exclude_topics = exclude_topics if exclude_topics is not None else ["SMS Alerts"]
        self.content_days = content_days

    # ---- reads -------------------------------------------------------------------
    def open_tickets(self) -> list[Ticket]:
        from datetime import datetime, timedelta

        since = (datetime.now() - timedelta(days=self.content_days)).strftime("%Y-%m-%d 00:00:00")
        tickets = [_ticket_from_row(r) for r in self.mb.query(
            self.db, tickets_sql("open", self.topics, self.keywords, self.exclude_topics, since))]
        return self._with_fields(tickets)

    def closed_tickets(self, since: date) -> list[Ticket]:
        tickets = [_ticket_from_row(r) for r in self.mb.query(
            self.db, tickets_sql("closed", self.topics, [], self.exclude_topics, None, closed_since=since.isoformat()))]
        return self._with_fields(tickets)

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


