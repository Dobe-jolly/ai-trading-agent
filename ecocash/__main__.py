"""Command line entry point.

    python -m nmbz --desk ecocash check [--ping-model]            validate config, Metabase databases, prompt
    python -m nmbz --desk ecocash run [--mode shadow|notes|live]  one pass over open EcoCash tickets
    python -m nmbz --desk ecocash run --ticket 123456             one ticket, ignoring the skip rules
    python -m nmbz --desk ecocash lookup --mobile 0771234567      a client's link attempts, link record, account (no model)
    python -m nmbz --desk ecocash monitor                         follow ecocashreg.service and record every attempt
    python -m nmbz --desk ecocash monitor --from-file log.txt     import a saved journal export (add --dry-run to just count)
    python -m nmbz --desk ecocash audit --since 2026-09-01        replay closed tickets, compare with what was posted
    python -m nmbz --desk ecocash compare --since 2026-10-01      shadow decisions vs what actually happened
    python -m nmbz --desk ecocash report --since 2026-10-01       export the decision log to CSV
    python -m nmbz --desk ecocash attachments --ticket 123456     what the agent would read from a ticket's files
    python -m nmbz portal                           the console for every desk (port 8085)
    python -m nmbz --desk ecocash portal-user add dobe --role admin
    python -m nmbz --desk ecocash notify-test --to you@example.com   one sample branch email
    python -m nmbz --desk ecocash notify-retry                      re-send emails that failed
    python -m nmbz --desk ecocash anomalies [--alert]               run the anomaly watch now

The commands every desk shares are agentcore.cli.DeskCli; this module adds the EcoCash builders, lookup, the
monitor, and the EcoCash side of `check` and `anomalies`.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from typing import Any, Optional

from agentcore.audit import AuditLog
from agentcore.cli import DeskCli
from agentcore.metabase import MetabaseClient, MetabaseError
from . import notify
from .config import BASE_DIR, Settings
from .ecocash import AttemptStore, BankAccounts, LinkRecords
from .notify import Notifier, build_sms_transport, build_transport
from .osticket import OsTicket, Ticket
from .tools import Toolbox

log = logging.getLogger("agent")


def build_store(s: Settings, mb: MetabaseClient) -> OsTicket:
    return OsTicket(mb, mb.resolve(s.ticket_db_name), topics=s.ecocash_topics, keywords=s.ecocash_keywords,
                    exclude_topics=s.ecocash_exclude_topics, content_days=s.ecocash_content_days)


def build_toolbox(s: Settings, mb: MetabaseClient) -> Toolbox:
    """Resolve databases by name. ecocash_monitor is required; Postilion and core banking are optional."""
    attempts = AttemptStore(mb, mb.resolve(s.ecocash_db_name), attempts_table=s.attempts_table,
                            legacy_table=s.legacy_failures_table)
    postilion = mb.try_resolve(s.postilion_db_name)
    standby = mb.try_resolve(s.standby_db_name)
    if postilion is None:
        log.warning("Metabase database %r not found: the bank's link record (eftc_ecocash) is not available.",
                    s.postilion_db_name)
    if standby is None:
        log.warning("Metabase database %r not found: core banking checks are not available.", s.standby_db_name)
    return Toolbox(attempts, LinkRecords(mb, postilion, s.postilion_table) if postilion is not None else None,
                   BankAccounts(mb, standby, s.core_extra_sql, s.pending_changes_sql) if standby is not None else None,
                   lookback_days=s.lookback_days)


def load_prompt(s: Settings) -> str:
    return DeskCli.load_prompt(s, LOOKBACK_DAYS=s.lookback_days, MAX_INFO_REQUESTS=s.max_info_requests)


def build_runner(s: Settings, mode: str, mb: MetabaseClient, audit: AuditLog):
    from .loop import TicketAgent
    from .runner import Runner
    from agentcore.writer import OsTicketWriter, ShadowWriter

    store = build_store(s, mb)
    toolbox = build_toolbox(s, mb)
    from agentcore.sdk_backend import build_client

    client = build_client(s, 3)
    agent = TicketAgent(
        client, model=s.model, system_prompt=load_prompt(s), toolbox=toolbox,
        max_turns=s.max_agent_turns, agent_staff_id=s.reply_staff_id,
    )
    if mode in ("notes", "live"):
        poster = store.staff_poster_name(int(s.reply_staff_id or 0))
        writer = OsTicketWriter(store, int(s.reply_staff_id or 0), poster, s.close_status_id)
    else:
        writer = ShadowWriter()
    # Built in every mode: emails already queued for posted replies still go out after a switch to shadow.
    notifier = Notifier(audit, s, build_transport(s), build_sms_transport(s))
    anomaly = build_anomaly(s, mb, audit, store.db, toolbox, client, notifier) if s.anomaly_enabled else None
    return Runner(s, store, agent, toolbox, writer, audit, mode, notifier, anomaly, build_reader(s, store, client))


def build_reader(s: Settings, store: OsTicket, client, mode: Optional[str] = None):
    from .attachments import AttachmentReader

    return AttachmentReader(store, client, s.attachment_model or s.model, mode=mode or s.attachment_mode,
                            max_bytes=s.attachment_max_bytes, max_files=s.attachment_max_files,
                            min_inline_bytes=s.attachment_min_inline_bytes,
                            max_text_chars=s.attachment_max_text_chars, agent_staff_id=s.reply_staff_id)


def build_anomaly(s: Settings, mb: MetabaseClient, audit: AuditLog, ticket_db: int, toolbox: Toolbox,
                  client, notifier):
    from .anomaly.collect import Collector
    from .anomaly.service import AnomalyService

    collector = Collector(mb, audit.db, ticket_db=ticket_db, attempts=toolbox.attempts, topics=s.ecocash_topics,
                          window_hours=s.anomaly_window_hours, baseline_days=s.anomaly_baseline_days,
                          sample_size=s.anomaly_sample_size)
    return AnomalyService(s, audit, collector, client, notifier)


# ---- commands --------------------------------------------------------------------
class EcoCashCli(DeskCli):
    desk = "ecocash"
    title = "EcoCash"
    settings_cls = Settings
    base_dir = BASE_DIR
    anomaly_noun = ("linked", "links")

    def build_store(self, s: Settings, mb: MetabaseClient) -> OsTicket:
        return build_store(s, mb)

    def build_runner(self, s: Settings, mode: str, mb: MetabaseClient, audit: AuditLog):
        return build_runner(s, mode, mb, audit)

    def build_reader(self, s: Settings, store: Any, client: Any, mode: Optional[str] = None):
        return build_reader(s, store, client, mode)

    def blank_ticket(self, number: str) -> Ticket:
        return Ticket(ticket_id=0, number=number, help_topic="", created="")

    def notify_module(self) -> Any:
        return notify

    def render_attachment(self, item: Any) -> str:
        from .attachments import render_attachment

        return render_attachment(item)

    def build_anomaly_service(self, s: Settings, mb: MetabaseClient, audit: AuditLog, client: Any, notifier: Any):
        return build_anomaly(s, mb, audit, mb.resolve(s.ticket_db_name), build_toolbox(s, mb), client, notifier)

    def describe(self, s: Settings) -> list[str]:
        return [f"[tickets] topics {', '.join(s.ecocash_topics)}; also tickets mentioning "
                f"{', '.join(s.ecocash_keywords)} from the last {s.ecocash_content_days} days, except under "
                f"{', '.join(s.ecocash_exclude_topics) or 'none'}"]

    def check(self, s: Settings, mb: MetabaseClient) -> bool:
        ok, found = self.check_databases(mb, [
            ("ticket", s.ticket_db_name, True), ("ecocash", s.ecocash_db_name, True),
            ("postilion", s.postilion_db_name, False), ("standby", s.standby_db_name, False)])
        if found["ticket"] is not None:
            store = build_store(s, mb)
            tickets = store.open_tickets()
            by_topic = sum(1 for t in tickets if t.topic_match)
            print(f"[osticket] {len(tickets)} open EcoCash ticket(s): {by_topic} by topic, "
                  f"{len(tickets) - by_topic} by content")
            if s.reply_staff_id:
                print(f"[osticket] replies will be posted as: {store.staff_poster_name(s.reply_staff_id)}")
        if found["ecocash"] is not None:
            toolbox = build_toolbox(s, mb)
            if toolbox.attempts.attempts_available():
                first = toolbox.attempts.attempts_start()
                print(f"[ecocash] {s.attempts_table}: " + (f"recording since {first:%d-%m-%Y %H:%M}" if first
                                                           else "empty (start the monitor)"))
            else:
                print(f"[ecocash] {s.attempts_table} is not in {s.ecocash_db_name!r} yet: lookups read the Java "
                      "monitor's failed_ecocash_registrations only (failures, not successful links). Start the "
                      "Python monitor to record every attempt.")
            if toolbox.link_records is not None:
                mb.query(toolbox.link_records.db, f"SELECT TOP 1 date_added FROM {s.postilion_table}")
                print(f"[postilion] {s.postilion_table} readable")
            if toolbox.accounts is not None:
                mb.query(toolbox.accounts.db, "SELECT 1 AS ok FROM NMBPRODIDC.IACLINK WHERE ROWNUM <= 1")
                print("[standby] NMBPRODIDC.IACLINK readable")
        return ok

    def add_commands(self, sub: Any) -> dict:
        p = sub.add_parser("lookup", help="a client's link attempts, link record and account, no model")
        p.add_argument("--mobile")
        p.add_argument("--account")
        p.add_argument("--id-number")
        p.add_argument("--days", type=int, default=60)

        p = sub.add_parser("monitor", help="follow ecocashreg.service and record every link/delink attempt")
        p.add_argument("--from-file", help="import a saved `journalctl -u ecocashreg.service` text export instead")
        p.add_argument("--dry-run", action="store_true", help="parse and count only; write nothing")
        return {"lookup": self.cmd_lookup, "monitor": self.cmd_monitor}

    def cmd_lookup(self, s: Settings, args: argparse.Namespace) -> int:
        """No model: a client's link attempts, the bank's link record and the account (as the agent sees them,
        unmasked)."""
        if not (args.mobile or args.account or args.id_number):
            print("Give --mobile, --account or --id-number")
            return 1
        mb = self.build_metabase(s)
        try:
            toolbox = build_toolbox(s, mb)
            lookup = toolbox.attempts.find(mobile=args.mobile, account=args.account, id_number=args.id_number,
                                           days=args.days)
            out: dict = {"status": lookup.status, "attempts": [a.describe() for a in lookup.attempts]}
            from .tools import standard_reply

            out["suggested_action"], out["standard_reply"] = standard_reply(lookup.latest, None)
            if toolbox.link_records is not None and (args.mobile or args.account):
                out["bank_link_record"] = toolbox.link_records.find(mobile=args.mobile, account=args.account)
            if toolbox.accounts is not None and args.account:
                out["core_banking"] = toolbox.accounts.check(args.account)
        except (MetabaseError, ValueError) as exc:
            print(f"Error: {exc}")
            return 2
        finally:
            mb.close()
        print(json.dumps(out, indent=2, default=str, ensure_ascii=False))
        return 0

    def cmd_monitor(self, s: Settings, args: argparse.Namespace) -> int:
        """Replace the Java ecocash-registration-monitor: record every link and delink attempt."""
        from .monitor.follower import JournalFollower
        from .monitor.parser import parse_text
        from .monitor.store import MemoryStore, MetabaseStore, PostgresStore, jdbc_to_conninfo

        store = None
        mb = None
        if args.dry_run:
            store = MemoryStore()
        else:
            problems = s.monitor_problems()
            if problems:
                for problem in problems:
                    log.error("Config: %s", problem)
                return 1
            if s.monitor_store == "metabase":
                mb = self.build_metabase(s)
                store = MetabaseStore(mb, mb.resolve(s.ecocash_db_name), legacy=s.monitor_legacy_table)
            else:
                store = PostgresStore(jdbc_to_conninfo(s.monitor_db_url or "", s.monitor_db_user,
                                                       s.monitor_db_password), legacy=s.monitor_legacy_table)
        try:
            if args.from_file:
                with open(args.from_file, encoding="utf-8", errors="replace") as handle:
                    attempts, parser = parse_text(handle, s.monitor_no_response_seconds)
                store.ensure_schema()
                saved = store.save(attempts)
                counts: dict = {}
                for a in attempts:
                    key = f"{a.operation.lower()} {a.outcome}" + (f" {a.response_code}" if a.outcome == "failed" else "")
                    counts[key] = counts.get(key, 0) + 1
                print(f"{len(attempts)} attempt(s) in {args.from_file}; {saved} new"
                      + (" (dry run, nothing written)" if args.dry_run else " saved"))
                for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                    print(f"  {n:5}  {key}")
                return 0
            from .monitor.parser import JournalParser

            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            follower = JournalFollower(JournalParser(s.monitor_no_response_seconds), store, unit=s.monitor_unit,
                                       position_file=s.monitor_position_file, journalctl=s.monitor_journalctl)
            follower.run(stop)
            log.info("Monitor stopped; %d attempt(s) saved this session", follower.saved)
            return 0
        finally:
            store.close()
            if mb is not None:
                mb.close()


CLI = EcoCashCli()


def main(argv: Optional[list[str]] = None) -> int:
    return CLI.main(argv)


if __name__ == "__main__":
    sys.exit(main())
