"""Console read models: the shared ones (agentcore.portal.data), with SMS gateway outcomes grouped for the overview."""
from __future__ import annotations

from typing import Any, Optional

from agentcore.portal import data as _core
from agentcore.portal.data import *  # noqa: F401,F403 - the read models both agents share
from agentcore.portal.data import (  # noqa: F401 - private helpers some callers use
    APPROVAL_COLUMNS, LOW_CONFIDENCE, NOTIFICATION_COLUMNS, REVIEW_SQL, SUMMARY_COLUMNS, _json,
)

GATEWAY_GROUPS = {
    "delivered_check_spam": "delivered",
    "socket_pending": "queued",
    "socket_failed": "failed",
    "socket_not_found": "never_sent",
    "sms_no_customer_email": "never_sent",
    "socket_unknown": "unknown",
}
GROUP_KEYS = ('delivered', 'queued', 'failed', 'never_sent', 'unknown')


def overview(conn: Any, days: int, stale_minutes: int, price_in: Optional[float] = None,
             price_out: Optional[float] = None) -> dict[str, Any]:
    return _core.overview(conn, days, stale_minutes, price_in, price_out, groups=GATEWAY_GROUPS,
                          group_keys=GROUP_KEYS, by_day_key='gateway_by_day')
