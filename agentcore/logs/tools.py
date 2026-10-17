"""log_errors and log_search: the agent's look into the ingested application logs.

- log_errors: what is going wrong in the services, as signatures with counts per window: the top ones, the spikes (a
  signature far above its usual level, with clusters across services at the same time), or the new ones. Service-wide,
  no client data: it answers "is something broken for everyone?" and "what does this error mean?" (a known cause is
  shown when staff have written one).
- log_search: the log lines about one client's attempt, found by an identifier the ticket gives (account, mobile or
  reference/RRN: matched through keyed hashes, so no number is stored or shown) or by a correlation id taken from an
  earlier result, with the other lines of the same requests (the trail through the services).

The agent never reads a log file or writes a query. What it sees is masked."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Optional

from agentcore.dataquery import parse_moment

from .mask import KeyedMasker
from .profiles import Profile, describe
from .store import DEFAULT_SEARCH_ROWS, MAX_SEARCH_ROWS, LogStore, stamp

CORR_RE = re.compile(r"^[A-Za-z0-9_<>:~\-]{4,80}$")
KINDS = ("account", "mobile", "reference")
Identify = Callable[[str, str], list]


class LogTools:
    def __init__(self, store: LogStore, masker: KeyedMasker, profiles: dict[str, Profile]) -> None:
        self.store, self.masker, self.profiles = store, masker, profiles

    def services(self) -> list[str]:
        return sorted({s["service"] for s in self.store.services()} | set(self.profiles))

    # ---- the tools -------------------------------------------------------------------------------------------------
    def tool_schemas(self) -> list[dict[str, Any]]:
        known = describe({k: v for k, v in self.profiles.items() if k in {s["service"] for s in self.store.services()}})
        first, last = self.store.span()
        held = f" Logs held: {first} to {last} (server local time)." if first else " No logs have been ingested yet."
        service = {"type": "string", "description": "A service name (see the description)."}
        return [
            {"name": "log_errors",
             "description": (
                 "What is going wrong in the bank's application services, from their logs: errors and warnings grouped "
                 "into signatures (the same problem whatever its numbers), counted. mode=top: the most frequent in the "
                 "window; mode=spikes: signatures far above their usual level, with clusters of services spiking "
                 "together; mode=new: signatures first seen in the window. No client data: use it to see whether a "
                 "service is failing for everyone, what an error is, and when it started. A known_cause is shown when "
                 "staff have recorded one. Errors that no database records (an exception, a lost connection pool, a "
                 f"downstream that refused) are here.{held} Services: {known}"),
             "input_schema": {"type": "object", "properties": {
                 "service": service,
                 "since": {"type": "string", "description": "YYYY-MM-DD or YYYY-MM-DD HH:MM."},
                 "until": {"type": "string", "description": "YYYY-MM-DD (that whole day) or YYYY-MM-DD HH:MM."},
                 "mode": {"type": "string", "enum": ["top", "spikes", "new"]},
                 "levels": {"type": "array", "items": {"type": "string", "enum": ["ERROR", "WARN"]}},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 25}}}},
            {"name": "log_search",
             "description": (
                 "The log lines about one client's attempt across the services: matched on an identifier the ticket "
                 "gives (account, mobile, or a reference/RRN with long digits) or on a correlation id (requestId, "
                 "sessionId, uuid, ...) seen in an earlier result, within a window. Returns the lines in time order "
                 "with the other lines of the same requests, so the trail through the services is visible: what each "
                 "service did and where it failed, including errors no database records. Lines are masked; each "
                 "result has a query_id (Q1, Q2, ...): start the evidence item it supports with it. CIFs are not "
                 f"logged as such. At most {MAX_SEARCH_ROWS} lines.{held}"),
             "input_schema": {"type": "object", "properties": {
                 "identifier": {"type": "object", "properties": {
                     "kind": {"type": "string", "enum": list(KINDS)},
                     "value": {"type": "string", "description": "The value from the ticket, e.g. <NUMBER_1>."}},
                     "required": ["kind", "value"]},
                 "correlation_id": {"type": "string", "description": "A correlation id from an earlier log result."},
                 "service": service,
                 "since": {"type": "string", "description": "Start, YYYY-MM-DD or YYYY-MM-DD HH:MM."},
                 "until": {"type": "string", "description": "End, YYYY-MM-DD (that whole day) or YYYY-MM-DD HH:MM."},
                 "levels": {"type": "array", "items": {"type": "string", "enum": ["INFO", "WARN", "ERROR"]},
                            "description": "Only these levels (default all)."},
                 "related": {"type": "boolean", "description": "Include the other lines of the same requests (default true)."},
                 "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_ROWS}}}},
        ]

    # ---- log_errors ---------------------------------------------------------------------------------------------------
    def errors(self, request: dict[str, Any]) -> dict[str, Any]:
        service = self._service(request.get("service"))
        mode = str(request.get("mode") or "top")
        if mode not in ("top", "spikes", "new"):
            raise ValueError("mode must be top, spikes or new")
        since, until = self._window(request)
        return self.store.errors(service, since, until, mode, request.get("levels") or ("ERROR", "WARN"),
                                 max(1, min(int(request.get("limit") or 12), 25)))

    # ---- log_search ----------------------------------------------------------------------------------------------------
    def search(self, request: dict[str, Any], identify: Identify,
               check_correlation: Optional[Callable[[str], None]] = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """(result, what was matched on: {kind, correlation})"""
        ident, corr = request.get("identifier"), str(request.get("correlation_id") or "").strip()
        if bool(ident) == bool(corr):
            raise ValueError("give either identifier {kind, value} or correlation_id")
        service = self._service(request.get("service"))
        since, until = self._window(request)
        hashes: list[str] = []
        kind = None
        if ident:
            if not isinstance(ident, dict):
                raise ValueError("identifier is {kind, value}")
            kind, value = str(ident.get("kind") or ""), str(ident.get("value") or "").strip()
            if kind == "cif":
                raise ValueError("a CIF is not logged as such: search on the client's account, mobile or a reference")
            if kind not in KINDS:
                raise ValueError(f"identifier kind must be one of {', '.join(KINDS)}")
            if not value:
                raise ValueError("identifier.value is required")
            forms = [f for f in identify(kind, value) if f]
            if not forms:
                raise ValueError(f"identifier.value is not a valid {kind}")
            hashes = self.masker.hashes_for(*forms)
            if not hashes:
                raise ValueError("this value has no long digits, which is what the logs are indexed on: search on the "
                                 "account, mobile or a reference with long digits, or on a correlation id")
        else:
            if not CORR_RE.match(corr):
                raise ValueError("correlation_id looks wrong: copy it from an earlier result")
            if check_correlation is not None:
                check_correlation(corr)
        out = self.store.search(hashes, corr or None, service, since, until, request.get("levels"),
                                int(request.get("limit") or DEFAULT_SEARCH_ROWS), request.get("related") is not False)
        return out, {"kind": kind or "correlation id", "window": (since, until)}

    # ---- shared ------------------------------------------------------------------------------------------------------------
    def _service(self, name: Any) -> Optional[str]:
        name = str(name or "").strip()
        if not name:
            return None
        if name not in self.services():
            raise ValueError(f"service must be one of {', '.join(self.services())}")
        return name

    @staticmethod
    def _window(request: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        since, until = parse_moment(request.get("since")), parse_moment(request.get("until"), end=True)
        if since and until and since >= until:
            raise ValueError("since must be before until")
        return stamp(since), stamp(until)


def run_tool(logs: LogTools, name: str, request: dict[str, Any], identify: Identify, queries: dict[str, dict[str, Any]],
             check_correlation: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """One log tool call for a desk, recorded in `queries` under its query_id (Q1, Q2, ...) like query_data. log_errors
    is service-wide (no client data): it is recorded as counts, so it can inform but never carries a close."""
    query_id = f"Q{len(queries) + 1}"
    if name == "log_errors":
        out = logs.errors(request)
        queries[query_id] = {"source": "logs: " + (str(request.get("service")) if request.get("service") else "all services"),
                             "mode": "count", "match_columns": [], "kind": None,
                             "rows": len(out.get("signatures", [])), "window": out.get("window")}
        return {"query_id": query_id, **out, "_no_follow_ups": True}
    out, matched = logs.search(request, identify, check_correlation)
    services = sorted(out.get("by_service", {}))
    queries[query_id] = {"source": "logs: " + (", ".join(services) or str(request.get("service") or "no match")),
                         "mode": "rows", "match_columns": ["identifier" if request.get("identifier") else "correlation_id"],
                         "kind": matched["kind"], "rows": out.get("events_shown", 0),
                         "window": {"from": matched["window"][0], "to": matched["window"][1]}}
    return {"query_id": query_id, **out}
