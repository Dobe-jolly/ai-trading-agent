"""The failed-transactions desk's part of the console: the shared server (agentcore.portal.server) with this desk's
name and read models. The service console (python -m nmbz portal) mounts it at /api/d/failed_txn/."""
from __future__ import annotations

from agentcore.portal import server as _core
from agentcore.portal.server import STATIC, HttpError, SERVED_MARKER, make_handler  # noqa: F401 - used by tests and tools

from . import data


class Portal(_core.Portal):
    realm = "Failed transactions agent"
    server_version = "failed-txn-portal"
    csv_name = "failed-txn-decisions.csv"
    data = data
