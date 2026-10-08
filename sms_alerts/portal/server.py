"""The SMS Alerts desk's part of the console: the shared server (agentcore.portal.server) with this desk's name and
read models. The service console (python -m nmbz portal) mounts it at /api/d/sms_alerts/."""
from __future__ import annotations

from agentcore.portal import server as _core
from agentcore.portal.server import STATIC, HttpError, SERVED_MARKER, make_handler  # noqa: F401 - used by tests and tools

from . import data


class Portal(_core.Portal):
    realm = 'SMS Alerts agent'
    server_version = 'sms-alerts-portal'
    csv_name = 'sms-agent-decisions.csv'
    data = data
