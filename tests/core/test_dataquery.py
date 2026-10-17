"""agentcore.dataquery: the approved catalogue and the SQL query_data builds from a structured request. All data
invented."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from agentcore.dataquery import CatalogueError, DataQuery, Source, load_catalogue, personal

NOW = datetime(2026, 7, 2, 15, 0)


def source(**kw) -> Source:
    base = dict(key="zipit", database="Postilion Billers", table="public.zipit", about="ZIPIT log.",
                columns=["date_created", "amount", "status", "error_code", "source_account", "rrn", "mobile_number"],
                identifiers={"source_account": "account", "rrn": "reference", "mobile_number": "mobile"},
                time_column="date_created", max_days=31, order_by="date_created", db_id=123, engine="postgres")
    base.update(kw)
    return Source(**base)


class FakeMb:
    def __init__(self, rows=None):
        self.rows, self.sql = rows or [], []

    def query(self, db, sql):
        self.sql.append((db, sql))
        return self.rows


def accounts(kind, value):
    assert kind in ("account", "reference", "mobile")
    return [value, value.lstrip("0")] if kind == "account" else [value]


def test_the_shipped_catalogue_loads_and_shows_no_personal_column():
    sources = load_catalogue()
    assert "zipit_middleware" in sources and "intellect_api_errors" in sources
    for src in sources.values():
        for column in src.columns:
            assert not personal({"name": column}) or src.identifiers.get(column) == "mobile", (src.key, column)
        assert src.identifiers or src.open
    assert "pan" not in str(load_catalogue()["card_requests"].columns) and "token" not in sources["zesa_tokens"].columns


def test_a_catalogue_with_a_personal_column_is_refused(tmp_path):
    bad = {"sources": {"x": {"database": "D", "table": "public.t", "columns": ["account_number", "first_name"],
                             "identifiers": {"account_number": "account"}}}}
    path = tmp_path / "c.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(CatalogueError, match="first_name"):
        load_catalogue(path)
    bad["sources"]["x"]["columns"] = ["account_number", "x; DROP TABLE t"]
    path.write_text(json.dumps(bad))
    with pytest.raises(CatalogueError):
        load_catalogue(path)


def test_rows_matched_on_an_account_in_every_form_within_a_window():
    mb = FakeMb([{"DATE_CREATED": datetime(2026, 7, 1, 9, 0), "amount": "20", "status": "FAILED"}])
    dq = DataQuery(mb, {"zipit": source()})
    p = dq.prepare({"source": "zipit", "match_column": ["source_account"], "match_value": "086000013101001",
                    "since": "2026-07-01", "until": "2026-07-01", "where": [{"column": "status", "op": "!=",
                                                                              "value": "SUCCESS"}],
                    "columns": ["date_created", "amount", "status"], "limit": 5}, accounts, now=NOW)
    assert p.sql == ('SELECT "date_created", "amount", "status" FROM "public"."zipit" WHERE ("source_account" IN '
                     "('086000013101001', '86000013101001')) AND \"date_created\" >= TIMESTAMP '2026-07-01 00:00:00' "
                     "AND \"date_created\" < TIMESTAMP '2026-07-02 00:00:00' AND \"status\" <> 'SUCCESS' "
                     'ORDER BY "date_created" DESC LIMIT 6')
    out = dq.run(p)
    assert out["rows"] == [{"date_created": "2026-07-01T09:00:00", "amount": "20", "status": "FAILED"}]
    assert out["matched_on"] == {"columns": ["source_account"], "kind": "account"} and out["more_rows"] is False


def test_client_data_needs_a_match_and_identifiers_cannot_be_filtered_or_counted():
    dq = DataQuery(FakeMb(), {"zipit": source()})
    with pytest.raises(ValueError, match="holds client data"):
        dq.prepare({"source": "zipit"}, accounts, now=NOW)
    with pytest.raises(ValueError, match="identifier"):
        dq.prepare({"source": "zipit", "match_column": ["rrn"], "match_value": "601234567890",
                    "where": [{"column": "source_account", "op": "=", "value": "1"}]}, accounts, now=NOW)
    with pytest.raises(ValueError, match="match_value"):
        dq.prepare({"source": "zipit", "match_column": ["rrn"], "match_value": "601234567890",
                    "where": [{"column": "status", "op": "=", "value": "86000099999999"}]}, accounts, now=NOW)
    with pytest.raises(ValueError, match="cannot count by source_account"):
        dq.prepare({"source": "zipit", "mode": "count", "count_by": ["source_account"]}, accounts, now=NOW)
    with pytest.raises(ValueError, match="one kind"):
        dq.prepare({"source": "zipit", "match_column": ["rrn", "source_account"], "match_value": "1"}, accounts, now=NOW)
    with pytest.raises(ValueError, match="not a column"):
        dq.prepare({"source": "zipit", "match_column": ["rrn"], "match_value": "601234567890",
                    "columns": ["first_name"]}, accounts, now=NOW)


def test_counts_without_a_match_are_grouped_and_kept_to_two_days():
    dq = DataQuery(FakeMb(), {"zipit": source()})
    p = dq.prepare({"source": "zipit", "mode": "count", "count_by": ["status", "error_code"]}, accounts, now=NOW)
    assert p.sql.startswith('SELECT "status", "error_code", COUNT(*) AS n, MIN("date_created") AS first_seen, '
                            'MAX("date_created") AS last_seen FROM "public"."zipit" WHERE "date_created" >= '
                            "TIMESTAMP '2026-07-02 00:00:00'")
    assert 'GROUP BY "status", "error_code" ORDER BY n DESC LIMIT 11' in p.sql
    with pytest.raises(ValueError, match="at most 2 day"):
        dq.prepare({"source": "zipit", "mode": "count", "since": "2026-06-20"}, accounts, now=NOW)


def test_the_window_never_reaches_past_the_audit_day_or_the_tables_limit():
    dq = DataQuery(FakeMb(), {"zipit": source(max_days=7)})
    p = dq.prepare({"source": "zipit", "match_column": "rrn", "match_value": "601234567890"}, accounts,
                   now=NOW, as_of=date(2026, 6, 10))
    assert p.until == datetime(2026, 6, 11) and p.since == datetime(2026, 6, 4)
    with pytest.raises(ValueError, match="at most 7 day"):
        dq.prepare({"source": "zipit", "match_column": "rrn", "match_value": "601234567890", "since": "2026-05-01"},
                   accounts, now=NOW)


def test_values_are_escaped_and_contains_cannot_carry_wildcards():
    dq = DataQuery(FakeMb(), {"zipit": source()})
    p = dq.prepare({"source": "zipit", "match_column": "rrn", "match_value": "601234567890", "where": [
        {"column": "error_code", "op": "contains", "value": "it's 100%_x"},
        {"column": "status", "op": "in", "value": ["A", "B'); DROP TABLE x; --"]}]}, accounts, now=NOW)
    assert "LOWER(CAST(\"error_code\" AS VARCHAR(4000))) LIKE '%it''s 100x%'" in p.sql
    assert "\"status\" IN ('A', 'B''); DROP TABLE x; --')" in p.sql


def test_each_engine_gets_its_own_quoting_and_row_limit():
    for engine, start, end in (("sqlserver", "SELECT TOP 11 [date_created]", "ORDER BY [date_created] DESC"),
                               ("mysql", "SELECT `date_created`", "ORDER BY `date_created` DESC LIMIT 11"),
                               ("oracle", 'SELECT * FROM (SELECT "date_created"', "DESC) WHERE ROWNUM <= 11")):
        dq = DataQuery(FakeMb(), {"zipit": source(engine=engine)})
        p = dq.prepare({"source": "zipit", "match_column": "rrn", "match_value": "601234567890",
                        "columns": ["date_created"]}, accounts, now=NOW)
        assert p.sql.startswith(start) and p.sql.endswith(end), p.sql
    sqlserver = DataQuery(FakeMb(), {"zipit": source(engine="sqlserver")}).prepare(
        {"source": "zipit", "match_column": "rrn", "match_value": "601234567890", "since": "2026-07-01"}, accounts, now=NOW)
    assert "[date_created] >= '2026-07-01T00:00:00'" in sqlserver.sql


def test_an_open_table_reads_without_a_match_and_a_desk_sees_only_its_sources():
    limits = Source(key="limits", database="connectorservices", table="public.limits_by_categ", about="Limits.",
                    columns=["category", "limit_amount"], open=True, desks=["failed_txn"], db_id=9)
    other = source(key="sms", desks=["sms_alerts"])
    dq = DataQuery(FakeMb([{"category": "A", "limit_amount": "500"}]), {"limits": limits, "sms": other}, desk="failed_txn")
    assert list(dq.sources) == ["limits"]
    assert dq.run(dq.prepare({"source": "limits"}, accounts, now=NOW))["rows"] == [{"category": "A", "limit_amount": "500"}]
    assert "limits [connectorservices]" in dq.tool_schema()["description"]


def test_computed_columns_come_from_the_catalogue_only():
    zesa = source(key="zesa", columns=["date_created", "status", "source_account"], identifiers={"source_account": "account"},
                  computed={"token_issued": "CASE WHEN COALESCE(token, '') <> '' THEN 'yes' ELSE 'no' END"})
    dq = DataQuery(FakeMb(), {"zesa": zesa})
    p = dq.prepare({"source": "zesa", "match_column": "source_account", "match_value": "86000013101001",
                    "columns": ["status", "token_issued"]}, accounts, now=NOW)
    assert "CASE WHEN COALESCE(token, '') <> '' THEN 'yes' ELSE 'no' END AS \"token_issued\"" in p.sql
    with pytest.raises(ValueError, match="token: not a column"):
        dq.prepare({"source": "zesa", "match_column": "source_account", "match_value": "86000013101001",
                    "columns": ["token"]}, accounts, now=NOW)


def test_a_probe_checks_the_tables_columns_without_reading_rows():
    mb = FakeMb()
    dq = DataQuery(mb, {"zipit": source()})
    assert dq.probe(dq.sources["zipit"]) is None
    assert mb.sql[0][1] == ('SELECT "date_created", "amount", "status", "error_code", "source_account", "rrn", '
                            '"mobile_number" FROM "public"."zipit" WHERE 1 = 0')

    class Broken(FakeMb):
        def query(self, db, sql):
            raise RuntimeError('column "error_code" does not exist')
    assert "error_code" in DataQuery(Broken(), {"zipit": source()}).probe(source())


def test_an_identifier_through_a_lookup_table_becomes_a_subquery():
    sms = Source(key="sms", database="SMS", table="public.sms_message", about="SMS log.",
                 columns=["created_at", "dispatch_status", "failure_reason"], identifiers={"recipient_mobile": "mobile"},
                 via={"recipient_mobile": {"column": "recipient_id", "table": "public.sms_recipient", "key": "id",
                                           "lookup": "mobile_number"}},
                 time_column="created_at", max_days=14, order_by="created_at", db_id=133)
    p = DataQuery(FakeMb(), {"sms": sms}).prepare(
        {"source": "sms", "match_column": "recipient_mobile", "match_value": "0772000111"},
        lambda kind, v: ["263772000111", "0772000111", "772000111"], now=NOW)
    assert ('("recipient_id" IN (SELECT "id" FROM "public"."sms_recipient" WHERE "mobile_number" IN '
            "('263772000111', '0772000111', '772000111')))") in p.sql


def test_counts_never_carry_numbers():
    mb = FakeMb([{"failure_reason": "Number 0772000111 barred", "n": 4}, {"failure_reason": "acc 86000013101001", "n": 1}])
    dq = DataQuery(mb, {"zipit": source(columns=["date_created", "failure_reason", "source_account"])})
    out = dq.run(dq.prepare({"source": "zipit", "mode": "count", "count_by": ["failure_reason"]}, accounts, now=NOW))
    assert [r["failure_reason"] for r in out["rows"]] == ["Number [number] barred", "acc [number]"]


def test_identifier_forms():
    from agentcore.dataquery import identifier_forms
    assert identifier_forms("account", "0086000013101001") == ["0086000013101001", "86000013101001"]
    assert identifier_forms("account", "21269522") == ["00000021269522", "21269522"]
    assert identifier_forms("mobile", "+263 77 200 0111") == ["263772000111", "0772000111", "772000111"]
    assert identifier_forms("mobile", "12345") == [] and identifier_forms("cif", "<NUMBER_1>") == []
    assert identifier_forms("reference", "ft-2601a") == ["FT2601A", "ft2601a"]
    assert identifier_forms("cif", "0191565") == ["0191565", "191565"]


def test_the_shipped_catalogue_gives_each_desk_its_own_sources():
    sources = load_catalogue()
    by_desk = {d: {k for k, s in sources.items() if d in s.desks} for d in ("failed_txn", "nmb_connect", "sms_alerts")}
    assert "zipit_middleware" in by_desk["failed_txn"] and "connect_profiles" in by_desk["nmb_connect"]
    assert by_desk["sms_alerts"] == {"sms_dispatch_messages", "sms_recipients"}
    assert "credential_value" not in sources["connect_credential_status"].columns
    assert not {"pan", "card_number", "last_eight", "masked_card_number"} & set(sources["connect_linked_cards"].columns)
