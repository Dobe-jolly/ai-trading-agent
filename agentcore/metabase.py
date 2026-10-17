"""Metabase API client, ported from the Java MetabaseClient.

- Auth: X-API-Key header.
- Databases are resolved by name (ids differ per environment) and cached per run.
- Reads retry transient failures (connection reset, timeouts, 502/503/504).
- Writes are never retried, and "did not produce a ResultSet" counts as success.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

TRANSIENT_MARKERS = (
    "could not connect", "connection reset", "connection refused", "communications link failure",
    "read timed out", "connect timed out", "i/o error",
)


class MetabaseError(RuntimeError):
    pass


class TransientMetabaseError(MetabaseError):
    pass


class MetabaseClient:
    def __init__(
        self,
        host: str,
        api_key: str,
        timeout: float = 180.0,
        retry_count: int = 3,
        retry_delay: float = 5.0,
        verify: bool | str = True,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        if not api_key:
            raise ValueError("METABASE_API_KEY is required")
        self._http = httpx.Client(
            base_url=host.rstrip("/"),
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=timeout,
            verify=verify,
            transport=transport,
        )
        self._retry_count = max(1, retry_count)
        self._retry_delay = max(0.0, retry_delay)
        self._databases: Optional[list[dict[str, Any]]] = None
        self._resolved: dict[str, int] = {}

    def close(self) -> None:
        self._http.close()

    # ---- transport ----------------------------------------------------------
    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        try:
            response = self._http.request(method, path, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TransientMetabaseError(f"{path}: {type(exc).__name__}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise MetabaseError(f"{path}: {type(exc).__name__}: {exc}") from exc
        if response.status_code in (502, 503, 504):
            raise TransientMetabaseError(f"{path}: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise MetabaseError(f"{path}: HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise MetabaseError(f"{path}: response was not JSON") from exc

    @staticmethod
    def _error_text(result: dict) -> str:
        via = result.get("via")
        if isinstance(via, list) and via and isinstance(via[0], dict) and via[0].get("error"):
            return str(via[0]["error"])
        return str(result.get("error") or "Unknown Metabase query error")

    @staticmethod
    def _payload(database_id: int, sql: str) -> dict[str, Any]:
        return {"database": int(database_id), "type": "native", "native": {"query": sql}, "parameters": []}

    # ---- databases ------------------------------------------------------------
    def databases(self) -> list[dict[str, Any]]:
        if self._databases is None:
            data = self._request("GET", "/api/database")
            items = data.get("data", []) if isinstance(data, dict) else data
            self._databases = [d for d in items if isinstance(d, dict)]
        return self._databases

    def resolve(self, name: str) -> int:
        """Database id for a Metabase database name (case-insensitive), cached."""
        key = name.strip().lower()
        if key not in self._resolved:
            for db in self.databases():
                if str(db.get("name", "")).strip().lower() == key:
                    self._resolved[key] = int(db["id"])
                    break
            else:
                raise MetabaseError(f"Database named '{name}' was not found in Metabase")
        return self._resolved[key]

    def try_resolve(self, name: str) -> Optional[int]:
        try:
            return self.resolve(name)
        except MetabaseError as exc:
            log.warning("Database '%s' unavailable: %s", name, exc)
            return None

    # ---- queries ----------------------------------------------------------------
    @staticmethod
    def rows(result: dict) -> list[dict[str, Any]]:
        data = result.get("data") or {}
        columns = [col.get("name") for col in data.get("cols", [])]
        return [dict(zip(columns, row)) for row in data.get("rows", [])]

    def query(self, database_id: int, sql: str) -> list[dict[str, Any]]:
        """Read query with the Java's retry policy for transient failures."""
        last: Optional[Exception] = None
        for attempt in range(1, self._retry_count + 1):
            try:
                result = self._request("POST", "/api/dataset", self._payload(database_id, sql))
                if str(result.get("status", "")).lower() == "failed":
                    text = self._error_text(result)
                    if any(marker in text.lower() for marker in TRANSIENT_MARKERS):
                        raise TransientMetabaseError(text)
                    raise MetabaseError(text)
                return self.rows(result)
            except TransientMetabaseError as exc:
                last = exc
                if attempt >= self._retry_count:
                    break
                log.warning("Metabase read attempt %d/%d failed (%s); retrying", attempt, self._retry_count, exc)
                time.sleep(self._retry_delay)
        raise MetabaseError(str(last))

    def query_bound(self, database_id: int, sql: str, params: dict[str, tuple[str, Any]]) -> list[dict[str, Any]]:
        """A read query written by a person, with {{name}} template tags that Metabase fills in: values are never
        pasted into the SQL here. params: {name: (type, value)}, type text | number | date."""
        tags, parameters = {}, []
        for name, (kind, value) in params.items():
            tag_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"nmbz-knowledge/{name}"))
            tags[name] = {"id": tag_id, "name": name, "display-name": name, "type": kind}
            target = ["variable", ["template-tag", name]]
            if kind == "number":
                parameters.append({"id": tag_id, "type": "number/=", "target": target, "value": [float(value)]})
            elif kind == "date":
                parameters.append({"id": tag_id, "type": "date/single", "target": target, "value": str(value)})
            else:
                parameters.append({"id": tag_id, "type": "category", "target": target, "value": str(value)})
        payload = {"database": int(database_id), "type": "native",
                   "native": {"query": sql, "template-tags": tags}, "parameters": parameters}
        result = self._request("POST", "/api/dataset", payload)
        if str(result.get("status", "")).lower() == "failed":
            raise MetabaseError(self._error_text(result))
        return self.rows(result)

    def write(self, database_id: int, sql: str) -> None:
        """INSERT/UPDATE. Not retried (a retry could double-post)."""
        result = self._request("POST", "/api/dataset", self._payload(database_id, sql))
        if str(result.get("status", "")).lower() == "failed":
            text = self._error_text(result)
            # MySQL and PostgreSQL drivers say this when a write (or DDL) succeeded but returned no rows.
            if not any(s in text for s in ("did not produce a ResultSet", "No results were returned by the query")):
                raise MetabaseError(text)
