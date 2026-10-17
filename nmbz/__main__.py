"""The service's command line. One .env, one pass over every desk, one console.

    python -m nmbz check [--desk D] [--ping-model]      settings, Metabase databases and prompts for each desk
    python -m nmbz run [--mode M] [--limit N]           one pass over every desk in DESKS (the timer runs this)
    python -m nmbz run --desk ecocash --ticket 123456   one ticket on one desk, ignoring the skip rules
    python -m nmbz portal                               the console for every desk (port 8085)
    python -m nmbz portal-user add dobe --role admin    console users (shared by all desks)
    python -m nmbz audit|compare|report|anomalies|attachments|notify-test|notify-retry --desk D ...
    python -m nmbz check-account --account N ...        SMS Alerts: one account, no model
    python -m nmbz lookup --mobile 077...               EcoCash: a client's link attempts, no model
    python -m nmbz monitor                              EcoCash: follow ecocashreg.service (its own systemd unit)

Desk commands take the same options as before; add --desk when more than one desk has the command.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv

from agentcore import logsafe
from agentcore.config import ROOT
from . import desks as registry

log = logging.getLogger("nmbz")
SERVICE_COMMANDS = ("run", "check", "portal", "portal-user", "desks")
DESK_COMMANDS = ("audit", "compare", "report", "anomalies", "attachments", "notify-test", "notify-retry",
                 "knowledge-seed")


def _logging(verbose: bool) -> None:
    logsafe.setup(verbose)  # customer numbers and secrets never reach the journal


def _service_lock():
    """One service pass at a time (each desk also locks its own pass)."""
    try:
        import fcntl
    except ImportError:  # Windows dev machine: no lock
        return open(os.devnull, "w")
    path = ROOT / "state" / "service.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _forward(desk: registry.Desk, command: str, rest: list[str], env_file: Optional[str], verbose: bool) -> int:
    argv = (["--env-file", env_file] if env_file else []) + (["-v"] if verbose else []) + [command] + rest
    log.debug("%s: %s", desk.key, " ".join(argv))
    return desk.main(argv)


def _which(command: str, desk: Optional[str]) -> registry.Desk:
    if desk:
        chosen = registry.get(desk)
        if command not in DESK_COMMANDS + SERVICE_COMMANDS and command not in chosen.commands:
            raise SystemExit(f"The {chosen.title} desk has no '{command}' command.")
        return chosen
    owners = [d for d in registry.ALL.values() if command in d.commands]
    if len(owners) == 1:
        return owners[0]
    names = ", ".join(d.key for d in registry.enabled())
    raise SystemExit(f"Say which desk: --desk {{{names}}}")


def cmd_run(args: argparse.Namespace, rest: list[str]) -> int:
    if args.desk:
        if registry.service_stop_file().exists():
            log.warning("Service kill switch %s exists; not running", registry.service_stop_file())
            return 0
        return _forward(registry.get(args.desk), "run", rest, args.env_file, args.verbose)
    if any(x == "--ticket" or x.startswith("--ticket=") for x in rest):
        raise SystemExit("--ticket needs --desk (a ticket is run through one desk only)")
    if registry.service_stop_file().exists():
        log.warning("Service kill switch %s exists; not running", registry.service_stop_file())
        return 0
    lock = _service_lock()
    if lock is None:
        log.warning("Another service pass is still running; skipping this one")
        return 0
    worst = 0
    try:
        for desk in registry.enabled():
            if registry.service_stop_file().exists():
                log.warning("Service kill switch appeared; stopping before %s", desk.title)
                break
            log.info("--- %s desk ---", desk.title)
            try:
                code = _forward(desk, "run", rest, args.env_file, args.verbose)
            except Exception:  # noqa: BLE001 - one desk failing must not stop the others
                log.exception("%s desk failed", desk.title)
                code = 3
            worst = max(worst, code)
    finally:
        lock.close()
    return worst


def cmd_check(args: argparse.Namespace, rest: list[str]) -> int:
    chosen = [registry.get(args.desk)] if args.desk else registry.enabled()
    worst = 0
    for desk in chosen:
        print(f"=== {desk.title} desk ===")
        worst = max(worst, _forward(desk, "check", rest, args.env_file, args.verbose))
    return worst


def cmd_portal(args: argparse.Namespace, rest: list[str]) -> int:
    from .console import serve

    p = argparse.ArgumentParser(prog="python -m nmbz portal")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    opts = p.parse_args(rest)
    return serve(args.env_file, host=opts.host, port=opts.port)


def cmd_portal_user(args: argparse.Namespace, rest: list[str]) -> int:
    first = registry.enabled()[0]  # every desk reads the same PORTAL_USERS_FILE
    return _forward(first, "portal-user", rest, args.env_file, args.verbose)


def cmd_desks(args: argparse.Namespace, rest: list[str]) -> int:
    on = {d.key for d in registry.enabled()}
    for desk in registry.ALL.values():
        s = desk.settings(args.env_file)
        print(f"{desk.key:12} {'on ' if desk.key in on else 'off'}  {desk.title:12} mode={s.mode:7} "
              f"log={s.state_db}  {desk.blurb}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = SERVICE_COMMANDS + DESK_COMMANDS + tuple(c for d in registry.ALL.values() for c in d.commands)
    parser = argparse.ArgumentParser(prog="python -m nmbz", description="NMBZ helpdesk agents", add_help=False,
                                     epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-h", "--help", action="store_true", help="this help; after a command, that command's help")
    parser.add_argument("--env-file", help="path to the .env file (default: .env in the project folder)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--desk", help=f"one desk: {', '.join(registry.ALL)}")
    parser.add_argument("command", nargs="?", choices=commands)
    args, rest = parser.parse_known_args(argv)
    if args.command is None or (args.help and args.command in ("portal", "desks")):
        parser.print_help()
        return 0 if args.help else 2
    if args.help:  # the desk's own help for this command
        rest = rest + ["--help"]
        args.desk = args.desk or (registry.enabled()[0].key if args.command in ("run", "check", "portal-user") else None)
    load_dotenv(args.env_file or ROOT / ".env", override=False)
    _logging(args.verbose)
    service = {"run": cmd_run, "check": cmd_check, "portal": cmd_portal, "portal-user": cmd_portal_user,
               "desks": cmd_desks}
    try:
        if args.command in service:
            return service[args.command](args, rest)
        desk = _which(args.command, args.desk)
        return _forward(desk, args.command, rest, args.env_file, args.verbose)
    except ValueError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
