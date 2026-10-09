"""Console read models: the shared ones (agentcore.portal.data), with where each decision sends the ticket grouped
for the overview (the decision's category: a topic an agent works, another team's topic, stays, unclear)."""
from __future__ import annotations

from typing import Any, Optional

from agentcore.portal import data as _core
from agentcore.portal.data import *  # noqa: F401,F403 - the read models every desk shares
from agentcore.portal.data import (  # noqa: F401 - private helpers some callers use
    APPROVAL_COLUMNS, LOW_CONFIDENCE, NOTIFICATION_COLUMNS, REVIEW_SQL, SUMMARY_COLUMNS, _json,
)

ROUTE_GROUPS = {"desk_topic": "desk_topic", "staff_topic": "staff_topic", "stays": "stays", "unclear": "unclear",
                "no_description": "no_description"}
GROUP_KEYS = ("desk_topic", "staff_topic", "stays", "unclear", "no_description")


def overview(conn: Any, days: int, stale_minutes: int, price_in: Optional[float] = None,
             price_out: Optional[float] = None) -> dict[str, Any]:
    return _core.overview(conn, days, stale_minutes, price_in, price_out, groups=ROUTE_GROUPS,
                          group_keys=GROUP_KEYS, by_day_key="routes_by_day", group_by="category")
