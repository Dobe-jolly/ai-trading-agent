"""Log files into records: one record per application log line, with the lines that follow it (a stack trace, a menu,
an ISO message dump) attached to it. Reads journalctl output (`Oct 17 14:57:49 host unit[pid]: ...`) or the
application's own file, carrying Spring Boot logback lines:

    2026-10-17 14:57:49.622  INFO [requestId=..] [correlationId=..] 3272 --- [o-9091-exec-396] c.n.i.c.Controller : message

The timestamp is the application's own (local time of the server, plus the profile's clock offset)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

from .profiles import Profile

JOURNAL = re.compile(r"^[A-Z][a-z]{2}\s+\d+ \d\d:\d\d:\d\d \S+ [^\[:\s]+(?:\[\d+\])?: ?(?P<rest>.*)$")
SPRING = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[.,]\d{3}\s+(?P<level>[A-Za-z]+)\s+(?P<mdc>(?:\[[^\]]*\]\s+){0,3})"
                    r"(?P<pid>\d+) --- \[\s*(?P<thread>[^\]]*)\]\s+(?P<logger>\S+)\s*: ?(?P<msg>.*)$")
LEVELS = {"WARNING": "WARN", "SEVERE": "ERROR", "FATAL": "ERROR", "CRITICAL": "ERROR", "TRACE": "DEBUG"}
MAX_EXTRA_LINES, MAX_EXTRA_CHARS = 80, 6000


@dataclass
class Record:
    ts: str  # local time, YYYY-MM-DD HH:MM:SS
    level: str
    logger: str
    thread: str
    message: str
    extra: list[str] = field(default_factory=list)  # the lines after it (stack trace, dump), capped
    mdc: str = ""  # the bracketed context before the process id ([requestId=..] [correlationId=..]), when there is one

    @property
    def text(self) -> str:
        return "\n".join([self.message, *self.extra])


def _shift(ts: str, seconds: int) -> str:
    if not seconds:
        return ts
    return (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def read_records(path: Path | str, profile: Profile) -> Iterator[Record]:
    cur: Optional[Record] = None
    chars = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            rest = line
            if profile.format == "journald":
                j = JOURNAL.match(line)
                if j:
                    rest = j["rest"]
                elif line.startswith("-- "):  # journalctl's "Logs begin at ..." banner
                    continue
            s = SPRING.match(rest)
            if s:
                if cur is not None:
                    yield cur
                level = s["level"].upper()
                cur = Record(_shift(s["ts"], profile.clock_offset), LEVELS.get(level, level), s["logger"], s["thread"],
                             s["msg"], mdc=s["mdc"].strip())
                chars = 0
            elif cur is not None and rest.strip():
                if len(cur.extra) < MAX_EXTRA_LINES and chars < MAX_EXTRA_CHARS:
                    cur.extra.append(rest.rstrip())
                    chars += len(rest)
    if cur is not None:
        yield cur
