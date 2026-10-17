"""Command line entry point.

    python -m nmbz --desk sms_alerts check [--ping-model]            validate config, Metabase databases, prompt
    python -m nmbz --desk sms_alerts run [--mode shadow|notes|live]  one pass over open SMS Alerts tickets
    python -m nmbz --desk sms_alerts run --ticket 123456             one ticket, ignoring the skip rules
    python -m nmbz --desk sms_alerts audit --since 2026-09-01        replay closed tickets, compare with what was posted
    python -m nmbz --desk sms_alerts compare --since 2026-10-01      shadow decisions vs what actually happened
    python -m nmbz --desk sms_alerts report --since 2026-10-01       export the decision log to CSV
    python -m nmbz --desk sms_alerts check-account --account N --phone P [--types both] [--trace]   one account, no model
    python -m nmbz portal                           the console for every desk (port 8085)
    python -m nmbz --desk sms_alerts portal-user add dobe --role admin
    python -m nmbz --desk sms_alerts notify-test --to you@example.com   one sample branch email
    python -m nmbz --desk sms_alerts notify-retry                      re-send emails that failed
    python -m nmbz --desk sms_alerts anomalies [--alert]               run the anomaly watch now
    python -m nmbz --desk sms_alerts attachments --ticket N            what the agent would read from a ticket's files

The commands every desk shares are agentcore.cli.DeskCli; this module adds the SMS builders, check-account and the
SMS side of `check` and `anomalies`.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from typing import Any, Optional

from agentcore.dataquery import build_for_desk, check_lines
from agentcore.audit import AuditLog
from agentcore.cli import DeskCli
from agentcore.metabase import MetabaseClient, MetabaseError
from . import notify
from .config import BASE_DIR, Settings
from .delivery import SocketDeliveryChecker
from .notify import Notifier, build_sms_transport, build_transport
from .osticket import OsTicket, Ticket
from .sms_checks import TRAN_PATTERN_RE, Databases, SmsChecks
from .tools import Toolbox

log = logging.getLogger("agent")


def build_checks(s: Settings, mb: MetabaseClient) -> tuple[int, SmsChecks]:
    """Resolve databases by name (as the Java does) and build the checks. Ticket and standby DBs are required."""
    ticket_db = mb.resolve(s.ticket_db_name)
    dbs = Databases(
        standby=mb.resolve(s.standby_db_name),
        customers=mb.try_resolve(s.customers_db_name),
        socket=mb.try_resolve(s.socket_db_name) if s.socket_check_enabled else None,
        dispatch=mb.try_resolve(s.dispatch_db_name) if (s.pipeline_trace_enabled or s.anomaly_enabled) else None,
    )
    if dbs.socket is None:
        log.warning("Socket SMS DB unavailable: gateway delivery cannot be confirmed, so no ticket will auto-close.")
    socket = SocketDeliveryChecker(
        mb, enabled=s.socket_check_enabled, window_minutes=s.socket_window_minutes,
        dispatch_lag_minutes=s.socket_dispatch_lag_minutes, over_send_threshold=s.socket_over_send_threshold,
        max_triggers=s.socket_max_triggers,
    )
    checks = SmsChecks(
        mb, dbs, socket, source_name=s.source_name, lookback_days=s.lookback_days,
        salaries_require_delivery_proof=s.salaries_require_delivery_proof, tran_table_pattern=s.tran_table_pattern,
    )
    return ticket_db, checks


def load_prompt(s: Settings) -> str:
    return DeskCli.load_prompt(s, SOURCE_NAME=s.source_name, LOOKBACK_DAYS=s.lookback_days,
                               MAX_INFO_REQUESTS=s.max_info_requests)


def build_runner(s: Settings, mode: str, mb: MetabaseClient, audit: AuditLog):
    from .loop import TicketAgent
    from .runner import Runner
    from agentcore.writer import OsTicketWriter, ShadowWriter

    ticket_db, checks = build_checks(s, mb)
    store = OsTicket(mb, ticket_db)
    lookups = None
    if s.lookups_enabled:
        from .lookups import Lookups

        pending_db = mb.try_resolve(s.pending_changes_db_name) if s.pending_changes_db_name else None
        lookups = Lookups(mb, checks, s.pending_changes_sql, pending_db)
    toolbox = Toolbox(checks, pipeline_trace_enabled=s.pipeline_trace_enabled, lookups=lookups,
                      data=build_for_desk(mb, "sms_alerts", s.query_data, s.data_catalogue))
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
    anomaly = build_anomaly(s, mb, audit, ticket_db, checks, client, notifier) if s.anomaly_enabled else None
    triage = None
    if s.scan_other_topics:
        from .triage import Triage

        triage = Triage(client, s.triage_model or s.model, audit.db, s.triage_min_confidence, s.reply_staff_id)
    return Runner(s, store, agent, toolbox, writer, audit, mode, notifier, anomaly, build_reader(s, store, client),
                  triage)


def build_reader(s: Settings, store: OsTicket, client, mode: Optional[str] = None):
    from .attachments import AttachmentReader

    return AttachmentReader(store, client, s.attachment_model or s.model, mode=mode or s.attachment_mode,
                            max_bytes=s.attachment_max_bytes, max_files=s.attachment_max_files,
                            min_inline_bytes=s.attachment_min_inline_bytes,
                            max_text_chars=s.attachment_max_text_chars, agent_staff_id=s.reply_staff_id)


def build_anomaly(s: Settings, mb: MetabaseClient, audit: AuditLog, ticket_db: int, checks: SmsChecks,
                  client, notifier):
    from .anomaly.collect import Collector
    from .anomaly.service import AnomalyService

    dispatch = checks.dbs.dispatch if checks.dbs.dispatch is not None else mb.try_resolve(s.dispatch_db_name)
    collector = Collector(mb, audit.db, ticket_db=ticket_db, socket_db=checks.dbs.socket, dispatch_db=dispatch,
                          standby_db=checks.dbs.standby, window_hours=s.anomaly_window_hours,
                          baseline_days=s.anomaly_baseline_days, sample_size=s.anomaly_sample_size)
    return AnomalyService(s, audit, collector, client, notifier)


# ---- commands --------------------------------------------------------------------
class SmsAlertsCli(DeskCli):
    desk = "sms_alerts"
    title = "SMS Alerts"
    settings_cls = Settings
    base_dir = BASE_DIR
    anomaly_noun = ("delivered", "deliveries")

    def build_store(self, s: Settings, mb: MetabaseClient) -> OsTicket:
        return OsTicket(mb, mb.resolve(s.ticket_db_name))

    def build_runner(self, s: Settings, mode: str, mb: MetabaseClient, audit: AuditLog):
        return build_runner(s, mode, mb, audit)

    def build_reader(self, s: Settings, store: Any, client: Any, mode: Optional[str] = None):
        return build_reader(s, store, client, mode)

    def blank_ticket(self, number: str) -> Ticket:
        return Ticket(ticket_id=0, number=number, help_topic="", created="", phone_number=None, cif=None,
                      account_number=None, account_number_2=None, currency=None, alert_type_field=None)

    def notify_module(self) -> Any:
        return notify

    def render_attachment(self, item: Any) -> str:
        from .attachments import render_attachment

        return render_attachment(item)

    def build_anomaly_service(self, s: Settings, mb: MetabaseClient, audit: AuditLog, client: Any, notifier: Any):
        ticket_db, checks = build_checks(s, mb)
        return build_anomaly(s, mb, audit, ticket_db, checks, client, notifier)

    def check(self, s: Settings, mb: MetabaseClient) -> bool:
        ok, found = self.check_databases(mb, [
            ("ticket", s.ticket_db_name, True), ("standby", s.standby_db_name, True),
            ("customers", s.customers_db_name, False), ("socket", s.socket_db_name, False),
            ("dispatch", s.dispatch_db_name, False)])
        ticket_db = found["ticket"]
        if ticket_db is not None:
            since = (datetime.now() - timedelta(days=s.scan_days)).strftime("%Y-%m-%d %H:%M:%S")
            tickets = OsTicket(mb, ticket_db).open_tickets(s.sms_topics, since if s.scan_other_topics else None,
                                                           s.exclude_topics)
            mine = sum(1 for t in tickets if t.topic_match)
            print(f"[osticket] {mine} open ticket(s) under {', '.join(s.sms_topics)}")
            if s.scan_other_topics:
                print(f"[osticket] {len(tickets) - mine} open ticket(s) under other topics from the last "
                      f"{s.scan_days} days, read by triage (excluding {', '.join(s.exclude_topics) or 'none'})")
            if s.reply_staff_id:
                print(f"[osticket] replies will be posted as: {OsTicket(mb, ticket_db).staff_poster_name(s.reply_staff_id)}")
        standby = found["standby"]
        if standby is not None and TRAN_PATTERN_RE.fullmatch(s.tran_table_pattern):
            table = s.tran_table_pattern.replace("{year}", str(date.today().year))
            mb.query(standby, f"SELECT 1 AS ok FROM {table} WHERE ROWNUM <= 1")
            print(f"[standby] {table} readable")
        for line in check_lines(build_for_desk(mb, "sms_alerts", s.query_data, s.data_catalogue), s.query_data):
            print(line)
        return ok

    def add_commands(self, sub: Any) -> dict:
        p = sub.add_parser("check-account", help="run the SMS check for one account, no model (ops lookup)")
        p.add_argument("--account", required=True)
        p.add_argument("--phone", help="the number the client expects alerts on")
        p.add_argument("--types", choices=("debits", "salaries", "both"), default="debits")
        p.add_argument("--as-of", help="end of the 15-day window, YYYY-MM-DD (default today)")
        p.add_argument("--trace", action="store_true", help="also run the CAE -> dispatch -> gateway trace for --phone")
        return {"check-account": self.cmd_check_account}

    def cmd_check_account(self, s: Settings, args: argparse.Namespace) -> int:
        """No model: run the production check for one account (the Java's single-CIF mode)."""
        from .normalize import ALERT_DEBITS, ALERT_SALARIES, normalize_account_number

        account = normalize_account_number(args.account)
        if account is None:
            print("--account must contain digits")
            return 1
        types = {"debits": [ALERT_DEBITS], "salaries": [ALERT_SALARIES],
                 "both": [ALERT_SALARIES, ALERT_DEBITS]}[args.types]
        mb = self.build_metabase(s)
        try:
            _, checks = build_checks(s, mb)
            as_of = date.fromisoformat(args.as_of) if args.as_of else None
            result = checks.check_account(account, types, args.phone, as_of=as_of)
            if args.trace and args.phone:
                result["pipeline_trace"] = checks.trace_pipeline(args.phone)
        except MetabaseError as exc:
            print(f"Metabase error: {exc}")
            return 2
        finally:
            mb.close()
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return 0


CLI = SmsAlertsCli()


def main(argv: Optional[list[str]] = None) -> int:
    return CLI.main(argv)


if __name__ == "__main__":
    sys.exit(main())
