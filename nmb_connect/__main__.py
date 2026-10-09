"""The NMB Connect desk's commands (run through python -m nmbz --desk nmb_connect ...).

    python -m nmbz --desk nmb_connect check [--ping-model]   settings, databases, open tickets, the profile tables
    python -m nmbz --desk nmb_connect run [--mode M] [--ticket N]
    python -m nmbz connect-lookup --cif C | --account A [--mobile M]   a client's profile, core banking, activity; no model
    python -m nmbz --desk nmb_connect audit|compare|report|attachments|notify-test|notify-retry ...
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Optional

from agentcore.audit import AuditLog
from agentcore.cli import DeskCli
from agentcore.metabase import MetabaseClient, MetabaseError
from . import notify
from .config import BASE_DIR, Settings
from .osticket import OsTicket, Ticket, topic_matches
from .sources import AppActivity, CoreBanking, Registration, RegistrationFailures, SmsGateway
from .tools import Toolbox

log = logging.getLogger("agent")


class NmbConnectCli(DeskCli):
    desk = "nmb_connect"
    title = "NMB Connect"
    settings_cls = Settings
    base_dir = BASE_DIR

    def build_store(self, s: Settings, mb: MetabaseClient) -> OsTicket:
        return OsTicket(mb, mb.resolve(s.ticket_db_name), topics=s.topics, exclude_topics=s.exclude_topics)

    def build_toolbox(self, s: Settings, mb: MetabaseClient) -> Toolbox:
        """Resolve each source by name; a missing one only removes its lookup (logged)."""
        names = {"reg": s.reg_db_name, "conect": s.conect_db_name, "standby": s.standby_db_name,
                 "failures": s.reg_failures_db_name, "socket": s.socket_db_name}
        found = {key: (mb.try_resolve(name) if name else None) for key, name in names.items()}
        for key, db in found.items():
            if db is None and names[key]:
                log.warning("Metabase database %r not found: its lookup is not available.", names[key])
        return Toolbox(
            registration=Registration(mb, found["reg"], s.reg_customer_table, s.reg_channels_table or None,
                                      s.reg_history_table or None) if found["reg"] is not None else None,
            core=CoreBanking(mb, found["standby"], s.pending_changes_sql) if found["standby"] is not None else None,
            activity=AppActivity(mb, found["conect"]) if found["conect"] is not None else None,
            failures=RegistrationFailures(mb, found["failures"]) if found["failures"] is not None else None,
            sms=SmsGateway(mb, found["socket"]) if found["socket"] is not None else None,
            activity_days=s.activity_days, sms_days=s.sms_days,
        )

    def build_runner(self, s: Settings, mode: str, mb: MetabaseClient, audit: AuditLog):
        import anthropic

        from agentcore.writer import OsTicketWriter, ShadowWriter
        from .loop import TicketAgent
        from .runner import Runner

        store = self.build_store(s, mb)
        toolbox = self.build_toolbox(s, mb)
        client = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=3)
        agent = TicketAgent(client, model=s.model, system_prompt=self.load_prompt(
            s, ACTIVITY_DAYS=s.activity_days, SMS_DAYS=s.sms_days, MAX_INFO_REQUESTS=s.max_info_requests),
            toolbox=toolbox, max_turns=s.max_agent_turns, agent_staff_id=s.reply_staff_id)
        if mode in ("notes", "live"):
            poster = store.staff_poster_name(int(s.reply_staff_id or 0))
            writer = OsTicketWriter(store, int(s.reply_staff_id or 0), poster, s.close_status_id)
        else:
            writer = ShadowWriter()
        notifier = notify.Notifier(audit, s, notify.build_transport(s), notify.build_sms_transport(s))
        return Runner(s, store, agent, toolbox, writer, audit, mode, notifier, None, self.build_reader(s, store, client))

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
        return [f"[tickets] topics {', '.join(s.topics)}"
                + (f"; except under {', '.join(s.exclude_topics)}" if s.exclude_topics else "")]

    def check(self, s: Settings, mb: MetabaseClient) -> bool:
        ok, found = self.check_databases(mb, [
            ("ticket", s.ticket_db_name, True), ("reg", s.reg_db_name, True), ("conect", s.conect_db_name, False),
            ("standby", s.standby_db_name, False), ("socket", s.socket_db_name, False)]
            + ([("failures", s.reg_failures_db_name, False)] if s.reg_failures_db_name else []))
        if not s.reg_failures_db_name:
            print("[failures] REG_FAILURES_DB_NAME is not set: failed registrations are not looked up")
        if found["ticket"] is not None:
            store = self.build_store(s, mb)
            tickets = store.open_tickets()
            print(f"[osticket] {len(tickets)} open NMB Connect ticket(s)")
            by_topic: dict = {}
            for t in tickets:
                by_topic[t.help_topic] = by_topic.get(t.help_topic, 0) + 1
            for topic, n in sorted(by_topic.items(), key=lambda kv: -kv[1]):
                print(f"[osticket] {n:5d}  {topic}")
            quiet = [t for t in s.topics if not any(topic_matches(name, [t]) for name in by_topic)]
            if quiet:
                print(f"[osticket] no open tickets under: {', '.join(quiet)} (check the name if that is unexpected)")
            if s.reply_staff_id:
                print(f"[osticket] replies will be posted as: {store.staff_poster_name(s.reply_staff_id)}")
        if found["reg"] is not None:
            reg = Registration(mb, found["reg"], s.reg_customer_table, s.reg_channels_table or None,
                               s.reg_history_table or None)
            try:
                schema = reg.schema()
            except MetabaseError as exc:
                print(f"[reg] could not list the tables: {exc}")
                return False
            tables = schema.pop("_tables", [])
            print(f"[reg] {len(tables)} table(s) in {s.reg_db_name}")
            for table in (s.reg_customer_table, s.reg_channels_table, s.reg_history_table):
                if not table:
                    continue
                columns = schema.get(table.split(".")[-1])
                if columns:
                    print(f"[reg] {table}: {', '.join(columns)}")
                elif table == s.reg_customer_table:
                    print(f"[reg] profile table {table!r} not found: set NMB_CONNECT_PROFILE_TABLE")
                    ok = False
                else:
                    print(f"[reg] {table!r} not found: that part of the profile lookup is skipped")
            print("[reg] columns whose names suggest secrets, names or addresses are never shown to the model")
        return ok

    def add_commands(self, sub: Any) -> dict:
        p = sub.add_parser("connect-lookup", help="a client's NMB Connect profile, core banking record and activity; "
                                                   "no model")
        p.add_argument("--cif", help="CIF (customer number)")
        p.add_argument("--account", help="account number")
        p.add_argument("--mobile", help="mobile number")
        p.add_argument("--id-number", help="national ID (for failed registrations)")
        return {"connect-lookup": self.cmd_lookup}

    def cmd_lookup(self, s: Settings, args: argparse.Namespace) -> int:
        from agentcore.pii import Pseudonymizer
        from .tools import Evidence

        if not (args.cif or args.account or args.mobile):
            print("Give --cif, --account or --mobile.")
            return 1
        mb = self.build_metabase(s)
        try:
            toolbox = self.build_toolbox(s, mb)
            evidence, pseudo = Evidence(), Pseudonymizer()
            if args.cif:
                evidence.client_cifs.add(args.cif.strip().lstrip("0"))
            base = {k: v for k, v in (("cif", args.cif), ("account_number", args.account), ("mobile", args.mobile)) if v}
            calls = [("get_connect_profile", base),
                     ("check_core_banking", {k: v for k, v in base.items() if k != "mobile"}),
                     ("get_app_activity", {k: v for k, v in base.items() if k != "account_number"})]
            if args.mobile or args.id_number:
                calls.append(("find_registration_failures", {k: v for k, v in (("mobile", args.mobile),
                                                                                ("id_number", args.id_number)) if v}))
            if args.mobile:
                calls.append(("get_sms_to_number", {"mobile": args.mobile}))
            available = {t["name"] for t in toolbox.schemas()}
            for name, call_args in calls:
                if name not in available:
                    print(f"== {name}: not available")
                    continue
                if not call_args:
                    continue
                text, error = toolbox.run(name, call_args, pseudo, evidence, None)
                print(f"== {name}")
                print(pseudo.unmask(text) if not error else f"ERROR {pseudo.unmask(text)}")
        finally:
            mb.close()
        print(json.dumps({"profiles": len(evidence.profiles), "client_cifs": sorted(evidence.client_cifs)}))
        return 0


CLI = NmbConnectCli()


def main(argv: Optional[list[str]] = None) -> int:
    return CLI.main(argv)


if __name__ == "__main__":
    sys.exit(main())
