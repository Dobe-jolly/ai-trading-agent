"""tools/metabase_names.py --columns: names and types from the catalogue, never rows. All data invented."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("metabase_names", ROOT / "tools" / "metabase_names.py")
names = importlib.util.module_from_spec(spec)
spec.loader.exec_module(names)


class FakeMb:
    def __init__(self, rows=None, meta=None):
        self.rows, self.meta, self.sql = rows or [], meta or {}, []

    def query(self, db_id, sql):
        self.sql.append(sql)
        return self.rows

    def _request(self, method, path, payload=None):
        return self.meta


def test_the_column_query_filters_by_table_and_name_and_quotes_what_it_is_given():
    sql = names.columns_sql("sqlserver", ["post_tran", "post_tran_leg"], ["rsp", "o'brien"])
    assert "FROM information_schema.columns" in sql
    assert "LOWER(TABLE_NAME) IN ('post_tran', 'post_tran_leg')" in sql
    assert "LOWER(COLUMN_NAME) LIKE '%rsp%' OR LOWER(COLUMN_NAME) LIKE '%o''brien%'" in sql
    assert sql.endswith("ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION")
    oracle = names.columns_sql("oracle", ["POST_TRAN"], [])
    assert "FROM all_tab_columns" in oracle and "LOWER(table_name) IN ('post_tran')" in oracle


def test_columns_come_from_the_catalogue_or_from_what_metabase_synced():
    mb = FakeMb(rows=[{"TABLE_SCHEMA": "dbo", "TABLE_NAME": "post_tran", "COLUMN_NAME": "rsp_code_rsp",
                       "DATA_TYPE": "char", "MAX_LEN": 2}])
    assert names.columns(mb, {"id": 3, "engine": "sqlserver"}, ["post_tran"], []) == [
        ("dbo.post_tran", "rsp_code_rsp", "char", "2")]
    synced = FakeMb(meta={"tables": [
        {"schema": "dbo", "name": "post_tran", "fields": [{"name": "rsp_code_rsp", "database_type": "char"},
                                                          {"name": "tran_nr", "database_type": "bigint"}]},
        {"schema": "dbo", "name": "other", "fields": [{"name": "rsp_x", "database_type": "int"}]}]})
    assert names.columns(synced, {"id": 3, "engine": "sqlserver"}, ["post_tran"], ["rsp"]) == [
        ("dbo.post_tran", "rsp_code_rsp", "char", "")]


def test_the_catalogue_lists_every_database_and_marks_personal_columns(tmp_path):
    mb = FakeMb(meta={"tables": [
        {"schema": "public", "name": "ecocash_registration", "estimated_row_count": 301832, "fields": [
            {"name": "account_id", "database_type": "varchar", "position": 0},
            {"name": "first_name", "database_type": "varchar", "position": 1},
            {"name": "msisdn", "database_type": "varchar", "position": 2},
            {"name": "holder", "database_type": "text", "semantic_type": "type/Name", "position": 3},
            {"name": "service_type", "database_type": "varchar", "position": 4}]}]})
    said = []
    names.catalogue(mb, [{"id": 16, "name": "Ecocash Reg", "engine": "postgres"},
                         {"id": 1, "name": "Sample", "engine": "h2"}], tmp_path, said.append)
    text = (tmp_path / "16-ecocash_reg.txt").read_text()
    line = [x for x in text.splitlines() if x.startswith("public.ecocash_registration")][0]
    assert "[rows ~301832]" in line and "account_id varchar," in line and "service_type varchar" in line
    assert "first_name varchar [personal]" in line and "msisdn varchar [personal]" in line
    assert "holder text [personal]" in line and "account_id varchar [personal]" not in line
    index = (tmp_path / "index.txt").read_text()
    assert "Ecocash Reg  [postgres]  1  5  3  16-ecocash_reg.txt" in index and "Sample" not in index
    assert mb.sql == []  # names and types from Metabase's metadata: nothing is queried


def test_personal_columns_by_name():
    for name in ("ACNTS_AC_NAME1", "CUST_NAME", "national_id", "ID_NO", "date_of_birth", "MOBILE_NO", "email",
                 "ACNTS_AC_ADDR1", "imei", "otp", "password", "username", "name", "pin", "pan", "new_pan",
                 "card_number"):
        assert names.personal({"name": name}), name
    for name in ("ACNTS_ATM_OPERN", "rsp_code_rsp", "tran_amount_req", "service_type", "currency_code", "ERR_DESC",
                 "table_name", "ACNTS_CLIENT_NUM", "account_id", "merchant_name", "cancel_flag", "footprint"):
        assert not names.personal({"name": name}), name


def test_a_database_whose_metadata_times_out_still_gets_its_table_names(tmp_path):
    class SlowMb(FakeMb):
        def _request(self, method, path, payload=None):
            raise names.MetabaseError("ReadTimeout: timed out")

    mb = SlowMb(rows=[{"TABLE_SCHEMA": "NMBPRODIDC", "TABLE_NAME": "ACNTS"},
                      {"TABLE_SCHEMA": "NMBPRODIDC", "TABLE_NAME": "IACLINK"}])
    names.catalogue(mb, [{"id": 81, "name": "STANDBY DB", "engine": "oracle"}], tmp_path, lambda _: None)
    assert "NMBPRODIDC.ACNTS" in (tmp_path / "81-standby_db.txt").read_text()
    assert "STANDBY DB  [oracle]  2  -  -  81-standby_db.txt" in (tmp_path / "index.txt").read_text()
