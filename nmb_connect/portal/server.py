"""The NMB Connect desk's part of the console: the shared server (agentcore.portal.server) with this desk's name and
read models. The service console (python -m nmbz portal) mounts it at /api/d/nmb_connect/."""
from __future__ import annotations

from agentcore.portal import server as _core
from agentcore.portal.server import STATIC, HttpError, SERVED_MARKER, make_handler  # noqa: F401 - used by tests and tools

from . import data


class Portal(_core.Portal):
    realm = "NMB Connect agent"
    server_version = "nmb-connect-portal"
    csv_name = "nmb-connect-decisions.csv"
    data = data
