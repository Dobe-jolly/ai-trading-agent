from __future__ import annotations

import json

import httpx
import pytest

from agent.__main__ import main
from agent.metabase import MetabaseClient, MetabaseError

OK = {"status": "completed", "data": {"cols": [{"name": "id"}, {"name": "v"}], "rows": [[1, "a"], [2, "b"]]}}


def client(handler, **kw) -> MetabaseClient:
    return MetabaseClient("http://mb.test/", "secret", transport=httpx.MockTransport(handler), retry_delay=0, **kw)


def test_native_query_is_literal_sql_with_api_key():
    seen = {}

    def handler(request):
        seen["headers"], seen["body"] = request.headers, json.loads(request.content)
        return httpx.Response(202, json=OK)

    assert client(handler).query(5, "SELECT 1") == [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]
    assert seen["headers"]["x-api-key"] == "secret"
    assert seen["body"] == {"database": 5, "type": "native", "native": {"query": "SELECT 1"}, "parameters": []}


def test_database_resolution_by_name_both_response_shapes():
    for payload in ({"data": [{"id": 121, "name": "OSTICKETS_NEW"}]}, [{"id": 121, "name": "OSTICKETS_NEW"}]):
        mb = client(lambda r, p=payload: httpx.Response(200, json=p))
        assert mb.resolve("ostickets_new") == 121
        assert mb.try_resolve("NOPE") is None


def test_transient_read_failures_retry_then_succeed():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503)
        if len(calls) == 2:
            return httpx.Response(202, json={"status": "failed", "via": [{"error": "Communications link failure"}]})
        return httpx.Response(202, json=OK)

    assert len(client(handler).query(5, "SELECT 1")) == 2 and len(calls) == 3


def test_permanent_errors_do_not_retry():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(202, json={"status": "failed", "via": [{"error": "ORA-00942: table or view does not exist"}]})

    with pytest.raises(MetabaseError, match="ORA-00942"):
        client(handler).query(5, "SELECT 1")
    assert len(calls) == 1


def test_write_tolerates_no_result_set_and_never_retries():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(202, json={"status": "failed", "via": [{"error": "Select statement did not produce a ResultSet for native query"}]})

    client(handler).write(121, "INSERT ...")
    assert len(calls) == 1

    def bad(request):
        calls.append(1)
        return httpx.Response(503)

    with pytest.raises(MetabaseError):
        client(bad).write(121, "INSERT ...")
    assert len(calls) == 2


def test_cli_report_runs_without_network(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB", str(settings.state_db))
    out = tmp_path / "out.csv"
    assert main(["--env-file", str(tmp_path / "none.env"), "report", "--since", "2026-01-01", "--out", str(out)]) == 0
    assert out.read_text().startswith("created_at,mode,ticket_number")


def test_config_problems(settings):
    from dataclasses import replace

    assert settings.problems() == []
    assert any("REPLY_STAFF_ID" in p for p in replace(settings, reply_staff_id=None).problems("live"))
    assert any("ECOCASH_HELP_TOPICS" in p for p in replace(settings, ecocash_topics=[]).problems())
    assert any("MONITOR_DB_URL" in p for p in replace(settings, monitor_db_url=None).monitor_problems())
    assert replace(settings, monitor_db_url="jdbc:postgresql://h:5422/ecocash_monitor").monitor_problems() == []
