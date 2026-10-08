"""The EcoCash console: the shared server (agentcore.portal.server) with this agent's name, read models and page."""
from __future__ import annotations

from pathlib import Path

from agentcore.portal import server as _core
from agentcore.portal.server import HttpError, SERVED_MARKER, make_handler  # noqa: F401 - used by tests and tools

from . import data

STATIC = Path(__file__).resolve().parent / "static" / "index.html"


class Portal(_core.Portal):
    realm = 'EcoCash agent'
    server_version = 'ecocash-agent-portal'
    csv_name = 'ecocash-agent-decisions.csv'
    data = data
    static = STATIC


def serve(settings) -> None:
    _core.serve(settings, Portal)
