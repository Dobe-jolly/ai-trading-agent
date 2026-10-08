"""Console read models: the shared ones (agentcore.portal.data), with where clients stood grouped for the overview."""
from __future__ import annotations

from typing import Any, Optional

from agentcore.portal import data as _core
from agentcore.portal.data import *  # noqa: F401,F403 - the read models both agents share
from agentcore.portal.data import (  # noqa: F401 - private helpers some callers use
    APPROVAL_COLUMNS, LOW_CONFIDENCE, NOTIFICATION_COLUMNS, REVIEW_SQL, SUMMARY_COLUMNS, _json,
)

# Where the latest link attempt left the client, grouped by who acts next.
OUTCOME_GROUPS = {
    "linked": "linked",
    "already_linked": "branch", "unlinked": "branch",
    "number_not_ecocash": "client", "wallet_closed": "client", "kyc_mismatch": "client",
    "ecocash_system_error": "ecocash", "no_response": "ecocash",
    "account_rejected": "ict", "unknown": "ict",
    "none": "none",
}
GROUP_KEYS = ('linked', 'branch', 'client', 'ecocash', 'ict', 'none')


def overview(conn: Any, days: int, stale_minutes: int, price_in: Optional[float] = None,
             price_out: Optional[float] = None) -> dict[str, Any]:
    return _core.overview(conn, days, stale_minutes, price_in, price_out, groups=OUTCOME_GROUPS,
                          group_keys=GROUP_KEYS, by_day_key='outcomes_by_day')
