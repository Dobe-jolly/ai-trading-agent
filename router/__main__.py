"""The router's commands (run through python -m nmbz --desk router ...).

    python -m nmbz --desk router check [--ping-model]       settings, the ticket database, catch-all tickets
    python -m nmbz --desk router run [--mode M] [--ticket N]
    python -m nmbz topics                                   the help topics the router can file tickets under
    python -m nmbz --desk router audit|compare|report|attachments|notify-test|notify-retry ...
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Optional

from agentcore.audit import AuditLog
from agentcore.cli import DeskCli
from agentcore.metabase import MetabaseClient, MetabaseError
from . import notify, topics
from .config import BASE_DIR, Settings
from .osticket import OsTicket, Ticket

log = logging.getLogger("agent")


def desk_owners() -> list[tuple[str, list, list]]:
    """(title, topics it works, subtopics it leaves alone) for every other desk that is on."""
    from nmbz import desks

    owners = []
    for desk in desks.enabled():
        if desk.key == "router":
            continue
        config = desk.module("config")
        if hasattr(config, "owned_topics"):
            works, leaves = config.owned_topics(desk.settings())
            owners.append((desk.title, works, leaves))
    return owners


class RouterCli(DeskCli):
    desk = "router"
    title = "Router"
    settings_cls = Settings
    base_dir = BASE_DIR

    def build_store(self, s: Settings, mb: MetabaseClient) -> OsTicket:
        return OsTicket(mb, mb.resolve(s.ticket_db_name), topics=s.topics, include_no_topic=s.include_no_topic,
                        exclude_topics=s.exclude_topics, days=s.days)

    def build_catalogue(self, s: Settings, mb: MetabaseClient, store: OsTicket) -> topics.Catalogue:
        return topics.load(mb, store.db, descriptions=topics.load_descriptions(s.topic_descriptions_file),
                           owners=desk_owners(), catch_alls=s.topics, never_to=s.never_to, move_to=s.move_to)

    def build_runner(self, s: Settings, mode: str, mb: MetabaseClient, audit: AuditLog):
        from agentcore.writer import OsTicketWriter, ShadowWriter
        from .loop import TicketAgent
        from .runner import Router
        from .tools import Toolbox

        store = self.build_store(s, mb)
        catalogue = self.build_catalogue(s, mb, store)
        toolbox = Toolbox(store, catalogue, s.examples_days)
        from agentcore.sdk_backend import build_client

        client = build_client(s, 3)
        prompt = self.load_prompt(s, TOPICS=catalogue.prompt_list(), CATCH_ALLS=", ".join(s.topics) or "none",
                                  MAX_INFO_REQUESTS=s.max_info_requests)
        agent = TicketAgent(client, model=s.model, system_prompt=prompt, toolbox=toolbox,
                            max_turns=s.max_agent_turns, agent_staff_id=s.reply_staff_id)
        if mode in ("notes", "live"):
            poster = store.staff_poster_name(int(s.reply_staff_id or 0))
            writer = OsTicketWriter(store, int(s.reply_staff_id or 0), poster, s.close_status_id)
        else:
            writer = ShadowWriter()
        notifier = notify.Notifier(audit, s, notify.build_transport(s), notify.build_sms_transport(s))
        return Router(s, store, agent, toolbox, writer, audit, mode, notifier, None,
                      self.build_reader(s, store, client), catalogue=catalogue)

    def build_reader(self, s: Settings, store: Any, client: Any, mode: Optional[str] = None):
        from .attachments import AttachmentReader

        return AttachmentReader(store, client, s.attachment_model or s.model, mode=mode or s.attachment_mode,
                                max_bytes=s.attachment_max_bytes, max_files=s.attachment_max_files,
                                min_inline_bytes=s.attachment_min_inline_bytes,
                                max_text_chars=s.attachment_max_text_chars, agent_staff_id=s.reply_staff_id)

    def blank_ticket(self, number: str) -> Ticket:
        return Ticket(ticket_id=0, number=number, help_topic="", created="")

    def notify_module(self) -> Any:
        return notify

    def render_attachment(self, item: Any) -> str:
        from .attachments import render_attachment

        return render_attachment(item)

    def describe(self, s: Settings) -> list[str]:
        return [f"[tickets] catch-alls {', '.join(s.topics) or 'none'}"
                + ("; and tickets with no help topic" if s.include_no_topic else "")
                + f"; opened in the last {s.days} days"
                + (f"; except under {', '.join(s.exclude_topics)}" if s.exclude_topics else ""),
                "[moves] on its own only to topics a desk works"
                + (f", and {', '.join(s.move_to)}" if s.move_to else "")
                + f"; at most {s.max_moves_per_run} per pass; never to {', '.join(s.never_to) or 'nothing excluded'}"]

    def check(self, s: Settings, mb: MetabaseClient) -> bool:
        ok, found = self.check_databases(mb, [("ticket", s.ticket_db_name, True)])
        if found["ticket"] is None:
            return ok
        store = self.build_store(s, mb)
        catalogue = self.build_catalogue(s, mb, store)
        print(f"[topics] {len(catalogue.active)} active help topics; {len(catalogue.targets())} a ticket can be "
              f"routed to; {sum(1 for t in catalogue.active if t.desk)} worked by an agent")
        missing = [t for t in s.topics if not any(topics.topic_matches(x.path, [t]) for x in catalogue.active)]
        if missing:
            print(f"[topics] catch-all not found among the active topics: {', '.join(missing)}")
            ok = False
        tickets = store.open_tickets()
        print(f"[osticket] {len(tickets)} open ticket(s) to route ({sum(1 for t in tickets if not t.topic_id)} with "
              f"no help topic)")
        if s.reply_staff_id:
            print(f"[osticket] notes and moves will be posted as: {store.staff_poster_name(s.reply_staff_id)}")
        return ok

    def add_commands(self, sub: Any) -> dict:
        sub.add_parser("topics", help="the help topics tickets can be routed to, and who works each one (no model)")
        return {"topics": self.cmd_topics}

    def cmd_topics(self, s: Settings, args: argparse.Namespace) -> int:
        mb = self.build_metabase(s)
        try:
            store = self.build_store(s, mb)
            catalogue = self.build_catalogue(s, mb, store)
        except MetabaseError as exc:
            print(f"Metabase error: {exc}")
            return 2
        finally:
            mb.close()
        print(f"{'Tickets (90 d)':>14}  {'Moved on its own':16}  Topic")
        for t in catalogue.active:
            if catalogue.is_catch_all(t.path):
                how = "catch-all"
            elif catalogue.is_never_to(t.path):
                how = "never"
            elif catalogue.may_move_to(t):
                how = "yes" + (f" ({t.desk})" if t.desk else "")
            else:
                how = "suggested"
            print(f"{t.recent:>14}  {how:16}  {t.name}" + (f"  - {t.description[:80]}" if t.description else ""))
        return 0


CLI = RouterCli()


def main(argv: Optional[list[str]] = None) -> int:
    return CLI.main(argv)


if __name__ == "__main__":
    sys.exit(main())
