"""osTicket (OSTICKETS_NEW) reads and writes through Metabase, ported from SmsAgentService."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar, Iterable, Optional

from agentcore import sql
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
TOPIC_JOINS = _INNER_TOPIC_JOINS
from .normalize import (
    normalize_account_number,
    parse_timestamp,
    requested_alert_types,
    ticket_account_numbers,
    trim_to_none,
)

log = logging.getLogger(__name__)

# First line of every escalation note. Identical to the Java agent's marker, so a ticket either
# agent has escalated is recognised by both, and a thread is escalated at most once.
ESCALATION_NOTE_MARKER = "SMS Alerts agent did not auto-close this ticket."
# First line of a notes-mode recommendation (the agent advises; staff act).
RECOMMENDATION_NOTE_MARKER = "SMS Alerts AI agent recommendation (not posted to the ticket owner)."

TOPIC_FILTER = (
    f"({TOPIC_PATH} = 'SMS Alerts' OR UPPER({TOPIC_PATH}) LIKE 'SMS ALERTS / %')"
)


FORM_FIELDS = """
    MAX(CASE WHEN (f.label LIKE '%receive the alerts%' OR f.label LIKE '%Phone%') AND f.label NOT LIKE '%account number%' THEN v.value END) AS phone_number,
    MAX(CASE WHEN f.label LIKE '%CIF%' THEN v.value END) AS cif,
    MAX(CASE WHEN LOWER(f.label) = 'account number' OR LOWER(f.label) = 'account number:' THEN v.value END) AS account_number,
    MAX(CASE WHEN LOWER(f.label) LIKE 'account number 2%' THEN v.value END) AS account_number_2,
    MAX(CASE WHEN f.label LIKE '%currency%' THEN v.value END) AS currency,
    MAX(CASE WHEN f.label LIKE '%Salaries%' OR f.label LIKE '%Debits%' THEN v.value END) AS type,
    MAX(CASE WHEN f.name = 'subject' THEN v.value END) AS subject,
    MAX(o.name) AS org_name"""
FORM_JOINS = """JOIN ost_form_entry e ON (e.object_id = t.ticket_id AND e.object_type = 'T')
JOIN ost_form_entry_values v ON (v.entry_id = e.id)
JOIN ost_form_field f ON (v.field_id = f.id)
LEFT JOIN ost_user u ON (u.id = t.user_id)
LEFT JOIN ost_organization o ON (o.id = u.org_id)"""

OPEN_TICKETS_SQL = f"""SELECT
    t.ticket_id,
    t.number AS ticket_num,
    MAX(t.created) AS created,
    MAX({TOPIC_PATH}) AS help_topic,{FORM_FIELDS}
FROM ost_ticket t
JOIN ost_ticket_status s ON (t.status_id = s.id)
{TOPIC_JOINS}
{FORM_JOINS}
WHERE s.state = 'open'
  AND {TOPIC_FILTER}
GROUP BY t.ticket_id
ORDER BY t.number DESC"""

CLOSED_TICKETS_SQL = f"""SELECT
    t.ticket_id,
    t.number AS ticket_num,
    MAX(t.created) AS created,
    MAX({TOPIC_PATH}) AS help_topic,{FORM_FIELDS},
    MAX(t.closed) AS closed_on
FROM ost_ticket t
JOIN ost_ticket_status s ON (t.status_id = s.id)
{TOPIC_JOINS}
{FORM_JOINS}
WHERE s.state = 'closed'
  AND t.closed >= '{{since}} 00:00:00'
  AND {TOPIC_FILTER}
GROUP BY t.ticket_id
ORDER BY t.number DESC"""


def open_tickets_sql(topics: Iterable[str], other_since: Optional[str], exclude: Iterable[str]) -> str:
    """Open tickets under the SMS topics; with other_since, also every other open ticket created since then
    (topic or none), except those under the excluded topics."""
    topics, exclude = list(topics), [x for x in exclude if x.strip()]
    wanted = topic_condition(topics)
    if other_since:
        excluded = f" AND NOT {topic_condition(exclude)}" if exclude else ""
        wanted = f"({wanted} OR (t.created >= '{sql.mysql_text(other_since)}'{excluded}))"
    return f"""SELECT
    t.ticket_id,
    t.number AS ticket_num,
    MAX(t.created) AS created,
    MAX({TOPIC_PATH}) AS help_topic,{FORM_FIELDS}
FROM ost_ticket t
JOIN ost_ticket_status s ON (t.status_id = s.id)
{OPTIONAL_TOPIC_JOINS if other_since else TOPIC_JOINS}
{FORM_JOINS}
WHERE s.state = 'open'
  AND {wanted}
GROUP BY t.ticket_id
ORDER BY t.number DESC"""


@dataclass
class Ticket(TicketThread):
    escalation_marker: ClassVar[str] = ESCALATION_NOTE_MARKER
    ticket_id: int
    number: str
    help_topic: str
    created: str
    phone_number: Optional[str]
    cif: Optional[str]
    account_number: Optional[str]  # normalised (14 digits), as the Java does
    account_number_2: Optional[str]
    currency: Optional[str]
    alert_type_field: Optional[str]
    closed_on: Optional[str] = None
    org_name: Optional[str] = None  # the ticket owner's organisation (usually the branch)
    thread_id: Optional[int] = None
    entries: list[ThreadEntry] = field(default_factory=list)
    subject: Optional[str] = None
    topic_match: bool = True  # filed under an SMS Alerts topic
    triage_kind: Optional[str] = None  # filed elsewhere: sms_alerts | mixed (from the triage read)
    found_by: Optional[str] = None  # why a ticket from another topic was picked up

    @property
    def requested_alert_types(self) -> list[str]:
        return requested_alert_types(self.alert_type_field, self.help_topic)

    @property
    def required_accounts(self) -> list[str]:
        """The accounts the Java agent would check for this ticket."""
        return ticket_account_numbers(self.account_number, self.account_number_2, self.currency)


    def mentions_account(self, account: str) -> bool:
        """True when this (normalised) account appears on the form, in a ticket-owner message, or in a
        file the ticket owner attached to one of their messages."""
        if account in (self.account_number, self.account_number_2):
            return True
        owner_texts = [[self.subject or ""]] + [
            [entry.body] + [v for a in entry.attachments if a.status == "read" for v in a.values]
            for entry in self.entries if entry.type == "M"]
        for texts in owner_texts:
            for text in texts:
                for run in re.findall(r"\d(?:[\s-]?\d){5,}", text):
                    if normalize_account_number(run) == account:
                        return True
        return False


def _ticket_from_row(row: dict[str, Any]) -> Ticket:
    return Ticket(
        ticket_id=int(row["ticket_id"]),
        number=str(row.get("ticket_num") or "").strip(),
        help_topic=str(row.get("help_topic") or ""),
        created=str(row.get("created") or ""),
        phone_number=trim_to_none(row.get("phone_number")),
        cif=trim_to_none(row.get("cif")),
        account_number=normalize_account_number(row.get("account_number")),
        account_number_2=normalize_account_number(row.get("account_number_2")),
        currency=trim_to_none(row.get("currency")),
        alert_type_field=trim_to_none(row.get("type")),
        closed_on=trim_to_none(row.get("closed_on")),
        org_name=trim_to_none(row.get("org_name")),
        subject=trim_to_none(html_to_text(row.get("subject"))),
    )


class OsTicket(OsTicketBase):
    escalation_marker: ClassVar[str] = ESCALATION_NOTE_MARKER

    # ---- reads -------------------------------------------------------------------
    def open_sms_tickets(self) -> list[Ticket]:
        return [_ticket_from_row(r) for r in self.mb.query(self.db, OPEN_TICKETS_SQL)]

    def open_tickets(self, topics: Iterable[str], other_since: Optional[str] = None,
                     exclude: Iterable[str] = ()) -> list[Ticket]:
        """Open tickets under the SMS topics (topic_match), plus, with other_since, every other open ticket
        created since then that is not under an excluded topic."""
        topics, exclude = list(topics), list(exclude)
        tickets = []
        for row in self.mb.query(self.db, open_tickets_sql(topics, other_since, exclude)):
            ticket = _ticket_from_row(row)
            ticket.topic_match = topic_matches(ticket.help_topic, topics)
            if not ticket.topic_match:
                if not other_since or topic_matches(ticket.help_topic, exclude):
                    continue
                created = parse_timestamp(ticket.created)
                since = parse_timestamp(other_since)
                if created and since and created < since:
                    continue
            tickets.append(ticket)
        return tickets

    def last_message_ids(self, ticket_ids: list[int]) -> dict[int, Optional[int]]:
        """Newest message from the ticket owner per ticket (one query): what the triage verdict is keyed on."""
        if not ticket_ids:
            return {}
        ids = ", ".join(sql.integer(i) for i in ticket_ids)
        rows = self.mb.query(self.db, f"""SELECT th.object_id AS ticket_id, MAX(te.id) AS last_message_id
FROM ost_thread th
JOIN ost_thread_entry te ON (te.thread_id = th.id)
WHERE th.object_type = 'T' AND te.type = 'M' AND th.object_id IN ({ids})
GROUP BY th.object_id""")
        found = {int(r["ticket_id"]): (int(r["last_message_id"]) if r.get("last_message_id") is not None else None)
                 for r in rows}
        return {tid: found.get(tid) for tid in ticket_ids}

    def closed_sms_tickets(self, since: date) -> list[Ticket]:
        query = CLOSED_TICKETS_SQL.replace("{since}", sql.mysql_text(since.isoformat()))
        return [_ticket_from_row(r) for r in self.mb.query(self.db, query)]


