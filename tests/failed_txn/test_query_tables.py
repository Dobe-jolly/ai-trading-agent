"""explore_data and query_tables as the desk's model calls them: the same rules as the other lookups about whose
numbers may be matched, every query recorded under an id a close may cite, and a failed read that does not block the
ticket. All data invented."""
from __future__ import annotations

import json

import pytest

from agentcore.metabase import MetabaseError
from agentcore.pii import Pseudonymizer
from agentcore.schemaquery import SchemaQuery
from failed_txn.decision import Decision
from failed_txn.guardrails import query_close_problem
from failed_txn.tools import Evidence, Toolbox
from tests.failed_txn.test_tools_and_guardrails import run, setup, ticket

SCHEMA = {"database": "IB", "engine": "mysql", "tables": {
    "billpayment": {"rows": 500, "columns": {
        "id": "bigint", "account_identification1": "varchar", "transaction_status": "varchar",
        "transaction_value": "decimal", "date_created": "datetime", "provider": "varchar"}},
    "airtimepurchase": {"rows": 500, "columns": {
        "id": "bigint", "account_identification1": "varchar", "transaction_status": "varchar",
        "date_created": "datetime", "is_txn_reversed": "tinyint"}}}}


class Mb:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.sql = rows or [], fail, []

    def databases(self):
        return [{"id": 6, "name": "IB", "engine": "mysql"}]

    def query(self, db, sql):
        self.sql.append(sql)
        if self.fail:
            raise MetabaseError("Table 'x' doesn't exist")
        return self.rows


def box(tmp_path, mb):
    (tmp_path / "ib.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    return Toolbox(schema=SchemaQuery.from_directory(mb, tmp_path))


def request(**changes):
    return {"database": "IB", "tables": [{"table": "billpayment", "as": "b"}, {"table": "airtimepurchase", "as": "a"}],
            "join": [{"left": "b.account_identification1", "right": "a.account_identification1"}],
            "match": {"columns": ["b.account_identification1"], "kind": "account", "value": "<NUMBER_1>"},
            "time": {"column": "b.date_created", "since": "2026-10-01"},
            "columns": ["b.transaction_status", "a.is_txn_reversed"], **changes}


def start():
    t, pseudo = ticket(), Pseudonymizer()
    return t, pseudo, setup(None, pseudo, t)


def test_the_tools_are_offered_only_when_the_schema_is_there(tmp_path):
    names = [x["name"] for x in box(tmp_path, Mb()).schemas()]
    assert "explore_data" in names and "query_tables" in names
    assert "query_tables" not in [x["name"] for x in Toolbox().schemas()]


def test_explore_data_shows_names_and_types_and_is_not_a_recorded_query(tmp_path):
    t, pseudo, ev = start()
    out = run(box(tmp_path, Mb()), "explore_data", {"database": "IB", "search": "reversed"}, pseudo, ev, t)
    assert out["tables"][0]["table"] == "airtimepurchase" and "is_txn_reversed tinyint" in out["tables"][0]["columns"]
    assert not ev.queries and not ev.refused


def test_query_tables_on_the_tickets_own_account_is_run_and_recorded(tmp_path):
    t, pseudo, ev = start()
    mb = Mb([{"c1": "FAILED", "c2": "0"}])
    out = run(box(tmp_path, mb), "query_tables", request(), pseudo, ev, t)
    assert out["query_id"] == "Q1" and out["rows"] == [{"b.transaction_status": "FAILED", "a.is_txn_reversed": "0"}]
    assert "JOIN `airtimepurchase` a ON b.`account_identification1` = a.`account_identification1`" in mb.sql[0]
    assert "86000013101001" in mb.sql[0]  # the ticket's real number, never the token, goes to the database
    rec = ev.queries["Q1"]
    assert rec["rows"] == 1 and rec["owner_given"] is True and rec["match_columns"] == ["b.account_identification1"]
    assert rec["source"] == "IB: billpayment + airtimepurchase"
    assert ev.searches[-1]["tool"] == "query_tables" and ev.searches[-1]["found"] == 1


def test_an_account_the_ticket_does_not_give_is_refused(tmp_path):
    t, pseudo, ev = start()
    mb = Mb()
    other = request(match={"columns": ["b.account_identification1"], "kind": "account", "value": "86000099999999"})
    text = run(box(tmp_path, mb), "query_tables", other, pseudo, ev, t)
    assert "not on this ticket" in text and ev.refused == ["query_tables account"] and not mb.sql
    token = run(box(tmp_path, mb), "query_tables",
                request(match={"columns": ["b.account_identification1"], "kind": "account", "value": "<NUMBER_9>"}),
                pseudo, ev, t)
    assert "is not a value from this ticket" in token and not mb.sql


def test_counts_over_other_clients_do_not_widen_what_may_be_searched(tmp_path):
    t, pseudo, ev = start()
    mb = Mb([{"c1": "Failed for 86000077777777", "n": 40, "first_seen": "a", "last_seen": "b"}])
    out = run(box(tmp_path, mb), "query_tables", {
        "database": "IB", "tables": [{"table": "billpayment", "as": "b"}], "mode": "count",
        "count_by": ["b.provider"], "time": {"column": "b.date_created", "since": "2026-10-16"}}, pseudo, ev, t)
    assert out["rows"][0]["b.provider"] == "Failed for [number]" and out["rows"][0]["n"] == 40  # no number reaches the model
    assert ev.queries["Q1"]["match_columns"] == [] and "86000077777777" not in ev.known.accounts


def test_a_bad_request_says_what_is_wrong_and_a_failed_read_does_not_block_the_ticket(tmp_path):
    t, pseudo, ev = start()
    text = run(box(tmp_path, Mb()), "query_tables", request(columns=["b.customer_name"]), pseudo, ev, t)
    assert "not a column of billpayment" in text and not ev.unread
    text = run(box(tmp_path, Mb(fail=True)), "query_tables", request(), pseudo, ev, t)
    assert "Query failed" in text and ev.unread and ev.unread[0].startswith("query_tables IB") and not ev.check_errors


def test_a_close_can_rest_on_a_query_tables_finding_like_one_on_query_data(tmp_path):
    t, pseudo, ev = start()
    run(box(tmp_path, Mb([{"c1": "FAILED", "c2": "0"}])), "query_tables", request(), pseudo, ev, t)
    decision = Decision("reply_and_close", 0.95, "x", "reason", ["Q1: bill payment failed, not reversed"],
                        "The payment failed and no money left the account.")
    problem, found = query_close_problem(decision, t, ev)
    assert found == ["Q1"] and "core banking postings were not checked" in problem  # accepted as a finding, then checked
    empty = Evidence()
    assert query_close_problem(decision, t, empty)[1] == []
