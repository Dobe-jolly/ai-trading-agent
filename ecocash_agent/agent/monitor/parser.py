"""Reading the ecocashreg.service journal: one record per EcoCash link or unlink attempt.

Replaces the Java ecocash-registration-monitor's parser, with these differences:
- every attempt is recorded, successes included, so a later successful link (or a delink)
  can be confirmed and failure rates measured;
- unlink requests (ECOCASH_DEREGISTRATION, sent from the branch) are recorded too;
- a request with no response becomes a `no_response` record instead of lingering and being
  attached to someone else's failure later;
- the un-threaded "Message:" lines are used only when exactly one attempt is waiting for its
  error, never to guess between customers.

How ecocashreg.service logs one attempt (all on the same request thread, synchronously):
    ... ECOCASH REGISTER REQUEST ....NMBZApiRequest{operation='ECOCASH_REGISTRATION', channel=..., requestBody={...}}
    ... ECOCASH REG REQUEST  BODY ***EcocashRegRequest{serviceType='BANKREG', externalReference='...', ...}
    success:  ... ECOCASH REG RESPONSE ***{"serviceType":"BANKREG","responseCode":"00",...,"name":"...",...}
    failure:  ... 400 BAD REQUEST ...   then   ... 400 RESPONSE BODY ****** {"status":400,"responseCode":"20503","message":...}
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

JOURNAL_PREFIX = re.compile(r"^(?P<syslog>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+(?P<proc>[^:]+?):\s?(?P<msg>.*)$")
APP_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(?P<level>TRACE|DEBUG|INFO|WARN|ERROR)\s+\d+\s+---\s+"
    r"\[(?P<thread>[^\]]*)\]\s+(?P<logger>\S+)\s*:\s?(?P<text>.*)$")
OPERATIONS = {"ECOCASH_REGISTRATION": "LINK", "ECOCASH_DEREGISTRATION": "UNLINK"}
SERVICE_TYPES = {"BANKREG": "LINK", "BANKDEREG": "UNLINK"}
QUOTED = re.compile(r"(\w+)='([^']*)'")
STATUS_LINE = re.compile(r"\*+\s*(\d{3})\s+[A-Z][A-Z _]+\s*\*+")
BODY_LINE = re.compile(r"(\d{3})\s+RESPONSE BODY\s*\**\s*(\{.*)")
REF_LINE = re.compile(r"ECOCASH REG REQUEST\s*\*+\s*([A-Za-z0-9]+)\s*$")
LOOSE = re.compile(r"^(Message|Response Code|Error Status):\s*(.*)$")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
DECODER = json.JSONDecoder()


@dataclass
class Attempt:
    """One link (BANKREG) or unlink (BANKDEREG) request to EcoCash and what came back."""
    thread: str
    operation: str  # LINK | UNLINK
    requested_at: datetime  # local time, as the service logs it
    host: Optional[str] = None
    channel: Optional[str] = None  # NMBCONNECT | IAPPROVE | ...
    via: Optional[str] = None  # USSD | VBS (branch staff) | IAPPROVE (account opening) | ...
    staff_user: Optional[str] = None  # e-mail of the staff member for branch-initiated requests
    mobile_number: Optional[str] = None
    account_number: Optional[str] = None
    id_number: Optional[str] = None
    currency: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    external_reference: Optional[str] = None
    request_uuid: Optional[str] = None
    outcome: Optional[str] = None  # linked | unlinked | failed | no_response
    http_status: Optional[int] = None
    response_code: Optional[str] = None
    message: Optional[str] = None
    ecocash_transaction_id: Optional[str] = None
    ecocash_name: Optional[str] = None  # wallet holder's name, returned by EcoCash on success
    responded_at: Optional[datetime] = None
    error_hint: Optional[str] = None  # first ERROR/WARN line on the thread while waiting
    awaiting_body: bool = False

    @property
    def key(self) -> str:
        """Unique per attempt: EcoCash's external reference, else where and when it was logged."""
        if self.external_reference:
            return self.external_reference
        return f"{self.host or '-'}|{self.thread}|{self.requested_at.isoformat(timespec='milliseconds')}"

    @property
    def done(self) -> bool:
        return self.outcome is not None

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("awaiting_body")
        row["attempt_key"] = self.key
        return row


def _ts(text: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(text.replace(" ", "T")[:26])
    except ValueError:
        return None


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def request_fields(text: str) -> dict[str, str]:
    """requestBody={firstName=..., mobileNumber=..., ...} -> dict (values may contain spaces)."""
    start = text.find("requestBody={")
    if start < 0:
        return {}
    body = text[start + len("requestBody={"):]
    end = body.rfind("}")
    body = body[:end] if end >= 0 else body
    body = body.rstrip("}")
    out: dict[str, str] = {}
    for part in re.split(r",\s*(?=\w+=)", body):
        if "=" in part:
            key, value = part.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _json_after(text: str, start: int) -> Optional[dict[str, Any]]:
    brace = text.find("{", start)
    if brace < 0:
        return None
    try:
        value, _ = DECODER.raw_decode(text[brace:])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _via(channel: Optional[str], model: Optional[str], imei: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    model = (model or "").strip()
    staff = None
    for value in (re.sub(r"(?i)^VBS-", "", model), imei or ""):
        m = EMAIL.search(value)
        if m:
            staff = m.group(0).lower()
            break
    if model.upper().startswith("VBS"):
        return "VBS", staff
    if model.upper() == "USSD":
        return "USSD", staff
    if (channel or "").upper() == "IAPPROVE":
        return "IAPPROVE", staff
    return (model.upper() or None), staff


class JournalParser:
    """Feed it journal lines (text export) or messages (journalctl -o json); it returns finished attempts."""

    def __init__(self, no_response_seconds: int = 180) -> None:
        self.timeout = timedelta(seconds=no_response_seconds)
        self.pending: dict[str, Attempt] = {}  # thread -> attempt in progress
        self._buffer: Optional[tuple[str, str]] = None  # (host, message) of the text entry being assembled
        self.last_seen: Optional[datetime] = None
        self.stats = {"entries": 0, "requests": 0, "linked": 0, "unlinked": 0, "failed": 0, "no_response": 0}

    # ---- input --------------------------------------------------------------------------
    def feed_text_line(self, raw: str) -> list[Attempt]:
        """A line of `journalctl` short output. Terminal-wrapped continuation lines are joined back."""
        raw = raw.rstrip("\r\n")
        if not raw or raw.startswith("-- "):
            return []
        m = JOURNAL_PREFIX.match(raw)
        if m and APP_LINE.match(m.group("msg")) or (m and not raw.startswith(" ") and "[" in m.group("proc")):
            done = self._flush_buffer()
            self._buffer = (m.group("host"), m.group("msg"))
            return done
        if self._buffer is None:
            return []
        host, msg = self._buffer
        self._buffer = (host, msg + raw)  # wrapped mid-token: join without a separator
        return []

    def feed_message(self, message: str, host: Optional[str] = None) -> list[Attempt]:
        """One journal entry's MESSAGE (journalctl -o json: never wrapped)."""
        return self._entry(host, message)

    def flush(self) -> list[Attempt]:
        """End of a text export: process the last entry and time out what is clearly finished."""
        done = self._flush_buffer()
        if self.last_seen:
            done += self.expire(self.last_seen)
        return done

    def expire(self, now: datetime) -> list[Attempt]:
        """Attempts with no response after the timeout become no_response records."""
        done = []
        for thread, attempt in list(self.pending.items()):
            if now - attempt.requested_at >= self.timeout:
                done.append(self._finish(thread, "no_response", message=attempt.error_hint or
                                         ("EcoCash rejected the request but the error text was not logged"
                                          if attempt.awaiting_body else "no response was logged within the timeout"),
                                         at=None))
        return done

    def oldest_pending(self) -> Optional[datetime]:
        return min((a.requested_at for a in self.pending.values()), default=None)

    # ---- entries ------------------------------------------------------------------------
    def _flush_buffer(self) -> list[Attempt]:
        if self._buffer is None:
            return []
        host, msg = self._buffer
        self._buffer = None
        return self._entry(host, msg)

    def _entry(self, host: Optional[str], message: str) -> list[Attempt]:
        self.stats["entries"] += 1
        m = APP_LINE.match(message.strip())
        if not m:
            return self._loose(message.strip())
        at = _ts(m.group("ts"))
        if at is None:
            return []
        self.last_seen = at if self.last_seen is None or at > self.last_seen else self.last_seen
        thread, text, level = m.group("thread").strip(), m.group("text"), m.group("level")
        done: list[Attempt] = []
        current = self.pending.get(thread)

        if "NMBZApiRequest{" in text and "requestBody={" in text:
            op = re.search(r"operation='(\w+)'", text)
            operation = OPERATIONS.get(op.group(1)) if op else None
            if operation is None:
                return done
            fields = request_fields(text)
            if (current is not None and current.external_reference is None and current.operation == operation
                    and current.mobile_number == _clean(fields.get("mobileNumber"))
                    and at - current.requested_at <= timedelta(seconds=10)):
                return done  # the service logs each request two or three times, milliseconds apart
            if current is not None:
                done.append(self._finish(thread, "no_response", at=None, message=current.error_hint or
                                         "no response was logged before the next request on this thread"))
            channel = re.search(r"channel='(\w*)'", text)
            uuid = re.search(r"uuid='([^']*)'", text)
            via, staff = _via(channel.group(1) if channel else None, fields.get("model"), fields.get("imei"))
            self.pending[thread] = Attempt(
                thread=thread, operation=operation, requested_at=at, host=host,
                channel=channel.group(1) if channel else None, via=via, staff_user=staff,
                mobile_number=_clean(fields.get("mobileNumber")), account_number=_clean(fields.get("accountNumber")),
                id_number=_clean(fields.get("idNumber")), currency=_clean(fields.get("currency")),
                first_name=_clean(fields.get("firstName")), last_name=_clean(fields.get("lastName")),
                request_uuid=uuid.group(1) if uuid else None)
            self.stats["requests"] += 1
            return done

        if "EcocashRegRequest{" in text:
            values = dict(QUOTED.findall(text))
            operation = SERVICE_TYPES.get(values.get("serviceType", ""), current.operation if current else "LINK")
            if current is None:  # the request line was before our starting point
                current = self.pending[thread] = Attempt(thread=thread, operation=operation, requested_at=at, host=host)
                self.stats["requests"] += 1
            current.external_reference = _clean(values.get("externalReference")) or current.external_reference
            # bankAccNumber is not the bank account: the service sends the client's mobile number in it
            # (for links and delinks alike), so the account from the incoming request is kept.
            current.account_number = current.account_number or _clean(values.get("bankAccNumber"))
            current.mobile_number = _clean(values.get("msisdn")) or current.mobile_number
            current.id_number = _clean(values.get("nationalId")) or current.id_number
            current.currency = _clean(values.get("currency")) or current.currency
            current.first_name = _clean(values.get("firstName")) or current.first_name
            current.last_name = _clean(values.get("lastName")) or current.last_name
            return done

        if current is None:
            return done
        ref = REF_LINE.search(text)
        if ref and not current.external_reference:
            current.external_reference = ref.group(1)
            return done

        if "ECOCASH REG RESPONSE" in text or re.search(r"\b0000 RESPONSE\b", text):
            body = _json_after(text, 0)
            if body is None:
                return done  # "200 OK" / "200" status lines: the body follows
            code = str(body.get("responseCode") or "")
            if code == "00":
                done.append(self._finish(thread, "unlinked" if current.operation == "UNLINK" else "linked", at=at,
                                         code=code, message=body.get("responseMessage") or body.get("statusMessage"),
                                         http=_int(body.get("status")) or 200,
                                         txn=body.get("transactionId"), name=body.get("name"),
                                         ref=body.get("externalReference") or body.get("requestId")))
            else:
                done.append(self._finish(thread, "failed", at=at, code=code or None,
                                         message=body.get("responseMessage") or body.get("message")
                                         or body.get("statusMessage"), http=_int(body.get("status"))))
            return done

        b = BODY_LINE.search(text)
        if b:
            body = _json_after(text, b.start(2))
            status = _int(b.group(1))
            if body is None:
                current.http_status, current.awaiting_body = status, True
                return done
            if 200 <= (_int(body.get("status")) or status or 0) < 300 and str(body.get("responseCode") or "00") == "00":
                return done
            done.append(self._finish(thread, "failed", at=at, code=_str(body.get("responseCode")),
                                     message=_str(body.get("message")) or _str(body.get("error")),
                                     http=_int(body.get("status")) or status))
            return done

        s = STATUS_LINE.search(text)
        if s and _int(s.group(1)) and _int(s.group(1)) >= 400:
            current.http_status, current.awaiting_body = _int(s.group(1)), True
            return done

        if level in ("ERROR", "WARN") and current.error_hint is None:
            current.error_hint = text.strip()[:300]
        return done

    def _loose(self, text: str) -> list[Attempt]:
        """Un-threaded System.out lines. Used only when exactly one attempt awaits its error text."""
        m = LOOSE.match(text)
        if not m:
            return []
        waiting = [t for t, a in self.pending.items() if a.awaiting_body]
        if len(waiting) != 1:
            return []
        attempt = self.pending[waiting[0]]
        label, value = m.group(1), m.group(2).strip()
        if label == "Response Code":
            attempt.response_code = value or attempt.response_code
            return []
        if label == "Message" and value:
            return [self._finish(waiting[0], "failed", at=self.last_seen, code=attempt.response_code,
                                 message=value, http=attempt.http_status)]
        return []

    def _finish(self, thread: str, outcome: str, *, at: Optional[datetime], code: Optional[str] = None,
                message: Optional[str] = None, http: Optional[int] = None, txn: Optional[str] = None,
                name: Optional[str] = None, ref: Optional[str] = None) -> Attempt:
        attempt = self.pending.pop(thread)
        attempt.outcome = outcome
        attempt.responded_at = at
        attempt.response_code = code or attempt.response_code
        attempt.message = (message or "").strip()[:1000] or None
        attempt.http_status = http or attempt.http_status
        attempt.ecocash_transaction_id = _str(txn)
        attempt.ecocash_name = _str(name)
        if ref and not attempt.external_reference:
            attempt.external_reference = str(ref)
        self.stats[outcome] = self.stats.get(outcome, 0) + 1
        return attempt


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_text(lines: Iterable[str], no_response_seconds: int = 180) -> tuple[list[Attempt], JournalParser]:
    """Parse a saved `journalctl` text export (backfill and tests)."""
    parser = JournalParser(no_response_seconds)
    done: list[Attempt] = []
    for line in lines:
        done += parser.feed_text_line(line)
    done += parser.flush()
    return done, parser
