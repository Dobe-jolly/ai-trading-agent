"""Telling the ticket owner (the branch) that the agent replied.

Replies are written into osTicket's tables directly (as the Java did), which bypasses osTicket's
own email alerts. Without this, a branch never learns the agent asked it a question.

Flow: after a live reply is posted, a notification is queued in the outbox (SQLite). Every pass
sends what is pending, retrying failures on later passes. A sent notification's body is erased,
so account and phone numbers do not linger in the log.

Transports:
- gateway: the bank notification gateway the Java AdminNotifGateway uses
  (POST NOTIF_EMAIL_URL {to, subject, body, uuid, access_token}, Basic auth header).
- smtp: a plain SMTP relay. Supports Reply-To, so a branch can answer by replying to the email
  when osTicket fetches that mailbox (the [#number] subject threads it into the ticket).
"""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any, Optional

import httpx

from agentcore.audit import OUTBOX_SCHEMA, AuditLog, now_iso

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
KINDS = {"request_info": "notify_request_info.txt", "reply_and_close": "notify_resolved.txt"}
SUBJECTS = {
    "request_info": "[#{number}] SMS alerts ticket: information needed",
    "reply_and_close": "[#{number}] SMS alerts ticket resolved",
}
class NotifyError(RuntimeError):
    pass


def valid_email(address: Optional[str]) -> bool:
    return bool(address) and bool(EMAIL_RE.match(address.strip()))  # type: ignore[union-attr]


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


@dataclass
class Rendered:
    subject: str
    body: str


def _duration(hours: float) -> str:
    if hours and hours % 24 == 0:
        days = int(hours // 24)
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{hours:g} hours"


def render(kind: str, template_dir: Path, *, number: str, ticket_id: int, owner_name: Optional[str], reply: str,
           client_url: Optional[str], reply_to: Optional[str], signature: str, follow_up_hours: float = 72) -> Rendered:
    template = (template_dir / KINDS[kind]).read_text(encoding="utf-8")
    link = ""
    if client_url:
        url = client_url.replace("{number}", str(number)).replace("{ticket_id}", str(ticket_id))
        link = f"Open the ticket: {url}"
    reply_hint = (f"You can answer by replying to this email (keep [#{number}] in the subject), "
                  "or on the ticket in the helpdesk portal." if reply_to
                  else "Please answer on the ticket in the helpdesk portal; replies to this email are not read.")
    values = _SafeDict(number=number, owner_name=(owner_name or "colleague").strip(), reply=reply.strip(),
                       ticket_link=link, reply_hint=reply_hint, signature=signature,
                       follow_up=_duration(follow_up_hours))
    body = template.format_map(values)
    body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
    return Rendered(SUBJECTS[kind].format(number=number), body)


# ---- transports ----------------------------------------------------------------
class GatewayEmail:
    channel = "gateway"

    def __init__(self, url: str, access_token: Optional[str], basic_auth: Optional[str], timeout: float = 10.0,
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        headers = {"Content-Type": "application/json"}
        if basic_auth:
            headers["Authorization"] = f"Basic {basic_auth}"  # pre-encoded, as NOTIF_GATEWAY_BASIC_AUTH in the Java
        self._url = url
        self._token = access_token
        self._http = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    def send(self, *, to: str, cc: list[str], subject: str, body: str, message_id: str) -> None:
        for recipient in [to, *cc]:
            payload = {"to": recipient, "subject": subject, "body": body,
                       # Stable per notification: lets the gateway drop a duplicate if a retry repeats a send.
                       "uuid": message_id if recipient == to else str(uuid.uuid5(uuid.UUID(message_id), recipient)),
                       "access_token": self._token}
            try:
                response = self._http.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                raise NotifyError(f"gateway unreachable: {type(exc).__name__}") from exc
            if response.status_code >= 400:
                raise NotifyError(f"gateway returned HTTP {response.status_code}: {response.text[:200]}")

    def close(self) -> None:
        self._http.close()


class GatewaySms:
    """SMS through the bank gateway, exactly as the Java AdminNotifGateway.sendSms."""

    channel = "sms"

    def __init__(self, url: str, access_token: Optional[str], basic_auth: Optional[str], originator: str = "NMB Bank",
                 timeout: float = 10.0, transport: Optional[httpx.BaseTransport] = None) -> None:
        headers = {"Content-Type": "application/json"}
        if basic_auth:
            headers["Authorization"] = f"Basic {basic_auth}"
        self._url, self._token, self._originator = url, access_token, originator
        self._http = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    def send(self, *, to: str, cc: list[str], subject: str, body: str, message_id: str) -> None:
        payload = {"mobile": to, "message": body, "originator": self._originator, "access_token": self._token}
        try:
            response = self._http.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise NotifyError(f"SMS gateway unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise NotifyError(f"SMS gateway returned HTTP {response.status_code}: {response.text[:200]}")

    def close(self) -> None:
        self._http.close()


class SmtpEmail:
    channel = "smtp"

    def __init__(self, host: str, port: int, sender: str, sender_name: Optional[str], reply_to: Optional[str],
                 starttls: bool, username: Optional[str], password: Optional[str], timeout: float = 20.0) -> None:
        self.host, self.port, self.sender, self.sender_name = host, port, sender, sender_name
        self.reply_to, self.starttls, self.username, self.password, self.timeout = reply_to, starttls, username, password, timeout

    def send(self, *, to: str, cc: list[str], subject: str, body: str, message_id: str) -> None:
        msg = EmailMessage()
        msg["From"] = formataddr((self.sender_name, self.sender)) if self.sender_name else self.sender
        msg["To"] = to
        if cc:
            msg["Cc"] = ", ".join(cc)
        if self.reply_to:
            msg["Reply-To"] = self.reply_to
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(idstring=message_id.replace("-", "")[:16])
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                if self.starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifyError(f"SMTP send failed: {type(exc).__name__}: {exc}") from exc

    def close(self) -> None:
        pass


def build_sms_transport(settings: Any) -> Optional[GatewaySms]:
    if not settings.notif_sms_url:
        return None
    return GatewaySms(settings.notif_sms_url, settings.notif_sms_access_token, settings.notif_gateway_basic_auth,
                      settings.notif_sms_originator)


def build_transport(settings: Any) -> Optional[Any]:
    kind = settings.notify_transport
    if kind == "gateway":
        return GatewayEmail(settings.notif_email_url, settings.notif_email_access_token, settings.notif_gateway_basic_auth)
    if kind == "smtp":
        return SmtpEmail(settings.smtp_host, settings.smtp_port, settings.smtp_from, settings.smtp_from_name,
                         settings.notify_reply_to, settings.smtp_starttls, settings.smtp_username, settings.smtp_password)
    return None


# ---- outbox ----------------------------------------------------------------------
class Notifier:
    def __init__(self, audit: AuditLog, settings: Any, transport: Optional[Any], sms_transport: Optional[Any] = None) -> None:
        self.audit = audit
        self.s = settings
        self.transport = transport
        self.sms_transport = sms_transport
        audit.db.executescript(OUTBOX_SCHEMA)

    @property
    def enabled(self) -> bool:
        return self.transport is not None

    def wants(self, action: str) -> bool:
        return self.enabled and action in self.s.notify_on

    def enqueue(self, *, decision_id: int, ticket_id: int, number: str, kind: str, recipient: Optional[str],
                owner_name: Optional[str], reply: str, skip_reason: Optional[str] = None) -> Optional[int]:
        """Queue one email. Returns the row id, or None if this decision was already queued."""
        rendered = render(kind, self.s.template_dir, number=number, ticket_id=ticket_id, owner_name=owner_name,
                          reply=reply, client_url=self.s.osticket_client_url,
                          reply_to=self.s.notify_reply_to if self.s.notify_transport == "smtp" else None,
                          signature=self.s.notify_signature, follow_up_hours=self.s.request_info_timeout_hours)
        ok = valid_email(recipient) and not skip_reason
        cc = [a for a in self.s.notify_cc if valid_email(a)]
        if not ok and not skip_reason:
            skip_reason = "the ticket owner has no valid email address in osTicket"
        try:
            cursor = self.audit.db.execute(
                "INSERT INTO notifications (uuid, created_at, decision_id, ticket_id, ticket_number, kind, channel, "
                "recipient, cc, subject, body, status, last_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), now_iso(), decision_id, ticket_id, number, kind,
                 getattr(self.transport, "channel", "none"), recipient, ",".join(cc), rendered.subject,
                 rendered.body if ok else None, "pending" if ok else "skipped",
                 None if ok else skip_reason))
            self.audit.db.commit()
        except Exception as exc:  # sqlite3.IntegrityError: already queued for this decision
            if "UNIQUE" in str(exc):
                return None
            raise
        return int(cursor.lastrowid)

    def enqueue_admin(self, *, kind: str, subject: str, email_body: str, sms_text: str) -> int:
        """Alert the admins (ADMIN_SMS_RECIPIENTS / ADMIN_EMAIL_RECIPIENTS). Returns how many were queued."""
        queued = 0
        targets = []
        if self.sms_transport is not None:
            targets += [("sms", r, sms_text) for r in self.s.admin_sms_recipients]
        if self.transport is not None:
            channel = getattr(self.transport, "channel", "email")
            targets += [(channel, r, email_body) for r in self.s.admin_email_recipients if valid_email(r)]
        for channel, recipient, body in targets:
            self.audit.db.execute(
                "INSERT INTO notifications (uuid, created_at, kind, channel, recipient, cc, subject, body, status) "
                "VALUES (?, ?, ?, ?, ?, '', ?, ?, 'pending')",
                (str(uuid.uuid4()), now_iso(), kind, channel, recipient, subject, body))
            queued += 1
        self.audit.db.commit()
        if not targets:
            log.warning("Admin alert not sent (no ADMIN_*_RECIPIENTS or transport configured): %s", subject)
        return queued

    def _transport_for(self, channel: str) -> Optional[Any]:
        return self.sms_transport if channel == "sms" else self.transport

    def dispatch(self, max_attempts: int = 5) -> dict[str, int]:
        """Send everything pending. Failures stay pending until max_attempts, then become failed."""
        counts = {"sent": 0, "failed": 0, "retry": 0}
        if self.transport is None and self.sms_transport is None:
            return counts
        rows = self.audit.db.execute(
            "SELECT * FROM notifications WHERE status = 'pending' ORDER BY id LIMIT 200").fetchall()
        for row in rows:
            transport = self._transport_for(row["channel"])
            if transport is None:
                continue
            try:
                transport.send(to=row["recipient"], cc=[c for c in (row["cc"] or "").split(",") if c],
                               subject=row["subject"], body=row["body"] or "", message_id=row["uuid"])
            except NotifyError as exc:
                attempts = row["attempts"] + 1
                status = "failed" if attempts >= max_attempts else "pending"
                counts["failed" if status == "failed" else "retry"] += 1
                self.audit.db.execute("UPDATE notifications SET attempts = ?, status = ?, last_error = ? WHERE id = ?",
                                      (attempts, status, str(exc)[:500], row["id"]))
                log.warning("#%s: notification to %s failed (attempt %d): %s",
                            row["ticket_number"], row["recipient"], attempts, exc)
            else:
                counts["sent"] += 1
                # Erase the body once delivered: it holds real account and phone numbers.
                self.audit.db.execute(
                    "UPDATE notifications SET status = 'sent', attempts = attempts + 1, sent_at = ?, body = NULL, "
                    "last_error = NULL WHERE id = ?", (now_iso(), row["id"]))
            self.audit.db.commit()
        if any(counts.values()):
            log.info("Notifications: %s", counts)
        return counts

    def retry_failed(self) -> int:
        cursor = self.audit.db.execute(
            "UPDATE notifications SET status = 'pending', attempts = 0 WHERE status = 'failed' AND body IS NOT NULL")
        self.audit.db.commit()
        return cursor.rowcount

    def close(self) -> None:
        for transport in (self.transport, self.sms_transport):
            if transport is not None:
                transport.close()
