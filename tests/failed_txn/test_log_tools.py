"""log_errors and log_search as the desk's model calls them: the same rules as the other lookups about whose numbers may
be searched, correlation ids only from this ticket or an earlier log result, every call recorded under a query id, and a
log store that fails not blocking the ticket. All data invented."""
from __future__ import annotations

import json
import sqlite3

import pytest

from agentcore.logs.mask import KeyedMasker
from agentcore.logs.profiles import Profile
from agentcore.logs.store import LogStore
from agentcore.logs.tools import LogTools
from agentcore.pii import Pseudonymizer
from failed_txn.tools import Evidence, Toolbox
from tests.core.test_logs import ACCOUNT, KEY, OTHER, build, burst, line, write
from tests.failed_txn.test_tools_and_guardrails import run, setup, ticket


@pytest.fixture
def toolbox(tmp_path):
    masker = KeyedMasker(KEY)
    store = LogStore(tmp_path / "logs.db")
    build(tmp_path, masker, store, extra=[
        line("2026-10-17 15:30:00", "INFO", "Controller", f"Debit started accountNumber={ACCOUNT} amount=50",
             mdc="[requestId=trail-0001-zzzz] "),
        line("2026-10-17 15:30:02", "ERROR", "WebClientService", "Intellect request failed with 500",
             mdc="[requestId=trail-0001-zzzz] "),
    ] + burst("15:15", 30, "HikariPool-PostgreSQL - Connection is not available"))
    return Toolbox(logs=LogTools(store, masker, {"ussd": Profile(service="ussd", about="USSD gateway")}))


def start():
    t, pseudo = ticket(), Pseudonymizer()
    return t, pseudo, setup(None, pseudo, t)


def search(account="<NUMBER_1>", **extra):
    return {"identifier": {"kind": "account", "value": account}, "since": "2026-10-17 15:29", "until": "2026-10-17 15:32", **extra}


def test_the_log_tools_are_offered_only_when_there_is_a_log_store(toolbox):
    assert {"log_errors", "log_search"} <= {x["name"] for x in toolbox.schemas()}
    assert not {"log_errors", "log_search"} & {x["name"] for x in Toolbox().schemas()}


def test_log_search_on_the_tickets_account_returns_the_trail_masked_and_recorded(toolbox):
    t, pseudo, ev = start()
    out = run(toolbox, "log_search", search(), pseudo, ev, t)
    assert out["query_id"] == "Q1" and out["matched_directly"] == 1
    messages = [e["message"] for e in out["events"]]
    assert any("Debit started" in m for m in messages) and any("Intellect request failed" in m for m in messages)
    assert ACCOUNT not in json.dumps(out) and "<NUMBER:" in json.dumps(out)  # a keyed hash, never the number
    rec = ev.queries["Q1"]
    assert rec["mode"] == "rows" and rec["owner_given"] is True and rec["match_columns"] == ["identifier"] and rec["rows"] == 2
    assert ev.searches[-1]["tool"] == "log_search" and ev.log_keys["trail-0001-zzzz"] is True


def test_another_clients_number_is_refused_and_never_reaches_the_store(toolbox):
    t, pseudo, ev = start()
    text = run(toolbox, "log_search", search(account=OTHER), pseudo, ev, t)
    assert "not on this ticket" in text and ev.refused == ["log_search account"] and not ev.queries
    token = run(toolbox, "log_search", search(account="<NUMBER_9>"), pseudo, ev, t)
    assert "is not a value from this ticket" in token


def test_a_correlation_id_must_come_from_the_ticket_or_an_earlier_log_result(toolbox):
    t, pseudo, ev = start()
    refused = run(toolbox, "log_search", {"correlation_id": "trail-0001-zzzz"}, pseudo, ev, t)
    assert "not from this ticket or an earlier log result" in refused and ev.refused == ["log_search correlation id"]
    run(toolbox, "log_search", search(), pseudo, ev, t)  # now the id has been seen, for the ticket owner
    out = run(toolbox, "log_search", {"correlation_id": "trail-0001-zzzz"}, pseudo, ev, t)
    assert out["events_shown"] == 2 and ev.queries["Q2"]["owner_given"] is True and ev.queries["Q2"]["kind"] == "correlation id"
    t2, pseudo2, ev2 = start()
    t2.entries[0].body += " ref trail-0001-zzzz"  # an id the ticket itself gives
    assert run(toolbox, "log_search", {"correlation_id": "trail-0001-zzzz"}, pseudo2, ev2, t2)["events_shown"] == 2


def test_a_correlation_id_found_through_another_clients_search_is_not_the_owners(toolbox):
    t, pseudo, ev = start()
    ev.log_keys["other-req-0001"] = False  # seen in a result that was not about the ticket owner
    out = run(toolbox, "log_search", {"correlation_id": "other-req-0001"}, pseudo, ev, t)
    assert ev.queries[out["query_id"]]["owner_given"] is False  # so a close cannot rest on it


def test_log_errors_is_service_wide_recorded_as_counts_and_never_widens_what_may_be_searched(toolbox):
    t, pseudo, ev = start()
    out = run(toolbox, "log_errors", {"mode": "spikes"}, pseudo, ev, t)
    assert out["signatures"][0]["count"] == 30 and out["signatures"][0]["peak_at"] == "2026-10-17 15:15"
    assert "_no_follow_ups" not in out
    rec = ev.queries["Q1"]
    assert rec["mode"] == "count" and rec["match_columns"] == [] and rec["owner_given"] is False
    assert run(toolbox, "log_errors", {"mode": "bad"}, pseudo, ev, t).startswith("Invalid input: mode must be")


def test_a_failing_log_store_is_reported_and_does_not_block_the_ticket(toolbox):
    t, pseudo, ev = start()
    toolbox.logs.store.db.close()
    text = run(toolbox, "log_search", search(), pseudo, ev, t)
    assert "could not be read" in text and ev.unread and ev.unread[0].startswith("log_search") and not ev.check_errors
