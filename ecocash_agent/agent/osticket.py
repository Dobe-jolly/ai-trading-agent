"""osTicket (OSTICKETS_NEW) through Metabase: finding EcoCash tickets, reading them, writing replies,
closes and internal notes. Writes are literal SQL, as in the SMS agent (Metabase runs native
queries through executeQuery, which MySQL refuses for parameterised INSERT/UPDATE)."""
from __future__ import annotations

import base64
import binascii
import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Optional

from . import sql
from .metabase import MetabaseClient, MetabaseError
from .normalize import ACCOUNT_RE, ID_RE, MOBILE_RE, account_key, id_key, mobile_key, parse_timestamp, trim_to_none

log = logging.getLogger(__name__)

# First line of every escalation note: a thread is escalated at most once, and the agent leaves
# a ticket alone after escalating it until the ticket owner writes again.
ESCALATION_NOTE_MARKER = "EcoCash agent did not auto-close this ticket."
# First line of a notes-mode recommendation (the agent advises; staff act).
RECOMMENDATION_NOTE_MARKER = "EcoCash AI agent recommendation (not posted to the ticket owner)."

TOPIC_PATH = "CONCAT_WS(' / ', ht4.topic, ht3.topic, ht2.topic, ht.topic)"
TOPIC_JOINS = """LEFT JOIN ost_help_topic ht ON (t.topic_id = ht.topic_id)
LEFT JOIN ost_help_topic ht2 ON (ht2.topic_id = ht.topic_pid)
LEFT JOIN ost_help_topic ht3 ON (ht3.topic_id = ht2.topic_pid)
LEFT JOIN ost_help_topic ht4 ON (ht4.topic_id = ht3.topic_pid)"""
# Files attached to thread entries (type 'H'). Inline images pasted into a message are attachments too.
ATTACHMENTS_SQL = """SELECT a.id AS attach_id, a.object_id AS entry_id, a.inline, COALESCE(NULLIF(a.name, ''), f.name) AS name,
       f.id AS file_id, f.type AS mime, f.size, f.bk, f.signature
FROM ost_attachment a
JOIN ost_file f ON (f.id = a.file_id)
WHERE a.type = 'H' AND a.object_id IN ({entry_ids})
ORDER BY a.object_id, a.id"""
FIELDS_SQL = """SELECT e.object_id AS ticket_id, f.label, f.name, v.value
FROM ost_form_entry e
JOIN ost_form_entry_values v ON (v.entry_id = e.id)
JOIN ost_form_field f ON (f.id = v.field_id)
WHERE e.object_type = 'T' AND e.object_id IN ({ticket_ids})
ORDER BY e.object_id, e.id, f.sort"""
SKIP_FIELDS = {"subject", "priority", "message"}


def topic_condition(topics: Iterable[str]) -> str:
    """The topic or any of its subtopics (case-insensitive)."""
    parts = []
    for topic in topics:
        t = sql.mysql_text(topic.strip().upper())
        if t:
            parts.append(f"UPPER({TOPIC_PATH}) = '{t}' OR UPPER({TOPIC_PATH}) LIKE '{sql.like_fragment(t)} / %'")
    return "(" + " OR ".join(parts) + ")" if parts else "(1 = 0)"


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


TICKET_STATE_SQL = f"""SELECT t.ticket_id, t.number AS ticket_num, s.state AS status_state, s.name AS status_name,
    {TOPIC_PATH} AS help_topic
FROM ost_ticket t
JOIN ost_ticket_status s ON (t.status_id = s.id)
{TOPIC_JOINS}
WHERE t.ticket_id = {{ticket_id}}"""


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


def html_to_text(body: Any) -> str:
    if not body:
        return ""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", str(body))
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_body(message: str) -> str:
    """Java htmlReplyBody: '<p>' + escaped + '</p>'; newlines become <br />."""
    return "<p>" + html.escape(message, quote=True).replace("\r\n", "\n").replace("\n", "<br />") + "</p>"


@dataclass
class Attachment:
    """A file attached to a thread entry (ost_attachment -> ost_file). The reader fills the rest."""
    attach_id: int
    entry_id: int
    file_id: int
    name: str
    mime: str
    size: int
    backend: str  # ost_file.bk: 'D' = content in ost_file_chunk (osTicket's default)
    inline: bool  # pasted into the message body (screenshots, but also e-mail signature logos)
    signature: str = ""  # ost_file.signature: content hash, used to spot the same file twice
    number: int = 0  # 1, 2, ... in thread order, as the model sees it
    status: str = "pending"  # read | skipped | not_read | failed
    note: str = ""  # why it was skipped / not read / failed
    kind: str = ""  # what it is, e.g. "Screenshot of SMS messages on a phone"
    summary: str = ""  # what the agent is shown (real values; masked with the rest of the ticket)
    values: list[str] = field(default_factory=list)  # digit strings read from it (owner-supplied check)


@dataclass
class ThreadEntry:
    id: int
    type: str  # M = message from the ticket owner, R = staff reply, N = internal note
    staff_id: Optional[int]
    poster: str
    body: str
    created: str
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class Ticket:
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

    @property
    def last_entry_id(self) -> Optional[int]:
        return max((e.id for e in self.entries), default=None)

    @property
    def last_message_id(self) -> Optional[int]:
        return max((e.id for e in self.entries if e.type == "M"), default=None)

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

    @property
    def attachments(self) -> list[Attachment]:
        return [a for e in self.entries for a in e.attachments]

    def agent_replies(self, staff_id: Optional[int]) -> int:
        """How many replies the agent's staff account has already posted on this ticket."""
        if staff_id is None:
            return 0
        return sum(1 for e in self.entries if e.type == "R" and e.staff_id == staff_id)

    def last_agent_reply(self, staff_id: Optional[int]) -> Optional["ThreadEntry"]:
        replies = [e for e in self.entries if e.type == "R" and staff_id is not None and e.staff_id == staff_id]
        return replies[-1] if replies else None

    def escalated_by_agent(self, staff_id: Optional[int], marker: str = ESCALATION_NOTE_MARKER) -> bool:
        """An agent note carrying the marker exists with no ticket-owner message after it."""
        notes = [e.id for e in self.entries if e.type == "N" and marker in e.body
                 and (staff_id is None or e.staff_id == staff_id)]
        return bool(notes) and max(notes) > (self.last_message_id or 0)

    def answered_since_last_message(self) -> bool:
        """A staff reply exists after the newest owner message (someone is waiting on the owner)."""
        replies = [e.id for e in self.entries if e.type == "R"]
        return bool(replies) and max(replies) > (self.last_message_id or 0)


@dataclass
class TicketState:
    ticket_id: int
    status_state: str
    status_name: str
    help_topic: str
    last_entry_id: Optional[int]

    @property
    def is_open(self) -> bool:
        return self.status_state.lower() == "open"


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


class OsTicket:
    def __init__(self, metabase: MetabaseClient, database_id: int, *, topics: Optional[list[str]] = None,
                 keywords: Optional[list[str]] = None, exclude_topics: Optional[list[str]] = None,
                 content_days: int = 30) -> None:
        self.mb = metabase
        self.db = database_id
        self.topics = topics if topics is not None else ["EcoCash"]
        self.keywords = keywords if keywords is not None else ["ecocash", "eco cash"]
        self.exclude_topics = exclude_topics if exclude_topics is not None else ["SMS Alerts"]
        self.content_days = content_days
        self._blob_encoding = "base64"  # how file chunks are read; "hex" if TO_BASE64 is missing

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

    def thread_ids(self, tickets: list[Ticket]) -> dict[str, int]:
        """As the Java agents: prefer the ticket ('T') thread over a 'C' thread."""
        if not tickets:
            return {}
        numbers = ", ".join(f"'{sql.mysql_text(t.number)}'" for t in tickets)  # as the Java: quoted, escaped
        query = f"""SELECT t.ticket_id, t.number AS ticket_num, th.id AS thread_id, th.object_type
FROM ost_ticket t
JOIN ost_thread th
  ON (
      (th.object_id = t.ticket_id AND th.object_type = 'T')
      OR (
          th.object_type = 'C'
          AND th.extra IS NOT NULL
          AND th.extra LIKE CONCAT('%\\"number\\":\\"', t.number, '\\"%')
      )
  )
WHERE t.number IN ({numbers})"""
        chosen: dict[str, tuple[int, str]] = {}
        for row in self.mb.query(self.db, query):
            number = str(row.get("ticket_num"))
            current = (int(row["thread_id"]), str(row.get("object_type")))
            existing = chosen.get(number)
            if existing is None or (existing[1] != "T" and current[1] == "T"):
                chosen[number] = current
        return {number: thread[0] for number, thread in chosen.items()}

    def load_thread(self, ticket: Ticket) -> Ticket:
        if ticket.thread_id is None:
            ticket.thread_id = self.thread_ids([ticket]).get(ticket.number)
        if ticket.thread_id is None:
            ticket.entries = []
            return ticket
        query = f"""SELECT id, type, staff_id, poster, body, created
FROM ost_thread_entry
WHERE thread_id = {sql.integer(ticket.thread_id)}
ORDER BY created ASC, id ASC"""
        ticket.entries = [
            ThreadEntry(
                id=int(r["id"]),
                type=str(r.get("type") or ""),
                staff_id=int(r["staff_id"]) if r.get("staff_id") not in (None, "") else None,
                poster=str(r.get("poster") or ""),
                body=html_to_text(r.get("body")),
                created=str(r.get("created") or ""),
            )
            for r in self.mb.query(self.db, query)
        ]
        return ticket

    def load_attachments(self, ticket: Ticket) -> list[Attachment]:
        """Attach the files listed for each thread entry (content is fetched separately)."""
        ids = [e.id for e in ticket.entries]
        if not ids:
            return []
        query = ATTACHMENTS_SQL.replace("{entry_ids}", ", ".join(sql.integer(i) for i in ids))
        by_entry = {e.id: e for e in ticket.entries}
        found: list[Attachment] = []
        for e in ticket.entries:
            e.attachments = []
        for r in self.mb.query(self.db, query):
            entry = by_entry.get(int(r["entry_id"]))
            if entry is None:
                continue
            item = Attachment(
                attach_id=int(r["attach_id"]), entry_id=entry.id, file_id=int(r["file_id"]),
                name=str(r.get("name") or "unnamed"), mime=str(r.get("mime") or "").lower(),
                size=int(r.get("size") or 0), backend=str(r.get("bk") or "D").upper(),
                inline=str(r.get("inline") or "0") in ("1", "True", "true"), signature=str(r.get("signature") or ""),
            )
            entry.attachments.append(item)
            found.append(item)
        position = {e.id: i for i, e in enumerate(ticket.entries)}
        found.sort(key=lambda a: (position[a.entry_id], a.attach_id))
        for entry in ticket.entries:
            entry.attachments.sort(key=lambda a: a.attach_id)
        for n, item in enumerate(found, 1):
            item.number = n
        return found

    def file_content(self, file_id: int, expected_size: int) -> bytes:
        """The bytes of a file kept in the database (ost_file_chunk), read chunk by chunk.

        The SQL encodes each chunk as base64 (TO_BASE64), so the content survives Metabase's
        JSON unchanged; databases without TO_BASE64 fall back to HEX."""
        fid = sql.integer(file_id)
        chunks = self.mb.query(self.db, f"SELECT chunk_id, LENGTH(filedata) AS n FROM ost_file_chunk "
                                        f"WHERE file_id = {fid} ORDER BY chunk_id")
        if not chunks:
            raise MetabaseError("the file has no content in ost_file_chunk")
        parts: list[bytes] = []
        for chunk in chunks:
            cid = sql.integer(chunk["chunk_id"])
            parts.append(self._chunk(fid, cid))
            if len(parts[-1]) != int(chunk.get("n") or 0):
                raise MetabaseError(f"chunk {cid} came back with {len(parts[-1])} of {chunk.get('n')} bytes")
        data = b"".join(parts)
        if expected_size and len(data) != expected_size:
            raise MetabaseError(f"read {len(data)} bytes but osTicket records {expected_size}")
        return data

    def _chunk(self, fid: str, cid: str) -> bytes:
        where = f"FROM ost_file_chunk WHERE file_id = {fid} AND chunk_id = {cid}"
        if self._blob_encoding == "base64":
            try:
                rows = self.mb.query(self.db, f"SELECT TO_BASE64(filedata) AS data {where}")
                return base64.b64decode("".join(str(rows[0]["data"] or "").split())) if rows else b""
            except MetabaseError as exc:
                if "to_base64" not in str(exc).lower():
                    raise
                self._blob_encoding = "hex"  # e.g. an old MySQL without TO_BASE64
        rows = self.mb.query(self.db, f"SELECT HEX(filedata) AS data {where}")
        try:
            return bytes.fromhex(str(rows[0]["data"] or "")) if rows else b""
        except (ValueError, binascii.Error) as exc:
            raise MetabaseError(f"could not decode chunk: {exc}") from exc

    def state(self, ticket: Ticket) -> Optional[TicketState]:
        rows = self.mb.query(self.db, TICKET_STATE_SQL.replace("{ticket_id}", sql.integer(ticket.ticket_id)))
        if not rows:
            return None
        last_entry = None
        if ticket.thread_id is not None:
            entry_rows = self.mb.query(
                self.db, f"SELECT MAX(id) AS last_id FROM ost_thread_entry WHERE thread_id = {sql.integer(ticket.thread_id)}"
            )
            if entry_rows and entry_rows[0].get("last_id") is not None:
                last_entry = int(entry_rows[0]["last_id"])
        row = rows[0]
        return TicketState(
            ticket_id=int(row["ticket_id"]),
            status_state=str(row.get("status_state") or ""),
            status_name=str(row.get("status_name") or ""),
            help_topic=str(row.get("help_topic") or ""),
            last_entry_id=last_entry,
        )

    def staff_poster_name(self, staff_id: int) -> str:
        rows = self.mb.query(
            self.db, f"SELECT staff_id, username, firstname, lastname FROM ost_staff WHERE staff_id = {sql.integer(staff_id)}"
        )
        if not rows:
            raise MetabaseError(f"Staff id {staff_id} was not found in the ticket database")
        staff = rows[0]
        parts = [p for p in (trim_to_none(staff.get("firstname")), trim_to_none(staff.get("lastname"))) if p]
        return " ".join(parts) if parts else (trim_to_none(staff.get("username")) or f"staff {staff_id}")

    def outcomes(self, ticket_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Current state, last staff reply and escalation-note count per ticket (for shadow comparison)."""
        if not ticket_ids:
            return {}
        ids = ", ".join(sql.integer(i) for i in ticket_ids)
        marker = sql.like_fragment(sql.mysql_text(html.escape(ESCALATION_NOTE_MARKER, quote=True)))
        query = f"""SELECT t.ticket_id, t.number AS ticket_num, s.state AS status_state, t.closed,
    (SELECT te.body FROM ost_thread th JOIN ost_thread_entry te ON te.thread_id = th.id
      WHERE th.object_id = t.ticket_id AND th.object_type = 'T' AND te.type = 'R'
      ORDER BY te.id DESC LIMIT 1) AS last_reply,
    (SELECT COUNT(*) FROM ost_thread th JOIN ost_thread_entry te ON te.thread_id = th.id
      WHERE th.object_id = t.ticket_id AND th.object_type = 'T' AND te.type = 'N'
        AND te.body LIKE '%{marker}%') AS escalation_notes
FROM ost_ticket t
JOIN ost_ticket_status s ON (t.status_id = s.id)
WHERE t.ticket_id IN ({ids})"""
        result: dict[int, dict[str, Any]] = {}
        for row in self.mb.query(self.db, query):
            result[int(row["ticket_id"])] = {
                "state": str(row.get("status_state") or ""),
                "closed": row.get("closed"),
                "last_reply": html_to_text(row.get("last_reply")) or None,
                "escalated": int(row.get("escalation_notes") or 0) > 0,
            }
        return result

    def owner_contact(self, ticket: Ticket) -> tuple[Optional[str], Optional[str]]:
        """(name, email) of the user who opened the ticket (usually a branch staff member)."""
        rows = self.mb.query(self.db, f"""SELECT u.name AS user_name, ue.address AS email
FROM ost_ticket t
JOIN ost_user u ON (u.id = t.user_id)
LEFT JOIN ost_user_email ue ON (ue.id = u.default_email_id)
WHERE t.ticket_id = {sql.integer(ticket.ticket_id)}""")
        if not rows:
            return None, None
        return trim_to_none(rows[0].get("user_name")), trim_to_none(rows[0].get("email"))

    def last_staff_reply(self, ticket: Ticket) -> Optional[str]:
        replies = [e.body for e in ticket.entries if e.type == "R"]
        return replies[-1] if replies else None

    # ---- writes (as the SMS agent, with read-back verification) --------
    def _entry_exists(self, thread_id: int, staff_id: int, kind: str, escaped_body: str) -> bool:
        query = f"""SELECT id FROM ost_thread_entry
WHERE thread_id = {sql.integer(thread_id)}
  AND staff_id = {sql.integer(staff_id)}
  AND type = '{kind}'
  AND body = '{escaped_body}'
ORDER BY id DESC
LIMIT 1"""
        return bool(self.mb.query(self.db, query))

    def _insert_entry(self, thread_id: int, staff_id: int, poster: str, kind: str, escaped_body: str) -> None:
        query = f"""INSERT INTO ost_thread_entry (
    pid, thread_id, staff_id, user_id, type, flags, poster, source,
    title, body, format, ip_address, recipients, created, updated
) VALUES (
    0, {sql.integer(thread_id)}, {sql.integer(staff_id)}, 0, '{kind}', 0, '{sql.mysql_text(poster)}', '',
    NULL, '{escaped_body}', 'html', '', NULL, NOW(), NOW()
)"""
        self.mb.write(self.db, query)

    def post_reply(self, ticket: Ticket, staff_id: int, poster: str, message: str) -> None:
        if ticket.thread_id is None:
            raise MetabaseError(f"No thread found for ticket {ticket.number}")
        escaped = sql.mysql_text(html_body(message))
        self._insert_entry(ticket.thread_id, staff_id, poster, "R", escaped)
        if not self._entry_exists(ticket.thread_id, staff_id, "R", escaped):
            raise MetabaseError(f"Reply verification failed for thread {ticket.thread_id}")

    def mark_answered(self, ticket: Ticket, staff_id: int) -> None:
        """For a reply that keeps the ticket open (request for information)."""
        self.mb.write(self.db, f"""UPDATE ost_ticket
SET isanswered = 1,
    lastupdate = NOW(),
    updated = NOW()
WHERE ticket_id = {sql.integer(ticket.ticket_id)}""")

    def close(self, ticket: Ticket, staff_id: int, close_status_id: int) -> None:
        self.mb.write(self.db, f"""UPDATE ost_ticket
SET status_id = {sql.integer(close_status_id)},
    closed = NOW(),
    lastupdate = NOW(),
    updated = NOW(),
    isanswered = 1,
    staff_id = {sql.integer(staff_id)}
WHERE ticket_id = {sql.integer(ticket.ticket_id)}""")
        rows = self.mb.query(
            self.db, f"SELECT status_id, staff_id, closed FROM ost_ticket WHERE ticket_id = {sql.integer(ticket.ticket_id)}"
        )
        if not rows:
            raise MetabaseError(f"Ticket verification failed for ticket {ticket.ticket_id}")
        row = rows[0]
        try:
            ok = (int(row.get("status_id")) == close_status_id and int(row.get("staff_id")) == staff_id
                  and row.get("closed") is not None)
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise MetabaseError(f"Ticket close verification failed for ticket {ticket.ticket_id}")

    def post_note(self, ticket: Ticket, staff_id: int, poster: str, note: str, marker: Optional[str] = None) -> bool:
        """Internal note ('N'). Returns False when skipped as a duplicate (marker or exact body)."""
        if ticket.thread_id is None:
            raise MetabaseError(f"No thread found for ticket {ticket.number}")
        escaped = sql.mysql_text(html_body(note))
        if marker:
            escaped_marker = sql.like_fragment(sql.mysql_text(html.escape(marker, quote=True)))
            query = f"""SELECT id FROM ost_thread_entry
WHERE thread_id = {sql.integer(ticket.thread_id)}
  AND staff_id = {sql.integer(staff_id)}
  AND type = 'N'
  AND body LIKE '%{escaped_marker}%'
ORDER BY id DESC
LIMIT 1"""
            if self.mb.query(self.db, query):
                log.info("Thread %s already has an escalation note from this agent; skipping", ticket.thread_id)
                return False
        if self._entry_exists(ticket.thread_id, staff_id, "N", escaped):
            log.info("Identical note already on thread %s; skipping", ticket.thread_id)
            return False
        self._insert_entry(ticket.thread_id, staff_id, poster, "N", escaped)
        if not self._entry_exists(ticket.thread_id, staff_id, "N", escaped):
            raise MetabaseError(f"Internal note verification failed for thread {ticket.thread_id}")
        return True


def closed_date(ticket: Ticket) -> Optional[date]:
    parsed = parse_timestamp(ticket.closed_on)
    return parsed.date() if parsed else None
