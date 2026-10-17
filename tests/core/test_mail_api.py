"""The bank's mail API as the branch e-mail transport (NOTIFY_TRANSPORT=mailapi). Invented addresses only."""
from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from agentcore.notify import MailApiEmail, NotifyError, build_transport
from failed_txn.config import Settings

TOKEN = "00000000-test-token-0000-000000000000"


def transport(status=200, answer=None, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        if isinstance(answer, str):
            return httpx.Response(status, text=answer)
        return httpx.Response(status, json=answer if answer is not None else {"status": "SUCCESS"})
    return httpx.MockTransport(handler)


def test_one_request_per_email_with_every_recipient_in_to():
    seen = []
    mail = MailApiEmail("http://mail.test/v1/secured/mail", TOKEN, transport=transport(seen=seen))
    mail.send(to="branch@example.invalid", cc=["ict@example.invalid"], subject="[#700512] Ticket resolved",
              body="Good day\nThe transfer went through.\n", message_id="5f1c0e4e-0000-4000-8000-000000000001")
    assert seen == [{"operation": "SEND_EMAIL", "channel": "NMB_MOBILE", "accessToken": TOKEN,
                     "uuid": "5f1c0e4e-0000-4000-8000-000000000001",
                     "requestBody": {"to": "branch@example.invalid, ict@example.invalid",
                                     "subject": "[#700512] Ticket resolved",
                                     "message": "Good day\nThe transfer went through.\n"}}]


@pytest.mark.parametrize("status, answer", [
    (500, f"server error for token {TOKEN}"),
    (200, {"success": False, "message": "bad token"}),
    (200, {"status": "FAILED", "accessToken": TOKEN}),
])
def test_failures_are_errors_and_never_carry_the_token(status, answer):
    mail = MailApiEmail("http://mail.test/v1/secured/mail", TOKEN, transport=transport(status, answer))
    with pytest.raises(NotifyError) as err:
        mail.send(to="branch@example.invalid", cc=[], subject="s", body="b", message_id="m")
    assert TOKEN not in str(err.value)


def test_an_unreachable_api_is_an_error():
    def down(request):
        raise httpx.ConnectError("refused")
    mail = MailApiEmail("http://mail.test/v1/secured/mail", TOKEN, transport=httpx.MockTransport(down))
    with pytest.raises(NotifyError, match="unreachable"):
        mail.send(to="branch@example.invalid", cc=[], subject="s", body="b", message_id="m")


def test_settings_choose_it_and_require_url_and_token(tmp_path):
    base = Settings.from_env(env_file=tmp_path / "none.env")
    s = replace(base, notify_transport="mailapi")
    assert any("MAIL_API_URL and MAIL_API_TOKEN" in p for p in s.problems("shadow", need_model=False))
    s = replace(s, mail_api_url="http://mail.test/v1/secured/mail", mail_api_token=TOKEN)
    assert not any("MAIL_API" in p or "NOTIFY_TRANSPORT" in p for p in s.problems("shadow", need_model=False))
    assert isinstance(build_transport(s), MailApiEmail) and s.mail_api_channel == "NMB_MOBILE"
