"""Branch notifications: rendering, both transports, the outbox, and the live flow."""
from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from agentcore.audit import AuditLog
from sms_alerts.notify import GatewayEmail, Notifier, NotifyError, SmtpEmail, render
from sms_alerts.portal.server import HttpError, Portal
from agentcore.portal.users import UserStore
from tests.sms_alerts.conftest import ScriptedModel, check, decision
from tests.sms_alerts.test_agent_runner import CLOSE_REPLY, build, seed

ROOT_TEMPLATES = __import__("pathlib").Path(__file__).resolve().parents[2] / "sms_alerts" / "templates"


class RecordingTransport:
    channel = "test"

    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[dict] = []
        self.fail_times = fail_times

    def send(self, **kw) -> None:
        if self.fail_times:
            self.fail_times -= 1
            raise NotifyError("gateway returned HTTP 503")
        self.sent.append(kw)

    def close(self) -> None:
        pass


@pytest.fixture
def nsettings(settings):
    return replace(settings, notify_transport="gateway", notif_email_url="http://gw.test/email",
                   osticket_client_url="https://helpdesk.example/tickets.php?id={ticket_id}", notify_cc=("desk@bank.example",),
                   template_dir=ROOT_TEMPLATES, request_info_timeout_hours=72.0)


def test_render_request_info_and_resolved():
    r = render("request_info", ROOT_TEMPLATES, number="482913", ticket_id=7, owner_name="Tendai", reply="Kindly share X.",
               client_url="https://h/tickets.php?id={ticket_id}", reply_to=None, signature="ICT Service Desk", follow_up_hours=72)
    assert r.subject == "[#482913] SMS alerts ticket: information needed"
    assert "Dear Tendai" in r.body and "Kindly share X." in r.body and "https://h/tickets.php?id=7" in r.body
    assert "3 days" in r.body and "replies to this email are not read" in r.body
    r2 = render("reply_and_close", ROOT_TEMPLATES, number="1", ticket_id=1, owner_name=None, reply="Done.", client_url=None,
                reply_to="helpdesk@bank.example", signature="Desk", follow_up_hours=72)
    assert r2.subject == "[#1] SMS alerts ticket resolved" and "Dear colleague" in r2.body and "{" not in r2.body


def test_gateway_payload_matches_the_java_contract():
    seen = []

    def handler(request):
        seen.append((dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    gw = GatewayEmail("http://gw.test/email", "email-token-456", "dXNlcjpwYXNz", transport=httpx.MockTransport(handler))
    gw.send(to="a@b.example", cc=["c@d.example"], subject="S", body="B", message_id="0d9c3c3e-2c2f-4b6e-9d6a-111111111111")
    (h1, b1), (h2, b2) = seen
    assert h1["authorization"] == "Basic dXNlcjpwYXNz"
    assert b1 == {"to": "a@b.example", "subject": "S", "body": "B", "uuid": "0d9c3c3e-2c2f-4b6e-9d6a-111111111111",
                  "access_token": "email-token-456"}
    assert b2["to"] == "c@d.example" and b2["uuid"] != b1["uuid"]
    bad = GatewayEmail("http://gw.test/email", "t", None, transport=httpx.MockTransport(lambda r: httpx.Response(503, text="down")))
    with pytest.raises(NotifyError, match="503"):
        bad.send(to="a@b.example", cc=[], subject="S", body="B", message_id="0d9c3c3e-2c2f-4b6e-9d6a-111111111111")


def test_smtp_message(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            captured["host"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            captured["tls"] = True

        def login(self, user, password):
            captured["login"] = user

        def send_message(self, msg):
            captured["msg"] = msg

    monkeypatch.setattr("sms_alerts.notify.smtplib.SMTP", FakeSMTP)
    SmtpEmail("relay.bank", 587, "sms-agent@bank.example", "SMS Alerts agent", "helpdesk@bank.example", True, "svc", "pw").send(
        to="a@b.example", cc=["c@d.example"], subject="[#1] x", body="hello", message_id="0d9c3c3e-2c2f-4b6e-9d6a-111111111111")
    msg = captured["msg"]
    assert captured["host"] == ("relay.bank", 587) and captured["tls"] and captured["login"] == "svc"
    assert msg["Reply-To"] == "helpdesk@bank.example" and msg["Cc"] == "c@d.example" and msg["Subject"] == "[#1] x"
    assert msg["From"] == "SMS Alerts agent <sms-agent@bank.example>" and msg["Auto-Submitted"] == "auto-generated"


def test_outbox_send_retry_fail_and_erase(nsettings, tmp_path):
    audit = AuditLog(tmp_path / "a.sqlite")
    transport = RecordingTransport(fail_times=2)
    n = Notifier(audit, replace(nsettings, notify_max_attempts=2), transport)
    nid = n.enqueue(decision_id=1, ticket_id=7, number="482913", kind="request_info", recipient="t.moyo@branch.example",
                    owner_name="Tendai", reply="Account 00012345678901 ...")
    assert n.enqueue(decision_id=1, ticket_id=7, number="482913", kind="request_info", recipient="x@y.example",
                     owner_name=None, reply="dup") is None
    assert n.dispatch(2) == {"sent": 0, "failed": 0, "retry": 1}
    assert n.dispatch(2) == {"sent": 0, "failed": 1, "retry": 0}
    row = audit.db.execute("SELECT * FROM notifications WHERE id = ?", (nid,)).fetchone()
    assert row["status"] == "failed" and row["body"] and "503" in row["last_error"]
    assert n.retry_failed() == 1 and n.dispatch(2)["sent"] == 1
    row = audit.db.execute("SELECT * FROM notifications WHERE id = ?", (nid,)).fetchone()
    assert row["status"] == "sent" and row["body"] is None and row["sent_at"]
    assert transport.sent[0]["cc"] == ["desk@bank.example"] and "00012345678901" in transport.sent[0]["body"]


def test_owner_without_email_is_skipped(nsettings, tmp_path):
    audit = AuditLog(tmp_path / "a.sqlite")
    n = Notifier(audit, nsettings, RecordingTransport())
    n.enqueue(decision_id=2, ticket_id=7, number="1", kind="reply_and_close", recipient="not-an-email", owner_name=None, reply="r")
    row = audit.db.execute("SELECT status, body, last_error FROM notifications").fetchone()
    assert row["status"] == "skipped" and row["body"] is None and "no valid email" in row["last_error"]
    assert n.dispatch() == {"sent": 0, "failed": 0, "retry": 0}


def run_with_notifier(nsettings, mb, model, mode="live", transport=None):
    runner, audit = build(nsettings, mb, model, mode=mode)
    transport = transport or RecordingTransport()
    runner.notifier = Notifier(audit, nsettings, transport)
    return runner, audit, transport


def test_live_request_info_emails_the_branch_with_real_values(nsettings, mb):
    seed(mb, phone="07721234")
    ask = "The phone number on the ticket (<NUMBER_1>) is not valid. Kindly reply with the client's correct mobile number."
    runner, audit, transport = run_with_notifier(nsettings, mb, ScriptedModel(check(account="<NUMBER_2>"), decision("request_info", ask)))
    summary = runner.run()
    assert summary.emails["sent"] == 1
    mail = transport.sent[0]
    assert mail["to"] == "t.moyo@branch.example" and mail["subject"] == "[#482913] SMS alerts ticket: information needed"
    assert "(07721234)" in mail["body"] and "<NUMBER_1>" not in mail["body"] and "Dear Tendai Moyo" in mail["body"]
    assert "https://helpdesk.example/tickets.php?id=7" in mail["body"]
    row = audit.last_for_ticket(7)
    assert "email queued" in json.loads(row["executed"])


def test_live_close_emails_resolution(nsettings, mb):
    seed(mb)
    runner, audit, transport = run_with_notifier(nsettings, mb, ScriptedModel(check(), decision("reply_and_close", CLOSE_REPLY)))
    runner.run()
    assert transport.sent[0]["subject"] == "[#482913] SMS alerts ticket resolved"
    assert "00012345678901" in transport.sent[0]["body"]


def test_no_email_in_shadow_or_for_escalations(nsettings, mb):
    seed(mb)
    runner, audit, transport = run_with_notifier(nsettings, mb, ScriptedModel(check(), decision("reply_and_close", CLOSE_REPLY)), mode="shadow")
    runner.run()
    mb.add_entry(7, "M", "again")
    runner2, audit2, transport2 = run_with_notifier(nsettings, mb, ScriptedModel(check(), decision("escalate")), mode="live")
    runner2.run()
    assert transport.sent == [] and transport2.sent == []
    assert audit2.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


def test_gateway_down_keeps_email_for_the_next_pass(nsettings, mb):
    seed(mb)
    runner, audit, transport = run_with_notifier(nsettings, mb, ScriptedModel(check(), decision("reply_and_close", CLOSE_REPLY)),
                                                 transport=RecordingTransport(fail_times=1))
    assert runner.run().emails == {"sent": 0, "failed": 0, "retry": 1}
    runner2, audit2, _ = run_with_notifier(nsettings, mb, ScriptedModel(), transport=transport)
    assert runner2.run().emails["sent"] == 1  # the ticket is closed now; the queued email still goes out


def test_portal_lists_emails_without_bodies_and_admin_retry(nsettings, mb, tmp_path):
    seed(mb)
    runner, audit, _ = run_with_notifier(nsettings, mb, ScriptedModel(check(), decision("reply_and_close", CLOSE_REPLY)),
                                         transport=RecordingTransport(fail_times=99))
    runner.notifier.s = replace(nsettings, notify_max_attempts=1)
    runner.s = replace(runner.s, notify_max_attempts=1)
    runner.run()
    users = UserStore(tmp_path / "u.json")
    users.set("dobe", "correct horse battery", "admin")
    users.set("viewer1", "another long password", "viewer")
    portal = Portal(replace(nsettings, portal_users_file=tmp_path / "u.json"), users)
    listing = portal.get("/api/notifications", {}, "dobe", "admin")
    assert listing["counts"] == {"failed": 1} and listing["retryable"] == 1
    assert "body" not in listing["items"][0]
    detail = portal.get(f"/api/decisions/{listing['items'][0]['decision_id']}", {}, "dobe", "admin")
    assert detail["notifications"][0]["status"] == "failed" and "body" not in detail["notifications"][0]
    with pytest.raises(HttpError):
        portal.post("/api/notifications/retry", {}, "viewer1", "viewer")
    assert portal.post("/api/notifications/retry", {}, "dobe", "admin") == {"requeued": 1}
    assert portal.get("/api/overview", {}, "dobe", "admin")["kpis"]["emails_pending"] == 1
