"""Branch e-mails: the shared outbox (agentcore.notify) with the router's subjects. The router only e-mails when it
asks the branch what the problem is."""
from __future__ import annotations

from typing import Any

from agentcore import notify as _core
from agentcore.notify import *  # noqa: F401,F403 - transports, Rendered, NotifyError, ...
from agentcore.notify import smtplib  # noqa: F401 - tests patch router.notify.smtplib

SUBJECTS = {
    "request_info": "[#{number}] Ticket: please describe the problem",
    "reply_and_close": "[#{number}] Ticket updated",
}


def render(kind: str, template_dir: Any, **kwargs: Any) -> _core.Rendered:
    return _core.render(kind, template_dir, subjects=SUBJECTS, **kwargs)


class Notifier(_core.Notifier):
    subjects = SUBJECTS
