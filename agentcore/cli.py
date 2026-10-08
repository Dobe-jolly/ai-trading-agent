"""Commands every desk has, written once. A desk subclasses DeskCli with its builders and its own commands.

    python -m nmbz --desk <desk> run | check | audit | compare | report | attachments | notify-test | notify-retry
                                 | portal | portal-user
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from .audit import AuditLog
from .controls import effective_mode, read_mode_override
from .decision import ESCALATE, REPLY_AND_CLOSE
from .metabase import MetabaseClient, MetabaseError
from .pii import Pseudonymizer

log = logging.getLogger("agent")
VALID_MODES = ("shadow", "notes", "live")


class DeskCli:
    """What a desk supplies: its settings class, its builders, and any commands of its own."""

    desk = "desk"            # the --desk value
    title = "Desk"           # e.g. "Failed transactions"
    settings_cls: Any = None
    base_dir: Path = Path(".")

    # ---- builders (the desk overrides these) ----------------------------------------------------
    def build_store(self, s: Any, mb: MetabaseClient) -> Any:
        raise NotImplementedError

    def build_runner(self, s: Any, mode: str, mb: MetabaseClient, audit: AuditLog) -> Any:
        raise NotImplementedError

    def build_reader(self, s: Any, store: Any, client: Any, mode: Optional[str] = None) -> Any:
        raise NotImplementedError

    def blank_ticket(self, number: str) -> Any:
        """A ticket object to load a thread into by number (for the attachments command)."""
        raise NotImplementedError

    def notify_module(self) -> Any:
        """The desk's notify module (Notifier, render, build_transport, NotifyError)."""
        raise NotImplementedError

    def render_attachment(self, item: Any) -> str:
        raise NotImplementedError

    def check(self, s: Any, args: argparse.Namespace) -> int:
        """Desk-specific connectivity checks after the settings check; return 0 when all is well."""
        return 0

    def add_commands(self, sub: Any) -> dict[str, Callable[[Any, argparse.Namespace], int]]:
        """Extra subcommands: add parsers to sub and return {name: handler}."""
        return {}

    # ---- shared helpers ---------------------------------------------------------------------------
    @staticmethod
    def build_metabase(s: Any) -> MetabaseClient:
        return MetabaseClient(
            s.metabase_host, s.metabase_api_key or "", timeout=s.metabase_timeout,
            retry_count=s.metabase_retry_count, retry_delay=s.metabase_retry_delay, verify=s.metabase_verify,
        )

    @staticmethod
    def run_lock(s: Any):
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

    @staticmethod
    def load_prompt(s: Any, **values: Any) -> str:
        text = s.prompt_file.read_text(encoding="utf-8")
        text = re.sub(r"<!--.*?-->\s*", "", text, flags=re.S)
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", str(value))
        return text

    # ---- commands ---------------------------------------------------------------------------------
    def cmd_check(self, s: Any, args: argparse.Namespace) -> int:
        ok = True
        for problem in s.problems(args.mode):
            print(f"[FAIL] {problem}")
            ok = False
        for warning in s.warnings(args.mode):
            print(f"[WARN] {warning}")
        if not ok:
            return 1
        code = self.check(s, args)
        if code == 0 and args.ping_model:
            try:
                import anthropic

                client = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=1)
                client.messages.create(model=s.model, max_tokens=5, messages=[{"role": "user", "content": "ping"}])
                print(f"[ OK ] Claude answers ({s.model})")
            except Exception as exc:  # noqa: BLE001
                print(f"[FAIL] Claude: {exc}")
                code = 1
        print("All good." if code == 0 else "Fix the items above.")
        return code

    def cmd_run(self, s: Any, args: argparse.Namespace) -> int:
        configured = args.mode or s.mode
        mode = effective_mode(configured, read_mode_override(s.mode_override_file))
        if mode != configured:
            log.warning("Mode held at %s by the console override (configured: %s)", mode, configured)
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
        lock = self.run_lock(s)
        if lock is None:
            log.warning("Another pass is still running; skipping this one")
            return 0
        mb = self.build_metabase(s)
        audit = AuditLog(s.state_db)
        try:
            runner = self.build_runner(s, mode, mb, audit)
            summary = runner.run(limit=args.limit, ticket_number=args.ticket, force=args.force)
        except MetabaseError as exc:
            log.error("Metabase unavailable: %s", exc)
            return 2
        finally:
            audit.close()
            mb.close()
            lock.close()
        return 0 if summary.errors == 0 else 3

    def cmd_audit(self, s: Any, args: argparse.Namespace) -> int:
        problems = s.problems("shadow")
        if problems:
            for problem in problems:
                log.error("Config: %s", problem)
            return 1
        mb = self.build_metabase(s)
        audit = AuditLog(s.state_db)
        try:
            runner = self.build_runner(s, "shadow", mb, audit)
            out = runner.audit_closed(date.fromisoformat(args.since), args.limit, args.window_today, Path(args.out_dir))
            print(f"Wrote {out}")
        except MetabaseError as exc:
            log.error("Metabase unavailable: %s", exc)
            return 2
        finally:
            audit.close()
            mb.close()
        return 0

    def cmd_report(self, s: Any, args: argparse.Namespace) -> int:
        audit = AuditLog(s.state_db)
        count = audit.export_csv(Path(args.out), args.since, args.mode)
        audit.close()
        print(f"Wrote {count} decision(s) since {args.since} to {args.out}")
        return 0

    def cmd_compare(self, s: Any, args: argparse.Namespace) -> int:
        """Shadow decisions vs what the desk actually did to each ticket."""
        audit = AuditLog(s.state_db)
        decisions = audit.latest_per_ticket(args.since, "shadow")
        audit.close()
        mb = self.build_metabase(s)
        try:
            outcomes = self.build_store(s, mb).outcomes([d["ticket_id"] for d in decisions])
        finally:
            mb.close()
        counts: dict[str, int] = {}
        rows = []
        for d in decisions:
            o = outcomes.get(d["ticket_id"])
            actual = ("missing" if o is None else "closed" if o["state"].lower() == "closed"
                      else "escalated" if o["escalated"] else "open")
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

    def cmd_attachments(self, s: Any, args: argparse.Namespace) -> int:
        """Read the attachments on one ticket and print what the agent would see. No decision, no writes."""
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
        mb = self.build_metabase(s)
        try:
            store = self.build_store(s, mb)
            ticket = self.blank_ticket(str(args.ticket))
            store.load_thread(ticket)
            if ticket.thread_id is None:
                print(f"Ticket #{args.ticket} not found")
                return 1
            result = self.build_reader(s, store, client, mode).read(ticket)
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
            text = self.render_attachment(item)
            print(text if args.unmasked else pseudo.mask(text))
            print()
        print(f"Model tokens: {result.input_tokens} in, {result.output_tokens} out "
              f"({'values shown unmasked' if args.unmasked else 'numbers masked as the agent sees them'})")
        return 0

    def cmd_notify_test(self, s: Any, args: argparse.Namespace) -> int:
        """Send one sample email through the configured transport (no queue, no ticket)."""
        notify = self.notify_module()
        transport = notify.build_transport(s)
        if transport is None:
            print("Branch notifications are off. Set NOTIF_EMAIL_URL (gateway) or NOTIFY_TRANSPORT=smtp with "
                  "SMTP_HOST/SMTP_FROM.")
            return 1
        sample = notify.render("request_info", s.template_dir, number="000000", ticket_id=0, owner_name="Test recipient",
                               reply=f"This is a test from the {self.title} agent. No action is needed.",
                               client_url=s.osticket_client_url,
                               reply_to=s.notify_reply_to if s.notify_transport == "smtp" else None,
                               signature=s.notify_signature, follow_up_hours=s.request_info_timeout_hours)
        try:
            transport.send(to=args.to, cc=[], subject=sample.subject.replace("[#000000]", "[TEST]"), body=sample.body,
                           message_id=str(uuid.uuid4()))
        except notify.NotifyError as exc:
            print(f"Send failed: {exc}")
            return 2
        finally:
            transport.close()
        print(f"Sent a test email to {args.to} via {s.notify_transport}.")
        return 0

    def cmd_notify_retry(self, s: Any, args: argparse.Namespace) -> int:
        notify = self.notify_module()
        audit = AuditLog(s.state_db)
        notifier = notify.Notifier(audit, s, notify.build_transport(s))
        try:
            reset = notifier.retry_failed()
            counts = notifier.dispatch(s.notify_max_attempts) if notifier.enabled else {}
        finally:
            notifier.close()
            audit.close()
        print(f"Re-queued {reset} failed email(s); sent now: {counts}")
        return 0

    def cmd_portal(self, s: Any, args: argparse.Namespace) -> int:
        """The console serves every desk (python -m nmbz portal); started from here it is the same console."""
        from nmbz.console import serve

        return serve(args.env_file, host=args.host, port=args.port)

    def cmd_portal_user(self, s: Any, args: argparse.Namespace) -> int:
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

    # ---- parsing ----------------------------------------------------------------------------------
    def parser(self) -> tuple[argparse.ArgumentParser, dict[str, Callable[[Any, argparse.Namespace], int]]]:
        parser = argparse.ArgumentParser(prog=f"python -m nmbz --desk {self.desk}", description=f"{self.title} desk")
        parser.add_argument("--env-file", help="path to a .env file (default: .env in the project folder)")
        parser.add_argument("-v", "--verbose", action="store_true")
        sub = parser.add_subparsers(dest="command", required=True)

        p = sub.add_parser("check", help="validate configuration and connectivity")
        p.add_argument("--mode", choices=VALID_MODES)
        p.add_argument("--ping-model", action="store_true", help="also make a tiny model call")

        p = sub.add_parser("run", help=f"process open {self.title} tickets once")
        p.add_argument("--mode", choices=VALID_MODES, help="overrides AGENT_MODE")
        p.add_argument("--limit", type=int, help="max tickets this run")
        p.add_argument("--ticket", help="process only this ticket number (ignores skip rules)")
        p.add_argument("--force", action="store_true", help="ignore skip rules")

        p = sub.add_parser("portal", help="serve the console (every desk)")
        p.add_argument("--host")
        p.add_argument("--port", type=int)

        p = sub.add_parser("portal-user", help="manage console users")
        p.add_argument("action", choices=("add", "remove", "list"))
        p.add_argument("name", nargs="?")
        p.add_argument("--role", choices=("viewer", "admin"), default="viewer")

        p = sub.add_parser("attachments", help="read one ticket's attachments and print what the agent would see")
        p.add_argument("--ticket", required=True, help="ticket number")
        p.add_argument("--local", action="store_true", help="read text-type files only; send nothing to the model")
        p.add_argument("--unmasked", action="store_true", help="print customer numbers as read (not masked)")

        p = sub.add_parser("notify-test", help="send one sample branch email through the configured transport")
        p.add_argument("--to", required=True)
        sub.add_parser("notify-retry", help="re-send branch emails that failed")

        week_ago = (date.today() - timedelta(days=7)).isoformat()
        p = sub.add_parser("audit", help="replay closed tickets and compare with what was posted (no writes)")
        p.add_argument("--since", default=week_ago, help="closed on/after this date (YYYY-MM-DD)")
        p.add_argument("--limit", type=int, default=50, help="max tickets (each one costs model calls)")
        p.add_argument("--window-today", action="store_true", help="look-ups end today, not at the close date")
        p.add_argument("--out-dir", default=str(self.base_dir / "reports"))

        p = sub.add_parser("compare", help="shadow decisions vs what actually happened")
        p.add_argument("--since", default=week_ago)
        p.add_argument("--out", default="compare.csv")

        p = sub.add_parser("report", help="export the decision log to CSV")
        p.add_argument("--since", default=week_ago)
        p.add_argument("--mode", choices=VALID_MODES + ("audit",))
        p.add_argument("--out", default="decisions.csv")

        commands = {"check": self.cmd_check, "run": self.cmd_run, "audit": self.cmd_audit, "report": self.cmd_report,
                    "compare": self.cmd_compare, "portal": self.cmd_portal, "portal-user": self.cmd_portal_user,
                    "notify-test": self.cmd_notify_test, "notify-retry": self.cmd_notify_retry,
                    "attachments": self.cmd_attachments}
        commands.update(self.add_commands(sub))
        return parser, commands

    def main(self, argv: Optional[list[str]] = None) -> int:
        parser, commands = self.parser()
        args = parser.parse_args(argv)
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        for noisy in ("httpx", "httpcore", "anthropic"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        settings = self.settings_cls.from_env(args.env_file)
        return commands[args.command](settings, args)


def run(cli: DeskCli) -> None:
    sys.exit(cli.main())
