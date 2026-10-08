"""Following ecocashreg.service's journal and saving each attempt as it finishes.

Runs on the integrations host (systemd: deploy/ecocash-monitor.service), replacing the Java
ecocash-registration-monitor.

    journalctl -u ecocashreg.service -f -o json --no-pager [--cursor=C | --after-cursor=C | -n 0]

Resuming: the journal position is saved in MONITOR_POSITION_FILE. While an attempt is still
waiting for EcoCash's answer, the saved position is that attempt's first line, so after a
restart the request is read again and its answer is matched; anything already saved is skipped
by the database (unique attempt_key). With no saved position the monitor starts at the end of
the journal, like the Java's `-n 0`.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .parser import Attempt, JournalParser
from .store import Store

log = logging.getLogger(__name__)


def load_position(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("cursor") else None


def save_position(path: Path, cursor: str, inclusive: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"cursor": cursor, "inclusive": inclusive, "saved_at": datetime.now().isoformat()}),
                   encoding="utf-8")
    os.replace(tmp, path)


def message_of(entry: dict[str, Any]) -> str:
    msg = entry.get("MESSAGE")
    if isinstance(msg, list):  # journald gives non-UTF-8 messages as byte arrays
        return bytes(int(b) & 0xFF for b in msg).decode("utf-8", errors="replace")
    return "" if msg is None else str(msg)


class JournalFollower:
    def __init__(self, parser: JournalParser, store: Store, *, unit: str, position_file: Path,
                 journalctl: str = "journalctl", restart_delay: float = 5.0, save_every: float = 5.0,
                 popen: Callable[..., Any] = subprocess.Popen, now: Callable[[], datetime] = datetime.now) -> None:
        self.parser = parser
        self.store = store
        self.unit = unit
        self.position_file = position_file
        self.journalctl = journalctl
        self.restart_delay = restart_delay
        self.save_every = save_every
        self.popen = popen
        self.now = now
        self.unsaved: list[Attempt] = []
        self._first_cursor: dict[str, tuple[int, str]] = {}  # thread -> (id(attempt), cursor of its first line)
        self._last_cursor: Optional[str] = None
        self._last_save = 0.0
        self.saved = 0

    def command(self) -> list[str]:
        cmd = [self.journalctl, "-u", self.unit, "-f", "-o", "json", "--no-pager"]
        position = load_position(self.position_file)
        if position:
            cmd.append(("--cursor=" if position.get("inclusive") else "--after-cursor=") + str(position["cursor"]))
        else:
            cmd += ["-n", "0"]
        return cmd

    # ---- one journal entry -------------------------------------------------------------
    def handle(self, line: str) -> None:
        try:
            entry = json.loads(line)
        except ValueError:
            return
        cursor = entry.get("__CURSOR")
        done = self.parser.feed_message(message_of(entry), entry.get("_HOSTNAME"))
        if cursor:
            self._last_cursor = cursor
            for thread, attempt in self.parser.pending.items():
                tracked = self._first_cursor.get(thread)
                if tracked is None or tracked[0] != id(attempt):
                    self._first_cursor[thread] = (id(attempt), cursor)
            for thread in list(self._first_cursor):
                if thread not in self.parser.pending:
                    del self._first_cursor[thread]
        self._store(done)

    def tick(self, idle: bool) -> None:
        """Time out unanswered attempts, retry unsaved ones, and save the journal position."""
        reference = self.now() if idle else (self.parser.last_seen or self.now())
        self._store(self.parser.expire(reference))
        if self.unsaved:
            self._store([])
        if time.monotonic() - self._last_save >= self.save_every:
            self.save_position()

    def _store(self, done: list[Attempt]) -> None:
        batch = self.unsaved + done
        if not batch:
            return
        try:
            self.saved += self.store.save(batch)
            self.unsaved = []
            for a in done:
                log.info("%s %s via %s: %s%s", a.operation.lower(), a.outcome, a.via or "?",
                         a.response_code or "-", f" {a.message}" if a.outcome != "linked" and a.message else "")
        except Exception as exc:  # noqa: BLE001 - keep reading; retry on the next tick
            self.unsaved = batch[-5000:]
            log.error("Could not save %d attempt(s), will retry: %s", len(batch), exc)

    def save_position(self) -> None:
        """Only when everything finished so far is saved; else a restart replays from the last good point."""
        if self.unsaved or self._last_cursor is None:
            return
        live = [t for t in self._first_cursor if t in self.parser.pending]
        self._first_cursor = {t: self._first_cursor[t] for t in live}
        if live:
            oldest = min(live, key=lambda t: self.parser.pending[t].requested_at)
            save_position(self.position_file, self._first_cursor[oldest][1], inclusive=True)
        else:
            save_position(self.position_file, self._last_cursor, inclusive=False)
        self._last_save = time.monotonic()

    # ---- the loop -------------------------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        self.store.ensure_schema()
        while not stop.is_set():
            cmd = self.command()
            log.info("Following: %s", " ".join(c if not c.startswith("--") or "cursor" not in c else c.split("=")[0] + "=…"
                                              for c in cmd))
            try:
                proc = self.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            except OSError as exc:
                log.error("Cannot start journalctl: %s", exc)
                stop.wait(self.restart_delay)
                continue
            lines: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=10000)
            reader = threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True)
            reader.start()
            while not stop.is_set():
                try:
                    line = lines.get(timeout=1.0)
                except queue.Empty:
                    self.tick(idle=True)
                    continue
                if line is None:
                    break
                self.handle(line)
                if lines.empty():
                    self.tick(idle=False)
            _terminate(proc)
            self.save_position()
            if not stop.is_set():
                log.warning("journalctl ended (exit %s); restarting in %ss", proc.poll(), self.restart_delay)
                stop.wait(self.restart_delay)
        self.save_position()
        self.store.close()


def _pump(stream: Any, out: "queue.Queue[Optional[str]]") -> None:
    try:
        for line in stream:
            out.put(line)
    finally:
        out.put(None)


def _terminate(proc: Any) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
