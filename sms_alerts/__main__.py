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
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from agentcore.audit import AuditLog
from .config import BASE_DIR, VALID_MODES, Settings
from agentcore.controls import effective_mode, read_mode_override
from .decision import ESCALATE, REPLY_AND_CLOSE
from .delivery import SocketDeliveryChecker
from agentcore.metabase import MetabaseClient, MetabaseError
from .notify import Notifier, NotifyError, build_sms_transport, build_transport, render
from .osticket import OsTicket
from .sms_checks import TRAN_PATTERN_RE, Databases, SmsChecks
from .tools import Toolbox

log = logging.getLogger("agent")


def build_metabase(s: Settings) -> MetabaseClient:
    return MetabaseClient(
        s.metabase_host, s.metabase_api_key or "", timeout=s.metabase_timeout,
        retry_count=s.metabase_retry_count, retry_delay=s.metabase_retry_delay, verify=s.metabase_verify,
    )


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
    text = s.prompt_file.read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->\s*", "", text, flags=re.S)
    return (text.replace("{{SOURCE_NAME}}", s.source_name)
                .replace("{{LOOKBACK_DAYS}}", str(s.lookback_days))
                .replace("{{MAX_INFO_REQUESTS}}", str(s.max_info_requests)))


def build_runner(s: Settings, mode: str, mb: MetabaseClient, audit: AuditLog):
    import anthropic

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
    toolbox = Toolbox(checks, pipeline_trace_enabled=s.pipeline_trace_enabled, lookups=lookups)
    client = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=3)
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
def cmd_check(s: Settings, args: argparse.Namespace) -> int:
    ok = True
    for problem in s.problems(args.mode):
        print(f"[config] {problem}")
        ok = False
    for warning in s.warnings(args.mode):
        print(f"[warning] {warning}")
    print(f"[notify] branch emails: {s.notify_transport}" + (f" on {', '.join(sorted(s.notify_on))}" if s.notify_transport != "off" else ""))
    print(f"[attachments] {s.attachment_mode}: " + {"model": "text files read here; images and PDFs read by the model",
                                                    "local": "text files read here; images and PDFs listed only",
                                                    "off": "ignored"}.get(s.attachment_mode, "?"))
    if s.metabase_api_key:
        mb = build_metabase(s)
        try:
            for label, name, required in (
                ("ticket", s.ticket_db_name, True), ("standby", s.standby_db_name, True),
                ("customers", s.customers_db_name, False), ("socket", s.socket_db_name, False),
                ("dispatch", s.dispatch_db_name, False),
            ):
                db_id = mb.try_resolve(name)
                print(f"[metabase] {label:9} '{name}': {db_id if db_id is not None else 'NOT FOUND'}")
                ok &= db_id is not None or not required
            ticket_db = mb.try_resolve(s.ticket_db_name)
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
            standby = mb.try_resolve(s.standby_db_name)
            if standby is not None and TRAN_PATTERN_RE.fullmatch(s.tran_table_pattern):
                year = date.today().year
                table = s.tran_table_pattern.replace("{year}", str(year))
                mb.query(standby, f"SELECT 1 AS ok FROM {table} WHERE ROWNUM <= 1")
                print(f"[standby] {table} readable")
        except MetabaseError as exc:
            print(f"[metabase] {exc}")
            ok = False
        finally:
            mb.close()
    if args.ping_model and s.anthropic_api_key:
        import anthropic

        try:
            reply = anthropic.Anthropic(api_key=s.anthropic_api_key).messages.create(
                model=s.model, max_tokens=5, messages=[{"role": "user", "content": "Reply with OK"}])
            print(f"[model] OK: {s.model} answered ({reply.usage.output_tokens} tokens)")
        except anthropic.APIError as exc:
            print(f"[model] {type(exc).__name__}: {exc}")
            ok = False
    print("All good." if ok else "Fix the items above.")
    return 0 if ok else 1


def _run_lock(s: Settings):
    """One pass at a time, even if a second copy is started by hand. Returns the open lock file or None."""
    try:
        import fcntl
    except ImportError:  # Windows dev machine: no lock
        return open(os.devnull, "w")
    s.state_db.parent.mkdir(parents=True, exist_ok=True)
    handle = open(s.state_db.parent / "agent.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def cmd_run(s: Settings, args: argparse.Namespace) -> int:
    configured = args.mode or s.mode
    override = read_mode_override(s.mode_override_file)
    mode = effective_mode(configured, override)
    if mode != configured:
        log.warning("Mode held at %s by the portal override (configured: %s)", mode, configured)
    problems = s.problems(mode)
    if problems:
        for problem in problems:
            log.error("Config: %s", problem)
        return 1
    for warning in s.warnings(mode):
        log.warning("Config: %s", warning)
    if s.kill_switch_file.exists():
        log.warning("Kill switch %s exists; not running", s.kill_switch_file)
        return 0
    lock = _run_lock(s)
    if lock is None:
        log.warning("Another pass is still running; skipping this one")
        return 0
    mb = build_metabase(s)
    audit = AuditLog(s.state_db)
    try:
        runner = build_runner(s, mode, mb, audit)
        summary = runner.run(limit=args.limit, ticket_number=args.ticket, force=args.force)
    except MetabaseError as exc:
        log.error("Metabase unavailable: %s", exc)
        return 2
    finally:
        audit.close()
        mb.close()
        lock.close()
    return 0 if summary.errors == 0 else 3


def cmd_audit(s: Settings, args: argparse.Namespace) -> int:
    problems = s.problems("shadow")
    if problems:
        for problem in problems:
            log.error("Config: %s", problem)
        return 1
    mb = build_metabase(s)
    audit = AuditLog(s.state_db)
    try:
        runner = build_runner(s, "shadow", mb, audit)
        out = runner.audit_closed(date.fromisoformat(args.since), args.limit, args.window_today, Path(args.out_dir))
        print(f"Wrote {out}")
    except MetabaseError as exc:
        log.error("Metabase unavailable: %s", exc)
        return 2
    finally:
        audit.close()
        mb.close()
    return 0


def cmd_check_account(s: Settings, args: argparse.Namespace) -> int:
    """No model: run the production check for one account (the Java's single-CIF mode)."""
    import json

    from .normalize import ALERT_DEBITS, ALERT_SALARIES, normalize_account_number

    account = normalize_account_number(args.account)
    if account is None:
        print("--account must contain digits")
        return 1
    types = {"debits": [ALERT_DEBITS], "salaries": [ALERT_SALARIES], "both": [ALERT_SALARIES, ALERT_DEBITS]}[args.types]
    mb = build_metabase(s)
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


def cmd_attachments(s: Settings, args: argparse.Namespace) -> int:
    """Read the attachments on one ticket and print what the agent would see. No decision, no writes."""
    from .attachments import render_attachment
    from .osticket import Ticket
    from agentcore.pii import Pseudonymizer

    mode = "local" if args.local else s.attachment_mode
    if mode == "off":
        print("ATTACHMENT_MODE=off: attachments are ignored. Use --local to read text files anyway.")
        mode = "local"
    client = None
    if mode == "model":
        if not s.anthropic_api_key:
            print("ANTHROPIC_API_KEY is not set (or use --local)")
            return 1
        import anthropic

        client = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=2)
    mb = build_metabase(s)
    try:
        store = OsTicket(mb, mb.resolve(s.ticket_db_name))
        ticket = Ticket(ticket_id=0, number=str(args.ticket), help_topic="", created="", phone_number=None, cif=None,
                        account_number=None, account_number_2=None, currency=None, alert_type_field=None)
        store.load_thread(ticket)
        if ticket.thread_id is None:
            print(f"Ticket #{args.ticket} not found")
            return 1
        result = build_reader(s, store, client, mode).read(ticket)
    except MetabaseError as exc:
        print(f"Metabase error: {exc}")
        return 2
    finally:
        mb.close()
    if result.error:
        print(result.error)
    if not result.attachments:
        print(f"Ticket #{args.ticket}: no attachments.")
        return 0
    pseudo = Pseudonymizer()
    for item in result.attachments:
        text = render_attachment(item)
        print(text if args.unmasked else pseudo.mask(text))
        print()
    print(f"Model tokens: {result.input_tokens} in, {result.output_tokens} out "
          f"({'values shown unmasked' if args.unmasked else 'numbers masked as the agent sees them'})")
    return 0


def cmd_portal(s: Settings, args: argparse.Namespace) -> int:
    """The console serves every desk (python -m nmbz portal); started from here it is the same console."""
    from nmbz.console import serve

    return serve(args.env_file, host=args.host, port=args.port)


def cmd_portal_user(s: Settings, args: argparse.Namespace) -> int:
    import getpass

    from agentcore.portal.users import UserStore

    store = UserStore(s.portal_users_file)
    if args.action == "list":
        for name, role in sorted(store.users().items()):
            print(f"{name:20} {role}")
        return 0
    if not args.name:
        print("A user name is required.")
        return 1
    if args.action == "remove":
        print("Removed." if store.remove(args.name) else "No such user.")
        return 0
    password = getpass.getpass(f"Password for {args.name} (12+ characters): ")
    if password != getpass.getpass("Repeat: "):
        print("Passwords do not match.")
        return 1
    try:
        store.set(args.name, password, args.role)
    except ValueError as exc:
        print(exc)
        return 1
    print(f"Saved {args.name} ({args.role}) in {s.portal_users_file}")
    return 0


def cmd_notify_test(s: Settings, args: argparse.Namespace) -> int:
    """Send one sample email through the configured transport (no queue, no ticket)."""
    import uuid as _uuid

    transport = build_transport(s)
    if transport is None:
        print("Branch notifications are off. Set NOTIF_EMAIL_URL (gateway) or NOTIFY_TRANSPORT=smtp with SMTP_HOST/SMTP_FROM.")
        return 1
    sample = render("request_info", s.template_dir, number="000000", ticket_id=0, owner_name="Test recipient",
                    reply="This is a test from the SMS Alerts agent. No action is needed.",
                    client_url=s.osticket_client_url, reply_to=s.notify_reply_to if s.notify_transport == "smtp" else None,
                    signature=s.notify_signature, follow_up_hours=s.request_info_timeout_hours)
    try:
        transport.send(to=args.to, cc=[], subject=sample.subject.replace("[#000000]", "[TEST]"), body=sample.body,
                       message_id=str(_uuid.uuid4()))
    except NotifyError as exc:
        print(f"Send failed: {exc}")
        return 2
    finally:
        transport.close()
    print(f"Sent a test email to {args.to} via {s.notify_transport}.")
    return 0


def cmd_notify_retry(s: Settings, args: argparse.Namespace) -> int:
    audit = AuditLog(s.state_db)
    notifier = Notifier(audit, s, build_transport(s))
    try:
        reset = notifier.retry_failed()
        counts = notifier.dispatch(s.notify_max_attempts) if notifier.enabled else {}
    finally:
        notifier.close()
        audit.close()
    print(f"Re-queued {reset} failed email(s); sent now: {counts}")
    return 0


def cmd_anomalies(s: Settings, args: argparse.Namespace) -> int:
    """Run the anomaly watch now and print what it found."""
    import anthropic

    mb = build_metabase(s)
    audit = AuditLog(s.state_db)
    notifier = Notifier(audit, s, build_transport(s), build_sms_transport(s))
    try:
        ticket_db, checks = build_checks(s, mb)
        client = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=2) if (s.anthropic_api_key and not args.no_model) else None
        service = build_anomaly(s, mb, audit, ticket_db, checks, client, notifier)
        report = service.run(alert=args.alert, use_model=not args.no_model)
    except MetabaseError as exc:
        print(f"Metabase error: {exc}")
        return 2
    finally:
        notifier.close()
        audit.close()
        mb.close()
    summary, analysis = report["summary"], report["analysis"]
    print(f"{report['severity'].upper()}: {summary['headline']}\n")
    print(summary["summary"] + "\n")
    for sig in analysis["signals"]:
        print(f"  [{sig['severity']:8}] {sig['label']}: {sig['value']} (usual {sig['usual']}) - {sig['detail']}")
    for source, comp in analysis["comparisons"].items():
        if comp["findings"]:
            print(f"\nWhat is different about failing {source} ({comp['failed_n']} failed vs {comp['delivered_n']} delivered):")
            for f in comp["findings"][:6]:
                print(f"  {f['label']} = {f['value']}: {f['failed_share']:.0%} of failures vs {f['ok_share']:.0%} of deliveries")
    for name, src in analysis["sources"].items():
        if not src["available"]:
            print(f"\n  (could not read {name}: {src['error']})")
    if report.get("alert"):
        print(f"\nAdmin alert: {report['alert']}")
    return 0


def cmd_report(s: Settings, args: argparse.Namespace) -> int:
    audit = AuditLog(s.state_db)
    count = audit.export_csv(Path(args.out), args.since, args.mode)
    audit.close()
    print(f"Wrote {count} decision(s) since {args.since} to {args.out}")
    return 0


def cmd_compare(s: Settings, args: argparse.Namespace) -> int:
    """Shadow decisions vs what the desk (or the Java agent) actually did to each ticket."""
    audit = AuditLog(s.state_db)
    decisions = audit.latest_per_ticket(args.since, "shadow")
    audit.close()
    mb = build_metabase(s)
    try:
        store = OsTicket(mb, mb.resolve(s.ticket_db_name))
        outcomes = store.outcomes([d["ticket_id"] for d in decisions])
    finally:
        mb.close()
    counts: dict[str, int] = {}
    rows = []
    for d in decisions:
        o = outcomes.get(d["ticket_id"])
        if o is None:
            actual = "missing"
        elif o["state"].lower() == "closed":
            actual = "closed"
        elif o["escalated"]:
            actual = "escalated"
        else:
            actual = "open"
        agent = d["final_action"]
        if (agent, actual) in ((REPLY_AND_CLOSE, "closed"), (ESCALATE, "escalated")):
            outcome = "agree"
        elif agent == REPLY_AND_CLOSE:
            outcome = f"agent_would_close_actual_{actual}"
        elif actual == "closed":
            outcome = f"actual_closed_agent_{agent}"
        else:
            outcome = f"agent_{agent}_actual_{actual}"
        counts[outcome] = counts.get(outcome, 0) + 1
        rows.append([d["ticket_number"], agent, d["confidence"], d["account_statuses"], actual, outcome,
                     (o or {}).get("last_reply") or "", d["reply_text"] or "", d["reason"]])
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticket", "agent_action", "confidence", "agent_statuses", "actual", "outcome",
                         "actual_last_reply", "agent_reply_masked", "agent_reason"])
        writer.writerows(rows)
    for outcome, n in sorted(counts.items()):
        print(f"{outcome:40} {n}")
    print(f"Details: {args.out}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m nmbz --desk sms_alerts", description="SMS Alerts osTicket agent")
    parser.add_argument("--env-file", help="path to a .env file (default: .env in the project folder)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="validate configuration and connectivity")
    p.add_argument("--mode", choices=VALID_MODES)
    p.add_argument("--ping-model", action="store_true", help="also make a tiny model call")

    p = sub.add_parser("run", help="process open SMS Alerts tickets once")
    p.add_argument("--mode", choices=VALID_MODES, help="overrides AGENT_MODE")
    p.add_argument("--limit", type=int, help="max tickets this run")
    p.add_argument("--ticket", help="process only this ticket number (ignores skip rules)")
    p.add_argument("--force", action="store_true", help="ignore skip rules")

    p = sub.add_parser("portal", help="serve the audit and monitoring portal")
    p.add_argument("--host", help="overrides PORTAL_HOST (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="overrides PORTAL_PORT (default 8085)")

    p = sub.add_parser("portal-user", help="manage portal users")
    p.add_argument("action", choices=("add", "remove", "list"))
    p.add_argument("name", nargs="?")
    p.add_argument("--role", choices=("viewer", "admin"), default="viewer")

    p = sub.add_parser("anomalies", help="run the anomaly watch now and print the findings")
    p.add_argument("--alert", action="store_true", help="also alert the admins if this is an incident")
    p.add_argument("--no-model", action="store_true", help="plain summary, no model call")

    p = sub.add_parser("attachments", help="read one ticket's attachments and print what the agent would see")
    p.add_argument("--ticket", required=True, help="ticket number")
    p.add_argument("--local", action="store_true", help="read text-type files only; send nothing to the model")
    p.add_argument("--unmasked", action="store_true", help="print customer numbers as read (not masked)")

    p = sub.add_parser("notify-test", help="send one sample branch email through the configured transport")
    p.add_argument("--to", required=True)

    sub.add_parser("notify-retry", help="re-send branch emails that failed")

    p = sub.add_parser("check-account", help="run the SMS check for one account, no model (ops lookup)")
    p.add_argument("--account", required=True)
    p.add_argument("--phone", help="the number the client expects alerts on")
    p.add_argument("--types", choices=("debits", "salaries", "both"), default="debits")
    p.add_argument("--as-of", help="end of the 15-day window, YYYY-MM-DD (default today)")
    p.add_argument("--trace", action="store_true", help="also run the CAE -> dispatch -> gateway trace for --phone")

    week_ago = (date.today() - timedelta(days=7)).isoformat()
    p = sub.add_parser("audit", help="replay closed tickets and compare with what was posted (no writes)")
    p.add_argument("--since", default=week_ago, help="closed on/after this date (YYYY-MM-DD)")
    p.add_argument("--limit", type=int, default=50, help="max tickets (each one costs model calls)")
    p.add_argument("--window-today", action="store_true", help="15-day window ends today, not at the close date")
    p.add_argument("--out-dir", default=str(BASE_DIR / "reports"))

    p = sub.add_parser("compare", help="shadow decisions vs what actually happened")
    p.add_argument("--since", default=week_ago)
    p.add_argument("--out", default="compare.csv")

    p = sub.add_parser("report", help="export the decision log to CSV")
    p.add_argument("--since", default=week_ago)
    p.add_argument("--mode", choices=VALID_MODES + ("audit",))
    p.add_argument("--out", default="decisions.csv")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = Settings.from_env(args.env_file)
    commands = {"check": cmd_check, "run": cmd_run, "audit": cmd_audit, "report": cmd_report, "compare": cmd_compare,
                "check-account": cmd_check_account, "portal": cmd_portal, "portal-user": cmd_portal_user,
                "notify-test": cmd_notify_test, "notify-retry": cmd_notify_retry,
                "anomalies": cmd_anomalies, "attachments": cmd_attachments}
    return commands[args.command](settings, args)


if __name__ == "__main__":
    sys.exit(main())
