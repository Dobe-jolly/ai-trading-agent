"""Console read models: the shared ones (agentcore.portal.data), with where the clients stood grouped for the
overview."""
from __future__ import annotations

from typing import Any, Optional

from agentcore.portal import data as _core
from agentcore.portal.data import *  # noqa: F401,F403 - the read models every desk shares
from agentcore.portal.data import (  # noqa: F401 - private helpers some callers use
    APPROVAL_COLUMNS, LOW_CONFIDENCE, NOTIFICATION_COLUMNS, REVIEW_SQL, SUMMARY_COLUMNS, _json,
)

# Where the assessment left the client, grouped by who acts next.
OUTCOME_GROUPS = {
    "registered": "advice", "how_to": "advice", "registration_failed_email": "advice",
    "registration_failed_temporary": "advice",
    "not_registered": "register",
    "mobile_mismatch": "branch_fix", "already_registered": "branch_fix", "account_active": "branch_fix",
    "reregistered": "fixed", "mobile_updated": "fixed",
    "account_inactive": "account",
    "duplicate_profiles": "needs_person", "number_on_other_profile": "needs_person", "locked": "needs_person",
    "otp_not_delivered": "needs_person", "registration_failed_duplicate": "needs_person",
    "registration_failed_unknown": "needs_person", "registered_cannot_login": "needs_person",
    "account_other_cif": "needs_person", "other": "needs_person",
    "needs_details": "details", "registered_unverified": "details", "no_attempt_found": "details",
    "account_not_found": "details",
}
GROUP_KEYS = ("advice", "register", "branch_fix", "fixed", "account", "needs_person", "details")


def overview(conn: Any, days: int, stale_minutes: int, price_in: Optional[float] = None,
             price_out: Optional[float] = None) -> dict[str, Any]:
    return _core.overview(conn, days, stale_minutes, price_in, price_out, groups=OUTCOME_GROUPS,
                          group_keys=GROUP_KEYS, by_day_key="outcomes_by_day")
