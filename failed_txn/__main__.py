"""The failed-transactions desk's commands (run through python -m nmbz --desk failed_txn ...).

    python -m nmbz --desk failed_txn check [--ping-model]       settings, databases, open tickets
    python -m nmbz --desk failed_txn run [--mode M] [--ticket N]
    python -m nmbz find-transactions --account N [--date D] [--amount A] [--reference R]   every source, no model
    python -m nmbz --desk failed_txn audit|compare|report|attachments|notify-test|notify-retry ...
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

from agentcore.audit import AuditLog
from agentcore.cli import DeskCli
from agentcore.dataquery import DataQuery, build_for_desk, check_lines
from agentcore import schemaquery
from agentcore.logs import tools as log_tools
from agentcore.metabase import MetabaseClient
from agentcore.sql import TRAN_PATTERN_RE
from . import notify
from .config import BASE_DIR, Settings
from .osticket import OsTicket, Ticket, topic_matches
from .sources import Connect, CoreBanking, EcoCashLinks, InternetBanking, Postilion
from .tools import Toolbox

log = logging.getLogger("agent")

# How a query_data / query_tables finding may be used, by QUERY_CLOSE (the guardrails enforce the same; failed_txn.guardrails).
QUERY_CLOSE_OFF = ("A close still stands on a confirmed transaction (confirm_transaction) or a confirmed EcoCash link. "
                   "When the explanation comes only from `query_data` or `query_tables`, escalate with your finished reply as the draft: "
                   "a person posts it.")
QUERY_CLOSE_ON = (
    "When the explanation comes from `query_data` or `query_tables` rather than a confirmed transaction or EcoCash link, you may close on "
    "it when all of these hold: the query was matched on an account, mobile, reference or CIF the ticket owner gave and "
    "found the record; you checked the client's account in core banking with `check_account_postings` for the days of "
    "that record; and what your reply says about the money agrees with those postings (say no money left the account "
    "only when core banking shows no debit of that amount that stayed out; say it was reversed or refunded only when "
    "core banking shows the debit and the money coming back). Cite the query ids (Q1, ...) at the start of the evidence "
    "items they support. Otherwise escalate with your finished reply as the draft.")


class FailedTxnCli(DeskCli):
    desk = "failed_txn"
    title = "Failed transactions"
    settings_cls = Settings
    base_dir = BASE_DIR

    def build_store(self, s: Settings, mb: MetabaseClient) -> OsTicket:
        return OsTicket(mb, mb.resolve(s.ticket_db_name), topics=s.topics, exclude_topics=s.exclude_topics)

    def build_toolbox(self, s: Settings, mb: MetabaseClient) -> Toolbox:
        """Resolve each source by name; a missing one only removes its lookup (logged)."""
        found = {name: mb.try_resolve(name) for name in (s.postoffice_db_name, s.conect_db_name, s.billers_db_name,
                                                          s.ib_db_name, s.standby_db_name, s.ecocash_reg_db_name)}
        for name, db in found.items():
            if db is None:
                log.warning("Metabase database %r not found: its lookup is not available.", name)
        postoffice, conect, billers = found[s.postoffice_db_name], found[s.conect_db_name], found[s.billers_db_name]
        ib, standby, ecocash = found[s.ib_db_name], found[s.standby_db_name], found[s.ecocash_reg_db_name]
        return Toolbox(
            postilion=Postilion(mb, postoffice, table=s.postilion_table) if postoffice is not None else None,
            connect=Connect(mb, conect, billers) if (conect is not None or billers is not None) else None,
            ib=InternetBanking(mb, ib) if ib is not None else None,
            core=CoreBanking(mb, standby, s.tran_table_pattern) if standby is not None else None,
            lookback_days=s.lookback_days, date_window_days=s.date_window_days,
            ecocash=EcoCashLinks(mb, ecocash, s.ecocash_reg_table) if ecocash is not None else None,
            data=self.build_data(s, mb),
            schema=schemaquery.build_for_desk(mb, s.query_tables, s.schema_dir),
            logs=self.build_logs(s),
        )

    @staticmethod
    def build_logs(s: Settings) -> Optional[log_tools.LogTools]:
        """log_errors and log_search over the ingested logs; off with QUERY_LOGS=false, with no LOG_HASH_KEY, or when no
        log store has been made yet (python -m agentcore.logs ingest)."""
        from agentcore.logs.mask import KeyedMasker
        from agentcore.logs.profiles import load_profiles
        from agentcore.logs.store import LogStore

        if not s.query_logs or len(s.log_hash_key) < 16 or s.log_db is None or not Path(s.log_db).exists():
            return None
        return log_tools.LogTools(LogStore(s.log_db), KeyedMasker(s.log_hash_key), load_profiles())

    @staticmethod
    def build_data(s: Settings, mb: MetabaseClient) -> Optional[DataQuery]:
        """query_data over the approved catalogue; off with QUERY_DATA=false, or when the catalogue cannot be used."""
        return build_for_desk(mb, "failed_txn", s.query_data, s.data_catalogue)

    def build_runner(self, s: Settings, mode: str, mb: MetabaseClient, audit: AuditLog):
        from agentcore.writer import OsTicketWriter, ShadowWriter
        from .loop import TicketAgent
        from .runner import Runner

        store = self.build_store(s, mb)
        toolbox = self.build_toolbox(s, mb)
        from agentcore.sdk_backend import build_client

        client = build_client(s, 3)
        agent = TicketAgent(client, model=s.model, system_prompt=self.load_prompt(
            s, LOOKBACK_DAYS=s.lookback_days, MAX_INFO_REQUESTS=s.max_info_requests, WINDOW_DAYS=s.date_window_days,
            QUERY_CLOSE_RULE=QUERY_CLOSE_ON if s.query_close else QUERY_CLOSE_OFF),
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
            ("ticket", s.ticket_db_name, True), ("postilion", s.postoffice_db_name, False),
            ("connect", s.conect_db_name, False), ("billers", s.billers_db_name, False), ("ib", s.ib_db_name, False),
            ("standby", s.standby_db_name, False), ("ecocash links", s.ecocash_reg_db_name, False)])
        if found["ticket"] is not None:
            store = self.build_store(s, mb)
            tickets = store.open_tickets()
            print(f"[osticket] {len(tickets)} open failed-transaction ticket(s)")
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
        if found["standby"] is not None and TRAN_PATTERN_RE.fullmatch(s.tran_table_pattern):
            table = s.tran_table_pattern.replace("{year}", str(date.today().year))
            mb.query(found["standby"], f"SELECT 1 AS ok FROM {table} WHERE ROWNUM <= 1")
            print(f"[standby] {table} readable")
        for line in check_lines(self.build_data(s, mb), s.query_data):  # tables and columns, no rows read
            print(line)
        return ok

    def add_commands(self, sub: Any) -> dict:
        p = sub.add_parser("find-transactions", help="a client's transactions in every source, no model")
        p.add_argument("--account", help="account number")
        p.add_argument("--reference", help="RRN")
        p.add_argument("--date", help="YYYY-MM-DD (default: the recent past)")
        p.add_argument("--amount", help="amount, e.g. 20 or 1500.00")
        p.add_argument("--kind", default="any", help="NMB Connect kind: rtgs, transfer, zipit, airtime, bill, zimra, any")
        return {"find-transactions": self.cmd_find}

    def cmd_find(self, s: Settings, args: argparse.Namespace) -> int:
        from agentcore.pii import Pseudonymizer
        from .tools import Evidence

        if not (args.account or args.reference):
            print("Give --account or --reference.")
            return 1
        mb = self.build_metabase(s)
        try:
            toolbox = self.build_toolbox(s, mb)
            evidence, pseudo = Evidence(), Pseudonymizer()
            base = {k: v for k, v in (("account_number", args.account), ("date", args.date), ("amount", args.amount))
                    if v}
            calls = [("find_switch_transactions", {"reference": args.reference} if args.reference else base)]
            if args.account:
                calls += [("find_nmb_connect_transactions", {**base, "kind": args.kind}),
                          ("find_internet_banking_transfers", base), ("check_account_postings", base)]
            for name, call_args in calls:
                if name not in {t["name"] for t in toolbox.schemas()}:
                    print(f"== {name}: not available")
                    continue
                text, error = toolbox.run(name, call_args, pseudo, evidence, None)
                print(f"== {name}")
                print(pseudo.unmask(text) if not error else f"ERROR {pseudo.unmask(text)}")
        finally:
            mb.close()
        print(json.dumps({t.id: t.status for t in evidence.txns.values()}))
        return 0


CLI = FailedTxnCli()


def main(argv: Optional[list[str]] = None) -> int:
    return CLI.main(argv)


if __name__ == "__main__":
    sys.exit(main())
