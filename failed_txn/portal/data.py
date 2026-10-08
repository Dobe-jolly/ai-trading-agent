"""Console read models: the shared ones (agentcore.portal.data), with where the confirmed transactions left the
clients grouped for the overview."""
from __future__ import annotations

from typing import Any, Optional

from agentcore.portal import data as _core
from agentcore.portal.data import *  # noqa: F401,F403 - the read models every desk shares
from agentcore.portal.data import (  # noqa: F401 - private helpers some callers use
    APPROVAL_COLUMNS, LOW_CONFIDENCE, NOTIFICATION_COLUMNS, REVIEW_SQL, SUMMARY_COLUMNS, _json,
)

# Where the confirmed transaction left the client, grouped by what it means for them.
OUTCOME_GROUPS = {
    "succeeded": "went_through",
    "declined": "no_money_moved", "failed_no_debit": "no_money_moved",
    "reversed": "money_back", "failed_refunded": "money_back",
    "reversal_pending": "reversal_pending",
    "reversal_failed": "needs_ops", "uncertain": "needs_ops", "failed": "needs_ops", "posted": "needs_ops",
    "not_found": "not_found",
}
GROUP_KEYS = ("went_through", "no_money_moved", "money_back", "reversal_pending", "needs_ops", "not_found")


def overview(conn: Any, days: int, stale_minutes: int, price_in: Optional[float] = None,
             price_out: Optional[float] = None) -> dict[str, Any]:
    return _core.overview(conn, days, stale_minutes, price_in, price_out, groups=OUTCOME_GROUPS,
                          group_keys=GROUP_KEYS, by_day_key="outcomes_by_day")
