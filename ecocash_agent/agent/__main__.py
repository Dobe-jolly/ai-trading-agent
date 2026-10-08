"""Command line entry point.

    python -m agent check [--ping-model]            validate config, Metabase databases, prompt
    python -m agent run [--mode shadow|notes|live]  one pass over open EcoCash tickets
    python -m agent run --ticket 123456             one ticket, ignoring the skip rules
    python -m agent lookup --mobile 0771234567      a client's link attempts, link record, account (no model)
    python -m agent monitor                         follow ecocashreg.service and record every attempt
    python -m agent monitor --from-file log.txt     import a saved journal export (add --dry-run to just count)
    python -m agent audit --since 2026-09-01        replay closed tickets, compare with what was posted
    python -m agent compare --since 2026-10-01      shadow decisions vs what actually happened
    python -m agent report --since 2026-10-01       export the decision log to CSV
    python -m agent attachments --ticket 123456     what the agent would read from a ticket's files
    python -m agent portal                          the audit and monitoring portal (port 8086)
    python -m agent portal-user add dobe --role admin
    python -m agent notify-test --to you@example.com   one sample branch email
    python -m agent notify-retry                      re-send emails that failed
    python -m agent anomalies [--alert]               run the anomaly watch now
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import signal
import sys
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from .audit import AuditLog
from .config import BASE_DIR, VALID_MODES, Settings
from .controls import effective_mode, read_mode_override
from .decision import ESCALATE, REPLY_AND_CLOSE
from .ecocash import AttemptStore, BankAccounts, LinkRecords
from .metabase import MetabaseClient, MetabaseError
from .notify import Notifier, NotifyError, build_sms_transport, build_transport, render
from .osticket import OsTicket
from .tools import Toolbox

log = logging.getLogger("agent")


def build_metabase(s: Settings) -> MetabaseClient:
    return MetabaseClient(
        s.metabase_host, s.metabase_api_key or "", timeout=s.metabase_timeout,
        retry_count=s.metabase_retry_count, retry_delay=s.metabase_retry_delay, verify=s.metabase_verify,
    )


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
    text = s.prompt_file.read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->\s*", "", text, flags=re.S)
    return (text.replace("{{LOOKBACK_DAYS}}", str(s.lookback_days))
                .replace("{{MAX_INFO_REQUESTS}}", str(s.max_info_requests)))


def build_runner(s: Settings, mode: str, mb: MetabaseClient, audit: AuditLog):
    import anthropic

    from .loop import TicketAgent
    from .runner import Runner
    from .writer import OsTicketWriter, ShadowWriter

    store = build_store(s, mb)
    toolbox = build_toolbox(s, mb)
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
    print(f"[tickets] topics {s.ecocash_topics}; also tickets mentioning {s.ecocash_keywords} from the last "
          f"{s.ecocash_content_days} days, except under {s.ecocash_exclude_topics}")
    if s.metabase_api_key:
        mb = build_metabase(s)
        try:
            for label, name, required in (("ticket", s.ticket_db_name, True), ("ecocash", s.ecocash_db_name, True),
                                          ("postilion", s.postilion_db_name, False), ("standby", s.standby_db_name, False)):
                db_id = mb.try_resolve(name)
                print(f"[metabase] {label:9} '{name}': {db_id if db_id is not None else 'NOT FOUND'}")
                ok &= db_id is not None or not required
            if mb.try_resolve(s.ticket_db_name) is not None:
                store = build_store(s, mb)
                tickets = store.open_tickets()
                by_topic = sum(1 for t in tickets if t.topic_match)
                print(f"[osticket] {len(tickets)} open EcoCash ticket(s): {by_topic} by topic, "
                      f"{len(tickets) - by_topic} by content")
                if s.reply_staff_id:
                    print(f"[osticket] replies will be posted as: {store.staff_poster_name(s.reply_staff_id)}")
            if mb.try_resolve(s.ecocash_db_name) is not None:
                toolbox = build_toolbox(s, mb)
                first = toolbox.attempts.attempts_start()
                print(f"[ecocash] {s.attempts_table}: " + (f"recording since {first:%d-%m-%Y %H:%M}" if first
                                                           else "empty (start the monitor)"))
                if toolbox.link_records is not None:
                    mb.query(toolbox.link_records.db, f"SELECT TOP 1 date_added FROM {s.postilion_table}")
                    print(f"[postilion] {s.postilion_table} readable")
                if toolbox.accounts is not None:
                    mb.query(toolbox.accounts.db, "SELECT 1 AS ok FROM NMBPRODIDC.IACLINK WHERE ROWNUM <= 1")
                    print("[standby] NMBPRODIDC.IACLINK readable")
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


def cmd_lookup(s: Settings, args: argparse.Namespace) -> int:
    """No model: a client's link attempts, the bank's link record and the account (as the agent sees them, unmasked)."""
    if not (args.mobile or args.account or args.id_number):
        print("Give --mobile, --account or --id-number")
        return 1
    mb = build_metabase(s)
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


def cmd_monitor(s: Settings, args: argparse.Namespace) -> int:
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
            mb = build_metabase(s)
            store = MetabaseStore(mb, mb.resolve(s.ecocash_db_name), legacy=s.monitor_legacy_table)
        else:
            store = PostgresStore(jdbc_to_conninfo(s.monitor_db_url or "", s.monitor_db_user, s.monitor_db_password),
                                  legacy=s.monitor_legacy_table)
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


def cmd_attachments(s: Settings, args: argparse.Namespace) -> int:
    """Read the attachments on one ticket and print what the agent would see. No decision, no writes."""
    from .attachments import render_attachment
    from .osticket import Ticket
    from .pii import Pseudonymizer

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
        store = build_store(s, mb)
        ticket = Ticket(ticket_id=0, number=str(args.ticket), help_topic="", created="")
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
    from dataclasses import replace

    from .portal.server import serve

    if args.host or args.port:
        s = replace(s, portal_host=args.host or s.portal_host, portal_port=args.port or s.portal_port)
    serve(s)
    return 0


def cmd_portal_user(s: Settings, args: argparse.Namespace) -> int:
    import getpass

    from .portal.users import UserStore

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
                    reply="This is a test from the EcoCash agent. No action is needed.",
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
        toolbox = build_toolbox(s, mb)
        client = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=2) if (s.anthropic_api_key and not args.no_model) else None
        service = build_anomaly(s, mb, audit, mb.resolve(s.ticket_db_name), toolbox, client, notifier)
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
            print(f"\nWhat is different about failing {source} ({comp['failed_n']} failed vs {comp['linked_n']} linked):")
            for f in comp["findings"][:6]:
                print(f"  {f['label']} = {f['value']}: {f['failed_share']:.0%} of failures vs {f['ok_share']:.0%} of links")
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
    """Shadow decisions vs what the desk actually did to each ticket."""
    audit = AuditLog(s.state_db)
    decisions = audit.latest_per_ticket(args.since, "shadow")
    audit.close()
    mb = build_metabase(s)
    try:
        store = build_store(s, mb)
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
    parser = argparse.ArgumentParser(prog="python -m agent", description="EcoCash osTicket agent")
    parser.add_argument("--env-file", help="path to a .env file (default: .env in the project folder)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="validate configuration and connectivity")
    p.add_argument("--mode", choices=VALID_MODES)
    p.add_argument("--ping-model", action="store_true", help="also make a tiny model call")

    p = sub.add_parser("run", help="process open EcoCash tickets once")
    p.add_argument("--mode", choices=VALID_MODES, help="overrides AGENT_MODE")
    p.add_argument("--limit", type=int, help="max tickets this run")
    p.add_argument("--ticket", help="process only this ticket number (ignores skip rules)")
    p.add_argument("--force", action="store_true", help="ignore skip rules")

    p = sub.add_parser("portal", help="serve the audit and monitoring portal")
    p.add_argument("--host", help="overrides PORTAL_HOST (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="overrides PORTAL_PORT (default 8086)")

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

    p = sub.add_parser("lookup", help="a client's link attempts, link record and account, no model")
    p.add_argument("--mobile")
    p.add_argument("--account")
    p.add_argument("--id-number")
    p.add_argument("--days", type=int, default=60)

    p = sub.add_parser("monitor", help="follow ecocashreg.service and record every link/delink attempt")
    p.add_argument("--from-file", help="import a saved `journalctl -u ecocashreg.service` text export instead")
    p.add_argument("--dry-run", action="store_true", help="parse and count only; write nothing")

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
                "lookup": cmd_lookup, "monitor": cmd_monitor, "portal": cmd_portal, "portal-user": cmd_portal_user,
                "notify-test": cmd_notify_test, "notify-retry": cmd_notify_retry,
                "anomalies": cmd_anomalies, "attachments": cmd_attachments}
    return commands[args.command](settings, args)


if __name__ == "__main__":
    sys.exit(main())
