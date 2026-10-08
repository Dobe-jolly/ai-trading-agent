"""Branch e-mails and admin alerts: the shared outbox (agentcore.notify) with this desk's e-mail subjects."""
from __future__ import annotations

from typing import Any

from agentcore import notify as _core
from agentcore.notify import *  # noqa: F401,F403 - transports, Rendered, NotifyError, ...
from agentcore.notify import smtplib  # noqa: F401 - tests patch failed_txn.notify.smtplib

SUBJECTS = {
    "request_info": "[#{number}] Failed transaction ticket: information needed",
    "reply_and_close": "[#{number}] Failed transaction ticket resolved",
}


def render(kind: str, template_dir: Any, **kwargs: Any) -> _core.Rendered:
    return _core.render(kind, template_dir, subjects=SUBJECTS, **kwargs)


class Notifier(_core.Notifier):
    subjects = SUBJECTS
