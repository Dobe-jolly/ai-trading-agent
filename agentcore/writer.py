"""Writing to osTicket: the Java agent's write path (native SQL through Metabase, read-back verified)."""
from __future__ import annotations

from typing import Optional

from .osticket import OsTicketBase as OsTicket
from typing import Any as Ticket


class TicketWriter:
    name = "base"

    def post_reply(self, ticket: Ticket, text: str) -> None:
        raise NotImplementedError

    def close(self, ticket: Ticket) -> None:
        raise NotImplementedError

    def mark_answered(self, ticket: Ticket) -> None:
        raise NotImplementedError

    def note(self, ticket: Ticket, text: str, marker: Optional[str] = None) -> bool:
        raise NotImplementedError

    def set_topic(self, ticket: Ticket, from_topic_id: int, to_topic_id: int) -> bool:
        raise NotImplementedError


class ShadowWriter(TicketWriter):
    """Changes nothing; remembers what it would have done (tests and dry runs)."""

    name = "shadow"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def post_reply(self, ticket: Ticket, text: str) -> None:
        self.calls.append(("reply", ticket.number, text))

    def close(self, ticket: Ticket) -> None:
        self.calls.append(("close", ticket.number, ""))

    def mark_answered(self, ticket: Ticket) -> None:
        self.calls.append(("answered", ticket.number, ""))

    def note(self, ticket: Ticket, text: str, marker: Optional[str] = None) -> bool:
        self.calls.append(("note", ticket.number, text))
        return True

    def set_topic(self, ticket: Ticket, from_topic_id: int, to_topic_id: int) -> bool:
        self.calls.append(("topic", ticket.number, f"{from_topic_id} -> {to_topic_id}"))
        return True


class OsTicketWriter(TicketWriter):
    name = "osticket"

    def __init__(self, store: OsTicket, staff_id: int, poster: str, close_status_id: int = 3) -> None:
        self.store = store
        self.staff_id = staff_id
        self.poster = poster
        self.close_status_id = close_status_id

    def post_reply(self, ticket: Ticket, text: str) -> None:
        self.store.post_reply(ticket, self.staff_id, self.poster, text)

    def close(self, ticket: Ticket) -> None:
        self.store.close(ticket, self.staff_id, self.close_status_id)

    def mark_answered(self, ticket: Ticket) -> None:
        self.store.mark_answered(ticket, self.staff_id)

    def note(self, ticket: Ticket, text: str, marker: Optional[str] = None) -> bool:
        return self.store.post_note(ticket, self.staff_id, self.poster, text, marker)

    def set_topic(self, ticket: Ticket, from_topic_id: int, to_topic_id: int) -> bool:
        return self.store.set_topic(ticket, from_topic_id, to_topic_id, (self.staff_id,))
