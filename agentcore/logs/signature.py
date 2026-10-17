"""The signature of a WARN or ERROR record: the same problem, whatever its numbers, ids and timestamps.

A signature is the service, level, logger, the message with its variable parts replaced, the exception class and the
first application frame of its stack trace. Counting signatures per minute is what makes "is this new?" and "is this
spiking?" answerable without reading lines."""
from __future__ import annotations

import hashlib
import re
from typing import Optional

from .parse import Record

EXC = re.compile(r"\b((?:[a-z_][a-z0-9_]*\.)+[A-Z][A-Za-z0-9_$]*(?:Exception|Error|Failure|Timeout))\b")
FRAME = re.compile(r"^\s*at ([\w.$<>/]+)\(([^)]*)\)")
# a value that identifies one request, whatever it looks like: requestId=..., sessionId=..., uuid=..., rrn=..., xxxRef=...
ID_FIELD = re.compile(r"\b([A-Za-z_]*(?:Id|ID|_id|Ref|Reference|Number|Uuid|UUID|uuid|rrn|RRN))=([^\s,;&)}\]]+)")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def template(message: str, limit: int = 220) -> str:  # long enough to keep the reason a failure line ends with
    text = ID_FIELD.sub(lambda m: m.group(1) + "=<ID>", message)
    text = re.sub(r"<[A-Z_0-9]+(?::[a-p]+)?>|<MASKED>|<HEX>|<JWT>", "<X>", text)
    text = re.sub(r"[A-Za-z]{0,6}<X>[A-Za-z]{0,6}(?![A-Za-z])", "<X>", text)  # an id like MB<digits>AN is one variable
    text = UUID.sub("<UUID>", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", "<IP>", text)
    text = re.sub(r"\d+(\.\d+)?", "N", text)
    text = re.sub(r"[=*\-.>@:_<~]{4,}", "~", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def exception_of(record: Record) -> Optional[str]:
    for line in [record.message, *record.extra]:
        m = EXC.search(line)
        if m:
            return m.group(1)
    return None


def frame_of(record: Record, packages: list[str]) -> Optional[str]:
    """The first stack frame in the application's own packages (else the first frame): class.method(file:line)."""
    first = None
    for line in record.extra:
        m = FRAME.match(line)
        if not m:
            continue
        frame = f"{m.group(1)}({m.group(2)})"
        first = first or frame
        if any(m.group(1).startswith(p) for p in packages):
            return frame
    return first


def signature(service: str, record: Record, packages: list[str]) -> tuple[str, str, Optional[str], Optional[str]]:
    """(id, template, exception, frame)."""
    tmpl, exc, frame = template(record.message), exception_of(record), frame_of(record, packages)
    if not tmpl and exc:
        tmpl = "(no message)"
    key = "|".join([service, record.level, record.logger.split(".")[-1], tmpl, exc or "", frame or ""])
    return hashlib.sha1(key.encode()).hexdigest()[:12], tmpl, exc, frame


def trim_detail(record: Record, packages: list[str], max_chars: int = 1200) -> str:
    """What to keep of a record's following lines: the exception lines and the first few frames (application ones first)."""
    keep, frames = [], []
    for line in record.extra:
        (frames if FRAME.match(line) else keep).append(line.strip())
    app = [f for f in frames if any(f[3:].startswith(p) for p in packages)]
    shown = (app + [f for f in frames if f not in app])[:4]
    return "\n".join(keep[:8] + shown)[:max_chars]
