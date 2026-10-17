"""explore_data and query_tables (agentcore.schemaquery): what the agent may see and read. The agent names tables,
columns, joins and filters; the SQL is built in code, over the schema files only. All data invented."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from agentcore.dataquery import identifier_forms
from agentcore.schemaquery import Schema, SchemaQuery, build_for_desk, run_tool, temporal

SCHEMA = {
    "ib.json": {"database": "IB", "engine": "mysql", "tables": {
        "billpayment": {"rows": 280000, "columns": {
            "id": "bigint", "account_identification1": "varchar", "transaction_status": "varchar",
            "transaction_value": "decimal", "date_created": "datetime", "provider": "varchar",
            "response_body": "longtext", "transactionid": "varchar", "profile_type": "varchar",
            "token_voucher": "varchar", "transactional_token": "text", "token_units": "varchar"},
            "indexes": [["id"], ["date_created", "id"]]},
        "airtimepurchase": {"rows": 200000, "columns": {
            "id": "bigint", "account_identification1": "varchar", "transaction_status": "varchar",
            "date_created": "datetime", "is_txn_reversed": "tinyint", "provider": "varchar"},
            "match_only": ["msisdn"]},
        "branch": {"rows": 12, "columns": {"id": "bigint", "branch": "varchar", "city": "varchar"}},
        "textdates": {"rows": 40, "columns": {"id": "bigint", "account_identification1": "varchar",
                                              "dateCreated": "varchar", "status": "varchar"}},
    }},
    "rt.json": {"database": "Postilion Realtime", "engine": "sqlserver", "protected": True, "tables": {
        "dbo.post_tran_leg": {"rows": 5000000, "columns": {"datetime_req": "datetime", "rsp_code_rsp": "varchar",
                                                           "retrieval_reference_nr": "varchar", "sink_node_name": "varchar"},
                              "indexes": [["datetime_req"], ["retrieval_reference_nr"]]},
        "dbo.noindex": {"rows": 10, "columns": {"a": "varchar", "d": "datetime"}}}},
    "standby_db.json": {"database": "STANDBY DB", "engine": "oracle",
                        "aliases": ["Intellect CBS", "Intellect LMS"], "tables": {
        "NMBPRODIDC.ASIINNP": {"columns": {  # Oracle row counts are unknown
            "ASIIN_FIELD_037": "VARCHAR2", "ASIIN_MTI": "VARCHAR2", "ASIIN_IN_TIME": "TIMESTAMP(6)", "ASIIN_SERIAL": "NUMBER",
            "ASIIN_DATE": "DATE"}, "indexes": [["ASIIN_DATE", "ASIIN_FIELD_037"], ["ASIIN_FIELD_037"]]},
        "NMBPRODIDC.ASIOUTNP": {"rows": 9000000, "columns": {
            "ASIOUT_FIELD_037": "VARCHAR2", "ASIOUT_FIELD_039": "VARCHAR2", "ASIOUT_OUT_TIME": "TIMESTAMP(6)"}},
    }},
    "pg.json": {"database": "BILLERS", "engine": "postgres", "tables": {
        "public.zesa": {"rows": 1000, "columns": {"id": "int8", "status": "varchar", "date_created": "timestamp",
                                                  "amount": "float8", "account": "varchar"}}}},
}


class FakeMb:
    def __init__(self, rows=None):
        self.sql, self.rows, self.calls = [], rows or [], []

    def databases(self):
        return [{"id": 6, "name": "IB", "engine": "mysql"}, {"id": 81, "name": "STANDBY DB", "engine": "oracle"},
                {"id": 7, "name": "BILLERS", "engine": "postgres"}, {"id": 3, "name": "Postilion Realtime", "engine": "sqlserver"}]

    def query(self, db, sql):
        self.calls.append((db, sql))
        return self.rows


@pytest.fixture
def sq(tmp_path):
    for name, data in SCHEMA.items():
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    mb = FakeMb()
    q = SchemaQuery.from_directory(mb, tmp_path)
    q.test_mb = mb
    return q


def identify(kind, value):
    return identifier_forms(kind, value.replace("<NUMBER_1>", "86000013101001"))


NOW = datetime(2026, 10, 17, 12, 0)
BASE = {"database": "IB", "tables": [{"table": "billpayment", "as": "b"}],
        "match": {"columns": ["b.account_identification1"], "kind": "account", "value": "<NUMBER_1>"},
        "time": {"column": "b.date_created", "since": "2026-10-10", "until": "2026-10-17"},
        "columns": ["b.date_created", "b.transaction_status", "b.transaction_value"]}


def prep(sq, **changes):
    return sq.prepare({**BASE, **changes}, identify, now=NOW)


# ---- explore_data -------------------------------------------------------------------------------------------
def test_explore_lists_databases_tables_and_searches_names_and_columns(sq):
    listed = {d["database"]: d for d in sq.explore({})["databases"]}
    assert set(listed) == {"IB", "STANDBY DB", "BILLERS", "Postilion Realtime"}
    assert listed["STANDBY DB"]["also_known_as"] == ["Intellect CBS", "Intellect LMS"]
    assert "live production" in listed["Postilion Realtime"]["protected"] and "protected" not in listed["IB"]
    listing = sq.explore({"database": "IB"})
    assert "billpayment (~280000 rows)" in listing["tables"]
    found = sq.explore({"search": "transaction status"})["tables"]
    assert {t["table"] for t in found} >= {"billpayment", "airtimepurchase"}
    assert all(t["database"] == "IB" for t in found)
    table = sq.explore({"database": "IB", "table": "billpayment"})
    assert "date_created datetime" in table["columns"] and table["date_columns"] == ["date_created"]
    assert "response_body longtext [cannot be shown or filtered]" in table["columns"]
    assert "account_identification1" in table["identifier_like_columns"] and "large" in table
    assert sq.explore({"database": "IB", "table": "airtimepurchase"})["match_only_columns"] == ["msisdn"]


def test_explore_does_not_list_personal_columns_and_needs_a_known_table(sq):
    text = json.dumps(sq.explore({"database": "IB", "table": "billpayment"}))
    assert "customer_name" not in text
    with pytest.raises(ValueError, match="similar: billpayment"):
        sq.explore({"database": "IB", "table": "billpay"})
    with pytest.raises(ValueError, match="database must be one of"):
        sq.explore({"database": "OSTICKETS_NEW", "search": "x"})
    with pytest.raises(ValueError, match="table needs database"):
        sq.explore({"table": "billpayment"})


def test_the_temporal_check_tells_dates_from_text():
    assert temporal("datetime") and temporal("TIMESTAMP(6)") and temporal("timestamptz") and temporal("date")
    assert not temporal("varchar") and not temporal("text") and not temporal("bigint")


# ---- one table ----------------------------------------------------------------------------------------------
def test_a_matched_query_in_a_window_is_built_from_the_schema(sq):
    p = prep(sq)
    assert p.sql.startswith("SELECT b.`date_created` AS c1, b.`transaction_status` AS c2, b.`transaction_value` AS c3 "
                            "FROM `billpayment` b WHERE (b.`account_identification1` IN (")
    for form in ("'86000013101001'", "'00086000013101001'".replace("000", "", 1)):
        assert form in p.sql
    assert "b.`date_created` >= '2026-10-10 00:00:00'" in p.sql and "b.`date_created` < '2026-10-17 12:00:00'" not in p.sql
    assert p.sql.endswith("ORDER BY b.`date_created` DESC LIMIT 11") and p.match_columns == ["b.account_identification1"]


def test_without_a_match_only_counts_over_a_short_window_are_allowed(sq):
    with pytest.raises(ValueError, match="only matched on an identifier"):
        sq.prepare({k: v for k, v in BASE.items() if k != "match"}, identify, now=NOW)
    nomatch = {k: v for k, v in BASE.items() if k != "match"}
    with pytest.raises(ValueError, match="at most 2 day"):
        sq.prepare({**nomatch, "mode": "count", "count_by": ["b.transaction_status"]}, identify, now=NOW)
    p = sq.prepare({**nomatch, "mode": "count", "count_by": ["b.transaction_status"],
                    "time": {"column": "b.date_created", "since": "2026-10-16", "until": "2026-10-17"}}, identify, now=NOW)
    assert "COUNT(*) AS n" in p.sql and "GROUP BY c1" in p.sql and "ORDER BY n DESC" in p.sql
    with pytest.raises(ValueError, match="need a time window"):
        sq.prepare({"database": "IB", "tables": [{"table": "branch", "as": "x"}], "mode": "count"}, identify, now=NOW)


def test_counts_cannot_be_grouped_by_identifiers_or_free_text(sq):
    base = {"database": "IB", "tables": [{"table": "billpayment", "as": "b"}], "mode": "count",
            "time": {"column": "b.date_created", "since": "2026-10-16"}}
    for col in ("b.account_identification1", "b.transactionid", "b.id", "b.response_body"):
        with pytest.raises(ValueError, match="cannot count by"):
            sq.prepare({**base, "count_by": [col]}, identify, now=NOW)


def test_a_large_table_needs_a_window_and_the_window_is_limited(sq):
    with pytest.raises(ValueError, match="large table"):
        prep(sq, time=None)
    with pytest.raises(ValueError, match="at most 31 day"):
        prep(sq, time={"column": "b.date_created", "since": "2026-08-01", "until": "2026-10-17"})
    small = {"database": "IB", "tables": [{"table": "branch", "as": "x"}], "columns": ["x.branch"],
             "match": {"columns": ["x.city"], "kind": "reference", "value": "ABC123"}}
    assert sq.prepare(small, lambda k, v: ["ABC123"], now=NOW).sql.count("WHERE") == 1  # a small table needs no window


def test_a_large_table_is_only_read_on_an_indexed_column(sq):
    # billpayment is indexed on id and date_created: the window on date_created uses an index
    assert "date_created" in prep(sq).sql
    # airtimepurchase (large, no index information) is allowed: not known is not refused
    air = {"database": "IB", "tables": [{"table": "airtimepurchase", "as": "a"}], "columns": ["a.transaction_status"],
           "match": {"columns": ["a.account_identification1"], "kind": "account", "value": "<NUMBER_1>"},
           "time": {"column": "a.date_created", "since": "2026-10-16"}}
    assert "airtimepurchase" in sq.prepare(air, identify, now=NOW).sql
    # an Oracle table of unknown size with index information: the window on an unindexed column is refused, naming the indexed ones
    oracle = {"database": "STANDBY DB", "tables": [{"table": "NMBPRODIDC.ASIINNP", "as": "i"}], "columns": ["i.ASIIN_MTI"],
              "match": {"columns": ["i.ASIIN_SERIAL"], "kind": "reference", "value": "612345678901"},
              "time": {"column": "i.ASIIN_IN_TIME", "since": "2026-10-16"}}
    with pytest.raises(ValueError, match=r"nothing in this query uses an index.*ASIIN_DATE, ASIIN_FIELD_037"):
        sq.prepare(oracle, identify, now=NOW)
    ok = {**oracle, "time": {"column": "i.ASIIN_DATE", "since": "2026-10-16"}}
    # an Oracle DATE column is compared with a DATE literal: a TIMESTAMP literal would convert the column and skip the index
    assert "i.\"ASIIN_DATE\" >= TO_DATE('2026-10-16 00:00:00', 'YYYY-MM-DD HH24:MI:SS')" in sq.prepare(ok, identify, now=NOW).sql
    ok2 = {**oracle, "match": {**oracle["match"], "columns": ["i.ASIIN_FIELD_037"]}}  # the match itself is indexed
    assert sq.prepare(ok2, identify, now=NOW)


def test_a_join_on_an_indexed_column_counts_as_using_the_index(sq):
    # the join is on ASIIN_FIELD_037 (indexed); the window and match columns are not: still accepted
    req = {**JOIN, "time": {"column": "i.ASIIN_IN_TIME", "since": "2026-10-16"},
           "match": {"columns": ["i.ASIIN_SERIAL"], "kind": "reference", "value": "612345678901"}}
    assert sq.prepare(req, identify, now=NOW)
    # join on an unindexed column instead: refused
    other = {**req, "join": [{"left": "i.ASIIN_SERIAL", "right": "o.ASIOUT_FIELD_037"}]}
    with pytest.raises(ValueError, match="uses an index"):
        sq.prepare(other, identify, now=NOW)


def test_explore_lists_the_indexed_columns(sq):
    assert sq.explore({"database": "STANDBY DB", "table": "NMBPRODIDC.ASIINNP"})["indexed_columns"] == ["ASIIN_DATE", "ASIIN_FIELD_037"]
    assert "indexed_columns" not in sq.explore({"database": "IB", "table": "branch"})


def test_text_dates_are_compared_as_text_and_real_dates_as_dates(sq):
    p = sq.prepare({"database": "IB", "tables": [{"table": "textdates", "as": "t"}], "columns": ["t.status"],
                    "match": {"columns": ["t.account_identification1"], "kind": "account", "value": "<NUMBER_1>"},
                    "time": {"column": "t.dateCreated", "since": "2026-10-16"}}, identify, now=NOW)
    assert "t.`dateCreated` >= '2026-10-16 00:00:00'" in p.sql
    pg = sq.prepare({"database": "BILLERS", "tables": [{"table": "zesa", "as": "z"}], "columns": ["z.status"],
                     "match": {"columns": ["z.account"], "kind": "account", "value": "<NUMBER_1>"},
                     "time": {"column": "z.date_created", "since": "2026-10-16"}}, identify, now=NOW)
    assert "z.\"date_created\" >= TIMESTAMP '2026-10-16 00:00:00'" in pg.sql and pg.sql.endswith("LIMIT 11")


def test_engine_specific_sql_for_oracle_and_the_no_as_table_alias(sq):
    p = sq.prepare({"database": "STANDBY DB", "tables": [{"table": "NMBPRODIDC.ASIINNP", "as": "a"}],
                    "match": {"columns": ["a.ASIIN_FIELD_037"], "kind": "reference", "value": "612345678901"},
                    "time": {"column": "a.ASIIN_IN_TIME", "since": "2026-10-16"},
                    "columns": ["a.ASIIN_MTI", "a.ASIIN_SERIAL"]}, identify, now=NOW)
    assert p.sql.startswith("SELECT * FROM (SELECT a.\"ASIIN_MTI\" AS c1, a.\"ASIIN_SERIAL\" AS c2 FROM "
                            "\"NMBPRODIDC\".\"ASIINNP\" a WHERE") and p.sql.endswith("WHERE ROWNUM <= 11")
    assert "TIMESTAMP '2026-10-16 00:00:00'" in p.sql and " AS a " not in p.sql


# ---- joins --------------------------------------------------------------------------------------------------
JOIN = {"database": "STANDBY DB",
        "tables": [{"table": "NMBPRODIDC.ASIINNP", "as": "i"}, {"table": "NMBPRODIDC.ASIOUTNP", "as": "o"}],
        "join": [{"left": "i.ASIIN_FIELD_037", "right": "o.ASIOUT_FIELD_037"}],
        "match": {"columns": ["i.ASIIN_FIELD_037"], "kind": "reference", "value": "612345678901"},
        "time": {"column": "i.ASIIN_IN_TIME", "since": "2026-10-16"},
        "columns": ["i.ASIIN_MTI", "o.ASIOUT_FIELD_039"]}


def test_two_tables_are_joined_on_the_columns_the_agent_names(sq):
    p = sq.prepare(JOIN, identify, now=NOW)
    assert ("FROM \"NMBPRODIDC\".\"ASIINNP\" i JOIN \"NMBPRODIDC\".\"ASIOUTNP\" o ON i.\"ASIIN_FIELD_037\" = "
            "o.\"ASIOUT_FIELD_037\" WHERE") in p.sql
    assert p.labels == ["i.ASIIN_MTI", "o.ASIOUT_FIELD_039"] and p.sources == ["NMBPRODIDC.ASIINNP", "NMBPRODIDC.ASIOUTNP"]
    left = sq.prepare({**JOIN, "join": [{**JOIN["join"][0], "how": "left"}]}, identify, now=NOW)
    assert " LEFT JOIN " in left.sql


def test_joins_must_connect_every_table_and_name_real_columns(sq):
    with pytest.raises(ValueError, match="needs a join"):
        sq.prepare({**JOIN, "join": []}, identify, now=NOW)
    with pytest.raises(ValueError, match="not a column of NMBPRODIDC.ASIOUTNP"):
        sq.prepare({**JOIN, "join": [{"left": "i.ASIIN_FIELD_037", "right": "o.NOPE"}]}, identify, now=NOW)
    with pytest.raises(ValueError, match="alias"):
        sq.prepare({**JOIN, "join": [{"left": "i.ASIIN_FIELD_037", "right": "z.ASIOUT_FIELD_037"}]}, identify, now=NOW)
    with pytest.raises(ValueError, match="different tables"):
        sq.prepare({**JOIN, "join": [{"left": "i.ASIIN_MTI", "right": "i.ASIIN_SERIAL"}]}, identify, now=NOW)
    four = {**JOIN, "tables": JOIN["tables"] + [{"table": "NMBPRODIDC.ASIINNP", "as": "x"},
                                                {"table": "NMBPRODIDC.ASIINNP", "as": "y"}]}
    with pytest.raises(ValueError, match="one to 3 tables"):
        sq.prepare(four, identify, now=NOW)
    with pytest.raises(ValueError, match="alias"):
        sq.prepare({**JOIN, "tables": [{"table": "NMBPRODIDC.ASIINNP", "as": "i"}, {"table": "NMBPRODIDC.ASIOUTNP", "as": "i"}]},
                   identify, now=NOW)


def test_tables_of_two_databases_cannot_be_joined(sq):
    with pytest.raises(ValueError, match="not a table of IB"):
        sq.prepare({**BASE, "tables": [{"table": "billpayment", "as": "b"}, {"table": "public.zesa", "as": "z"}]},
                   identify, now=NOW)


def test_three_tables_chain_through_their_joins(sq):
    req = {**BASE, "tables": [{"table": "billpayment", "as": "b"}, {"table": "airtimepurchase", "as": "a"},
                              {"table": "branch", "as": "r"}],
           "join": [{"left": "b.account_identification1", "right": "a.account_identification1"},
                    {"left": "a.provider", "right": "r.branch"}]}
    p = sq.prepare(req, identify, now=NOW)
    assert p.sql.index(" JOIN `airtimepurchase` a ON ") < p.sql.index(" JOIN `branch` r ON ")


# ---- what can never be read ---------------------------------------------------------------------------------
def test_personal_and_free_text_columns_cannot_be_named_shown_or_filtered(sq):
    with pytest.raises(ValueError, match="not a column of billpayment"):
        prep(sq, columns=["b.customer_name"])  # personal columns are not in the schema at all
    with pytest.raises(ValueError, match="free text or a secret and cannot be shown"):
        prep(sq, columns=["b.response_body"])
    with pytest.raises(ValueError, match="free text or a secret and cannot be filtered"):
        prep(sq, where=[{"column": "b.response_body", "op": "contains", "value": "x"}])


def test_token_and_voucher_columns_are_never_shown_filtered_or_counted_but_amounts_of_a_token_are(sq):
    for col in ("b.token_voucher", "b.transactional_token"):
        with pytest.raises(ValueError, match="secret"):
            prep(sq, columns=[col])
        with pytest.raises(ValueError, match="secret"):
            prep(sq, where=[{"column": col, "op": "=", "value": "x"}])
    assert "b.`token_units` AS c1" in prep(sq, columns=["b.token_units"]).sql


def test_a_mobile_column_matches_a_mobile_but_is_never_shown_or_matched_by_other_kinds(sq):
    mobile = {"database": "IB", "tables": [{"table": "airtimepurchase", "as": "a"}], "columns": ["a.transaction_status"],
              "match": {"columns": ["a.msisdn"], "kind": "mobile", "value": "0772123456"},
              "time": {"column": "a.date_created", "since": "2026-10-16"}}
    p = sq.prepare(mobile, lambda k, v: ["263772123456", "0772123456", "772123456"], now=NOW)
    assert "a.`msisdn` IN ('263772123456', '0772123456', '772123456')" in p.sql
    with pytest.raises(ValueError, match="not a column of airtimepurchase"):  # for any other kind it does not exist
        sq.prepare({**mobile, "match": {**mobile["match"], "kind": "account", "value": "<NUMBER_1>"}}, identify, now=NOW)
    with pytest.raises(ValueError, match="not a column of airtimepurchase"):
        sq.prepare({**mobile, "columns": ["a.msisdn"]}, lambda k, v: ["x"], now=NOW)


def test_values_are_escaped_and_numbers_go_through_match_not_filters(sq):
    p = prep(sq, where=[{"column": "b.provider", "op": "=", "value": "O'Hara\\"}])
    assert "b.`provider` = 'O''Hara\\\\'" in p.sql  # MySQL: quote doubled and backslash escaped
    with pytest.raises(ValueError, match="go in match.value"):
        prep(sq, where=[{"column": "b.provider", "op": "=", "value": "0772123456"}])
    with pytest.raises(ValueError, match="alias.column"):
        prep(sq, where=[{"column": "b.provider; DROP TABLE x", "op": "=", "value": "a"}])
    with pytest.raises(ValueError, match="alias"):
        prep(sq, tables=[{"table": "billpayment", "as": "b; --"}])


def test_the_value_must_be_one_the_desk_accepts(sq):
    def refuse(kind, value):
        raise ValueError("match value (account) is not on this ticket")
    with pytest.raises(ValueError, match="not on this ticket"):
        sq.prepare(BASE, refuse, now=NOW)
    with pytest.raises(ValueError, match="not a valid account"):
        sq.prepare(BASE, lambda k, v: [], now=NOW)
    with pytest.raises(ValueError, match="match kind"):
        prep(sq, match={"columns": ["b.account_identification1"], "kind": "card", "value": "x"})


def test_rows_need_columns_and_the_row_cap_holds(sq):
    with pytest.raises(ValueError, match="name 1 to 30"):
        prep(sq, columns=[])
    assert prep(sq, limit=500).sql.endswith("LIMIT 26")


# ---- running and recording ----------------------------------------------------------------------------------
def test_run_maps_columns_back_to_their_names_and_marks_the_window(sq):
    sq.test_mb.rows = [{"c1": "2026-10-16 10:00:00", "c2": "FAILED", "c3": "20.00"}]
    out = sq.run(prep(sq))
    assert out["rows"] == [{"b.date_created": "2026-10-16 10:00:00", "b.transaction_status": "FAILED",
                            "b.transaction_value": "20.00"}]
    assert out["matched_on"] == {"columns": ["b.account_identification1"], "kind": "account"}
    assert out["window"]["from"] == "2026-10-10 00:00" and out["tables"] == ["billpayment"]
    sq.test_mb.rows = []
    assert "Nothing found" in sq.run(prep(sq))["note"]


def test_counts_come_back_without_long_numbers(sq):
    sq.test_mb.rows = [{"c1": "Error for 86000013101001", "n": 4, "first_seen": "a", "last_seen": "b"}]
    nomatch = {k: v for k, v in BASE.items() if k != "match"}
    out = sq.run(sq.prepare({**nomatch, "mode": "count", "count_by": ["b.transaction_status"],
                             "time": {"column": "b.date_created", "since": "2026-10-16"}}, identify, now=NOW))
    assert out["rows"][0]["b.transaction_status"] == "Error for [number]" and out["rows"][0]["n"] == 4


def test_run_tool_records_a_citable_query_and_explore_is_not_recorded(sq):
    queries: dict = {}
    assert "databases" in run_tool(sq, "explore_data", {}, identify, queries) and not queries
    sq.test_mb.rows = [{"c1": "x", "c2": "y", "c3": "1"}]
    out = run_tool(sq, "query_tables", BASE, identify, queries, as_of=date(2026, 10, 17))
    assert out["query_id"] == "Q1" and queries["Q1"]["rows"] == 1 and queries["Q1"]["mode"] == "rows"
    assert queries["Q1"]["match_columns"] == ["b.account_identification1"] and queries["Q1"]["source"] == "IB: billpayment"
    assert "_no_follow_ups" not in out
    nomatch = {k: v for k, v in BASE.items() if k != "match"}
    out = run_tool(sq, "query_tables", {**nomatch, "mode": "count", "count_by": ["b.provider"],
                                        "time": {"column": "b.date_created", "since": "2026-10-16"}},
                   identify, queries, as_of=date(2026, 10, 17))
    assert out["_no_follow_ups"] is True and out["query_id"] == "Q2"


def test_a_database_missing_from_metabase_is_left_out_and_bad_schema_files_turn_it_off(tmp_path):
    (tmp_path / "x.json").write_text(json.dumps({"database": "Gone", "engine": "postgres", "tables": {"public.t": {
        "columns": {"a": "int"}}}}), encoding="utf-8")
    assert build_for_desk(FakeMb(), True, tmp_path) is None  # nothing left
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert build_for_desk(FakeMb(), True, tmp_path) is None
    assert build_for_desk(FakeMb(), False) is None


def test_the_shipped_schema_files_load_and_never_list_personal_columns():
    schema = Schema.load()
    assert "ib" in schema.databases and "billers" in schema.databases
    from agentcore.dataquery import personal
    bad = [(d.name, t.key, c) for d in schema.databases.values() for t in d.tables.values() for c in t.columns
           if personal({"name": c})]
    assert not bad, bad[:10]


# ---- aliases and the protected (live production) tier ---------------------------------------------------------
def test_an_alias_names_the_same_database(sq):
    assert sq.explore({"database": "Intellect CBS", "search": "asiinnp"})["tables"][0]["database"] == "STANDBY DB"
    req = {**JOIN, "database": "Intellect LMS"}
    p = sq.prepare(req, identify, now=NOW)
    assert p.database.name == "STANDBY DB" and p.database.db_id == 81


RT = {"database": "Postilion Realtime", "tables": [{"table": "dbo.post_tran_leg", "as": "p"}],
      "match": {"columns": ["p.retrieval_reference_nr"], "kind": "reference", "value": "612345678901"},
      "time": {"column": "p.datetime_req", "since": "2026-10-16"}, "columns": ["p.rsp_code_rsp"]}


def test_the_live_postilion_database_is_read_only_on_an_index_in_a_short_window(sq):
    p = sq.prepare(RT, identify, now=NOW)
    assert "TOP 11" in p.sql and "[datetime_req] >= '2026-10-16T00:00:00'" in p.sql
    with pytest.raises(ValueError, match="at most 2 day"):
        sq.prepare({**RT, "time": {"column": "p.datetime_req", "since": "2026-10-01"}}, identify, now=NOW)
    with pytest.raises(ValueError, match="live production database: give `time`"):
        sq.prepare({k: v for k, v in RT.items() if k != "time"}, identify, now=NOW)
    # nothing on an index: not even a small table is read on a live database
    with pytest.raises(ValueError, match="no known index"):
        sq.prepare({"database": "Postilion Realtime", "tables": [{"table": "dbo.noindex", "as": "n"}], "columns": ["n.a"],
                    "match": {"columns": ["n.a"], "kind": "reference", "value": "612345678901"},
                    "time": {"column": "n.d", "since": "2026-10-16"}}, identify, now=NOW)
    unindexed = {**RT, "match": {**RT["match"], "columns": ["p.sink_node_name"]},
                 "time": {"column": "p.rsp_code_rsp", "since": "2026-10-16"}}
    with pytest.raises(ValueError, match="uses an index"):
        sq.prepare(unindexed, identify, now=NOW)


def test_counts_on_the_live_postilion_database_need_an_indexed_window(sq):
    base = {"database": "Postilion Realtime", "tables": [{"table": "dbo.post_tran_leg", "as": "p"}], "mode": "count",
            "count_by": ["p.rsp_code_rsp"], "time": {"column": "p.datetime_req", "since": "2026-10-17"}}
    assert "GROUP BY" in sq.prepare(base, identify, now=NOW).sql
    with pytest.raises(ValueError, match="live production database: give `time`"):
        sq.prepare({k: v for k, v in base.items() if k != "time"}, identify, now=NOW)
