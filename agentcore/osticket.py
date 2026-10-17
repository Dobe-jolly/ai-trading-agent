"""osTicket (OSTICKETS_NEW) through Metabase: what both agents share.

Reading a ticket's thread and attachments, re-reading its state, and the writes (reply, close,
internal note), all as literal SQL through Metabase with read-back checks, as the Java agents do.
Each agent subclasses OsTicketBase with how it finds its tickets, and gives its Ticket class the
TicketThread methods.
"""
from __future__ import annotations

import base64
import binascii
import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar, Iterable, Optional

from . import sql
from .metabase import MetabaseClient, MetabaseError
from .normalize import parse_timestamp, trim_to_none

log = logging.getLogger(__name__)

TOPIC_PATH = "CONCAT_WS(' / ', ht4.topic, ht3.topic, ht2.topic, ht.topic)"
TOPIC_JOINS = """JOIN ost_help_topic ht ON (t.topic_id = ht.topic_id)
LEFT JOIN ost_help_topic ht2 ON (ht2.topic_id = ht.topic_pid)
LEFT JOIN ost_help_topic ht3 ON (ht3.topic_id = ht2.topic_pid)
LEFT JOIN ost_help_topic ht4 ON (ht4.topic_id = ht3.topic_pid)"""
# Tickets with no help topic (often the ones e-mailed in) are kept when every topic is read.
OPTIONAL_TOPIC_JOINS = TOPIC_JOINS.replace("JOIN ost_help_topic ht ON", "LEFT JOIN ost_help_topic ht ON", 1)
# Files attached to thread entries (type 'H'). Inline images pasted into a message are attachments too.
ATTACHMENTS_SQL = """SELECT a.id AS attach_id, a.object_id AS entry_id, a.inline, COALESCE(NULLIF(a.name, ''), f.name) AS name,
       f.id AS file_id, f.type AS mime, f.size, f.bk, f.signature
FROM ost_attachment a
JOIN ost_file f ON (f.id = a.file_id)
WHERE a.type = 'H' AND a.object_id IN ({entry_ids})
ORDER BY a.object_id, a.id"""
TICKET_STATE_SQL = f"""SELECT t.ticket_id, t.number AS ticket_num, s.state AS status_state, s.name AS status_name,
    {TOPIC_PATH} AS help_topic
FROM ost_ticket t
JOIN ost_ticket_status s ON (t.status_id = s.id)
{OPTIONAL_TOPIC_JOINS}
WHERE t.ticket_id = {{ticket_id}}"""


# osTicket keeps some names with stray spaces or a trailing slash ('Ecocash / Failed Transactions / '); compare
# names without them, as the people who configure the agents write them.
FOLDED_TOPIC_PATH = f"TRIM(TRIM(TRAILING '/' FROM TRIM({TOPIC_PATH})))"


def fold_topic(name: Optional[str]) -> str:
    """A help topic name as compared, the same way as FOLDED_TOPIC_PATH in SQL: case, spaces at the ends and a
    trailing slash do not matter (spaces inside the name do)."""
    text = (name or "").strip().upper()
    while text.endswith("/"):
        text = text[:-1].strip()
    return text


# Form fields captured on tickets (osTicket dynamic forms), by ticket.
FIELDS_SQL = """SELECT e.object_id AS ticket_id, f.label, f.name, v.value
FROM ost_form_entry e
JOIN ost_form_entry_values v ON (v.entry_id = e.id)
JOIN ost_form_field f ON (f.id = v.field_id)
WHERE e.object_type = 'T' AND e.object_id IN ({ticket_ids})
ORDER BY e.object_id, e.id, f.sort"""
SKIP_FIELDS = {"subject", "priority", "message"}


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


def topic_condition(topics: Iterable[str]) -> str:
    """The topic or any of its subtopics, case-insensitive (SQL)."""
    parts = []
    for topic in topics:
        t = sql.mysql_text(fold_topic(topic))
        if t:
            parts.append(f"UPPER({FOLDED_TOPIC_PATH}) = '{t}' OR UPPER({TOPIC_PATH}) LIKE '{sql.like_fragment(t)} / %'")
    return "(" + " OR ".join(parts) + ")" if parts else "(1 = 0)"


def topic_matches(topic: Optional[str], topics: Iterable[str]) -> bool:
    """The same test in Python: the topic is one of these or a subtopic of one."""
    topic = fold_topic(topic)
    return bool(topic) and any(topic == x or topic.startswith(x + " / ") for x in map(fold_topic, topics) if x)


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
class TicketState:
    ticket_id: int
    status_state: str
    status_name: str
    help_topic: str
    last_entry_id: Optional[int]

    @property
    def is_open(self) -> bool:
        return self.status_state.lower() == "open"


class TicketThread:
    """Thread helpers for an agent's Ticket dataclass (which has entries, ticket_id, number, ...)."""
    escalation_marker: ClassVar[str] = "agent did not auto-close this ticket."
    entries: list

    @property
    def last_entry_id(self) -> Optional[int]:
        return max((e.id for e in self.entries), default=None)

    @property
    def last_message_id(self) -> Optional[int]:
        return max((e.id for e in self.entries if e.type == "M"), default=None)

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

    def escalated_by_agent(self, staff_id: Optional[int], marker: Optional[str] = None) -> bool:
        """An agent note carrying the marker exists with no ticket-owner message after it."""
        marker = marker or self.escalation_marker
        notes = [e.id for e in self.entries if e.type == "N" and marker in e.body
                 and (staff_id is None or e.staff_id == staff_id)]
        return bool(notes) and max(notes) > (self.last_message_id or 0)

    def handled_by_staff(self, agent_staff_id: Optional[int]) -> bool:
        """Someone other than the agent has replied or left a note."""
        return any(e.type in ("R", "N") and (agent_staff_id is None or e.staff_id != agent_staff_id)
                   for e in self.entries)

    def answered_since_last_message(self) -> bool:
        """A staff reply exists after the newest owner message (someone is waiting on the owner)."""
        replies = [e.id for e in self.entries if e.type == "R"]
        return bool(replies) and max(replies) > (self.last_message_id or 0)


def closed_date(ticket: Any) -> Optional[date]:
    parsed = parse_timestamp(ticket.closed_on)
    return parsed.date() if parsed else None


class OsTicketBase:
    escalation_marker: ClassVar[str] = TicketThread.escalation_marker

    def __init__(self, metabase: MetabaseClient, database_id: int) -> None:
        self.mb = metabase
        self.db = database_id
        self._blob_encoding = "base64"  # how file chunks are read; "hex" if TO_BASE64 is missing

    def thread_ids(self, tickets: list[Any]) -> dict[str, int]:
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

    def load_thread(self, ticket: Any) -> Any:
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

    def load_attachments(self, ticket: Any) -> list[Attachment]:
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

    def state(self, ticket: Any) -> Optional[TicketState]:
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

    def reopen_states(self, ticket_ids: list[int]) -> dict[int, dict[str, Any]]:
        """For tickets the agent closed: osTicket's last reopen time and the status state now (one query)."""
        if not ticket_ids:
            return {}
        ids = ", ".join(sql.integer(i) for i in ticket_ids)
        query = f"""SELECT t.ticket_id, t.reopened, s.state AS status_state
FROM ost_ticket t
JOIN ost_ticket_status s ON (t.status_id = s.id)
WHERE t.ticket_id IN ({ids})"""
        return {int(r["ticket_id"]): r for r in self.mb.query(self.db, query)}

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
        marker = sql.like_fragment(sql.mysql_text(html.escape(self.escalation_marker, quote=True)))
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

    def owner_contact(self, ticket: Any) -> tuple[Optional[str], Optional[str]]:
        """(name, email) of the user who opened the ticket (usually a branch staff member)."""
        rows = self.mb.query(self.db, f"""SELECT u.name AS user_name, ue.address AS email
FROM ost_ticket t
JOIN ost_user u ON (u.id = t.user_id)
LEFT JOIN ost_user_email ue ON (ue.id = u.default_email_id)
WHERE t.ticket_id = {sql.integer(ticket.ticket_id)}""")
        if not rows:
            return None, None
        return trim_to_none(rows[0].get("user_name")), trim_to_none(rows[0].get("email"))

    def last_staff_reply(self, ticket: Any) -> Optional[str]:
        replies = [e.body for e in ticket.entries if e.type == "R"]
        return replies[-1] if replies else None

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

    def post_reply(self, ticket: Any, staff_id: int, poster: str, message: str) -> None:
        if ticket.thread_id is None:
            raise MetabaseError(f"No thread found for ticket {ticket.number}")
        escaped = sql.mysql_text(html_body(message))
        self._insert_entry(ticket.thread_id, staff_id, poster, "R", escaped)
        if not self._entry_exists(ticket.thread_id, staff_id, "R", escaped):
            raise MetabaseError(f"Reply verification failed for thread {ticket.thread_id}")

    def mark_answered(self, ticket: Any, staff_id: int) -> None:
        """For a reply that keeps the ticket open (request for information)."""
        self.mb.write(self.db, f"""UPDATE ost_ticket
SET isanswered = 1,
    lastupdate = NOW(),
    updated = NOW()
WHERE ticket_id = {sql.integer(ticket.ticket_id)}""")

    def close(self, ticket: Any, staff_id: int, close_status_id: int) -> None:
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

    def set_topic(self, ticket: Any, from_topic_id: int, to_topic_id: int, staff_ids: Iterable[int] = (0,)) -> bool:
        """File the ticket under another help topic, only if it is still under the one it was read under and is not
        assigned to anyone but staff_ids (0 = nobody). True when moved; False when the ticket changed meanwhile."""
        tid = sql.integer(ticket.ticket_id)
        allowed = ", ".join(sql.integer(i) for i in sorted(set(staff_ids) | {0}))
        self.mb.write(self.db, f"""UPDATE ost_ticket
SET topic_id = {sql.integer(to_topic_id)},
    lastupdate = NOW(),
    updated = NOW()
WHERE ticket_id = {tid} AND topic_id = {sql.integer(from_topic_id)} AND COALESCE(staff_id, 0) IN ({allowed})""")
        rows = self.mb.query(self.db, f"SELECT topic_id, staff_id FROM ost_ticket WHERE ticket_id = {tid}")
        if not rows:
            raise MetabaseError(f"Help topic change verification failed for ticket {ticket.ticket_id}")
        try:
            topic, staff = int(rows[0].get("topic_id") or 0), int(rows[0].get("staff_id") or 0)
        except (TypeError, ValueError):
            raise MetabaseError(f"Help topic change verification failed for ticket {ticket.ticket_id}") from None
        if topic == int(to_topic_id):
            return True
        if topic == int(from_topic_id) and str(staff) in allowed.split(", "):
            raise MetabaseError(f"Help topic change did not apply to ticket {ticket.ticket_id}")
        return False  # filed elsewhere or assigned to a person meanwhile

    def post_note(self, ticket: Any, staff_id: int, poster: str, note: str, marker: Optional[str] = None) -> bool:
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
