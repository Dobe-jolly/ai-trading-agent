"""tools/export_post_tran_leg.py: the name columns, the column/leg options, the day windows, the per-day check with
retries, and the .part -> final file handling. No database: Metabase is faked. All data invented."""
from __future__ import annotations

import csv
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location("export_post_tran_leg", ROOT / "tools" / "export_post_tran_leg.py")
ex = importlib.util.module_from_spec(spec)
sys.modules["export_post_tran_leg"] = ex  # a dataclass needs its module registered
spec.loader.exec_module(ex)


# ---- names -------------------------------------------------------------------------------------------------------
def test_message_types_get_their_iso_names_and_unknown_ones_say_so():
    assert ex.message_type_name("0200") == "Financial transaction request"
    assert ex.message_type_name("0100") == "Authorization request"
    assert ex.message_type_name("0220") == "Financial transaction advice"
    assert ex.message_type_name("0420") == "Reversal advice"
    assert ex.message_type_name("0600") == "Administrative request"
    assert ex.message_type_name("420") == "Reversal advice"  # stored without the leading zero
    assert ex.message_type_name("0999") == "Message type 0999 (unnamed)"
    assert ex.message_type_name("") == "Message type (none) (unnamed)"


def test_the_banks_own_name_wins_then_the_plain_type_name_then_unnamed():
    assert ex.transaction_type("50", "0200", "NmbZswIss", "NmbIntellect") == "Zipit Send"
    assert ex.transaction_type("42", "0200", "NmbVNodeSrc", "NmbZswAcq") == "Zipit Send"
    assert ex.transaction_type("01", "0200", "NmbZswIss", "NmbIntellect") == "Withdrawal - Us on Them"
    assert ex.transaction_type("00", "0420", "EFTHostedSrc", "NmbIntellect") == "POS Purchase Reversal - Us on Us"
    assert ex.transaction_type("1", "200", "nmbatmsrc", "NMBINTELLECT") == "Withdrawal - Us on Us"  # case and zeros do not matter
    assert ex.transaction_type("00", "0200", "MatsimbaSrc", None) == "Purchase"  # the pair is not in the bank's map
    assert ex.transaction_type("31", "0200", "NmbEcocashSc", "NmbIntellect") == "Balance / available funds inquiry"
    assert ex.transaction_type("32", "0100", "NmbVNodeSrc", "NmbZswAcq") == "Type 32 (unnamed)"  # nobody has named it: not guessed
    assert ex.transaction_type("", "", "", "") == "Type (none) (unnamed)"


def test_a_names_file_names_a_code_nobody_has_named_but_never_overrides_the_banks_own_map():
    names = {"tran_types": {"32": "Zipit Validation", "00": "Goods and services"}, "message_types": {"0600": "Pin change request"}}
    assert ex.transaction_type("32", "0100", "NmbVNodeSrc", "NmbZswAcq", names) == "Zipit Validation"
    assert ex.transaction_type("39", "0100", "NmbVNodeSrc", None, names) == "Type 39 (unnamed)"  # still not guessed
    assert ex.transaction_type("00", "0200", "MatsimbaSrc", None, names) == "Goods and services"  # beats the plain name
    assert ex.transaction_type("50", "0200", "NmbZswIss", "NmbIntellect", names) == "Zipit Send"  # the bank's exact map wins
    assert ex.message_type_name("0600", names) == "Pin change request" and ex.message_type_name("0200", names).startswith("Financial")
    assert ex.Spec(names=names).out_row(["1", "32", "", "00", "0100", "00", "NmbZswAcq", "NmbVNodeSrc", "1", "840", "5", "*", "A",
                                         "B", "2026-01-02 00:00:00.000"])[2] == "Zipit Validation"


# ---- columns and legs ----------------------------------------------------------------------------------------------
def test_the_default_layout_is_the_requested_columns_with_the_two_names_inserted():
    s = ex.Spec()
    assert s.header == ["tran_nr", "tran_type", "transaction_type", "Authoriser_id", "State_code", "message_type",
                        "message_type_name", "rsp_code_rsp", "sink_node_name", "source_node_name", "STAN",
                        "USD_currency_code", "tran_amount_req", "pan", "from_account_id", "to_account_id", "datetime_req"]
    assert "settle_amount_req" not in s.select and "l.tran_amount_req" in s.select
    assert "tran_postilion_originated" not in s.select and "retrieval_reference_nr" not in s.select


def test_rrn_and_the_leg_flag_are_added_where_asked_and_client_leg_filters_both_queries():
    s = ex.Spec("client", rrn=True, leg_flag=True)
    assert s.header[s.header.index("tran_amount_req") + 1:s.header.index("pan")] == ["RRN", "tran_postilion_originated"]
    assert "l.retrieval_reference_nr AS RRN" in s.select and "l.tran_postilion_originated AS tran_postilion_originated" in s.select
    assert s.select.count("l.tran_postilion_originated = '0'") == 1 and s.count.count("l.tran_postilion_originated = '0'") == 1
    allrows = ex.Spec("all", rrn=True)
    assert "= '0'" not in allrows.select and "= '0'" not in allrows.count and "tran_postilion_originated" not in allrows.header


def test_out_row_inserts_the_names_after_their_codes_and_keeps_everything_else_in_place():
    s = ex.Spec("client", rrn=True)
    db_row = ["10042460", "50", "", "61", "0200", "61", "NmbIntellect", "NmbZswIss", "730529", "840", "7300", "612345678901",
              "*", "85729663102001", "0771152966", "2026-01-01 00:01:01.010"]
    assert len(db_row) == len(s.sql_header)
    out = s.out_row(db_row)
    assert len(out) == len(s.header) == len(db_row) + 2
    assert dict(zip(s.header, out))["transaction_type"] == "Zipit Send"
    assert dict(zip(s.header, out))["message_type_name"] == "Financial transaction request"
    assert [v for h, v in zip(s.header, out) if h not in ("transaction_type", "message_type_name")] == db_row


# ---- day windows ----------------------------------------------------------------------------------------------------
def test_day_windows_are_contiguous_clipped_to_the_range_and_keep_the_last_instant():
    windows = list(ex.day_windows(datetime(2026, 1, 31, 0, 1, 0), datetime(2026, 2, 2, 23, 59, 59)))
    assert [w[0] for w in windows] == ["2026-01-31", "2026-02-01", "2026-02-02"]
    assert [w[2] for w in windows] == ["2026-01", "2026-02", "2026-02"]
    assert "l.datetime_req >= '2026-01-31 00:01:00.000' AND l.datetime_req < '2026-02-01 00:00:00.000'" == windows[0][1]
    assert "l.datetime_req >= '2026-02-01 00:00:00.000' AND l.datetime_req < '2026-02-02 00:00:00.000'" == windows[1][1]
    assert windows[2][1].endswith("l.datetime_req <= '2026-02-02 23:59:59.000'")  # the end is included


# ---- the per-day check and retries -----------------------------------------------------------------------------------
class FakeMb:
    def __init__(self, counts):
        self.counts = counts
        self.queries = 0

    def query(self, db, sql):
        self.queries += 1
        return [{"cur": c, "n": n} for c, n in self.counts.items()]


def fake_rows(spec, usd=2, zwg=1):
    out = []
    for cur, n in (("840", usd), ("924", zwg)):
        for i in range(n):
            row = ["100", "50", "", "00", "0200", "00", "NmbIntellect", "NmbZswIss", "000001", cur, "500", "*", "A", "B",
                   f"2026-01-02 00:00:0{i}.000"]
            out.append(row)
    return out


def patch_stream(monkeypatch, spec, attempts):
    """stream_rows answers from a list of outcomes: rows, or an exception to raise."""
    calls = iter(attempts)

    def fake(mb, db, sql, header):
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        yield from outcome

    monkeypatch.setattr(ex, "stream_rows", fake)


def test_a_day_that_matches_the_database_count_is_returned_decorated_by_currency(monkeypatch):
    s = ex.Spec()
    patch_stream(monkeypatch, s, [fake_rows(s)])
    got = ex.fetch_day(FakeMb({"840": 2, "924": 1}), 5, s, "w", "2026-01-02", sleep=lambda x: None)
    assert [len(got["840"]), len(got["924"])] == [2, 1]
    assert got["840"][0][2] == "Zipit Send" and got["840"][0][6] == "Financial transaction request"  # names in place


def test_a_failed_download_is_retried_from_scratch_and_never_duplicates_rows(monkeypatch, capsys):
    s = ex.Spec()
    waits = []
    patch_stream(monkeypatch, s, [TimeoutError("timed out"), RuntimeError("Metabase answered 404"), fake_rows(s)])
    got = ex.fetch_day(FakeMb({"840": 2, "924": 1}), 5, s, "w", "2026-01-02", sleep=waits.append)
    assert [len(got["840"]), len(got["924"])] == [2, 1] and waits == [5, 15]
    assert "attempt 1 failed" in capsys.readouterr().out


def test_a_short_download_is_rejected_and_gives_up_with_the_day_named(monkeypatch):
    s = ex.Spec()
    patch_stream(monkeypatch, s, [fake_rows(s, usd=1)] * (len(ex.RETRY_WAITS) + 1))  # one USD row fewer than the database counts
    with pytest.raises(RuntimeError, match=r"2026-01-02: gave up after 6 attempts.*1 USD rows downloaded, the database counts 2"):
        ex.fetch_day(FakeMb({"840": 2, "924": 1}), 5, s, "w", "2026-01-02", sleep=lambda x: None)


# ---- files -----------------------------------------------------------------------------------------------------------
def test_files_are_parts_until_finished_and_csv_and_txt_carry_the_same_rows(tmp_path):
    s = ex.Spec()
    o = ex.Out(tmp_path, "2026-01", "840", s.header, ("csv", "txt"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["post_tran_leg_2026-01_USD.csv.part", "post_tran_leg_2026-01_USD.txt.part"]
    row = s.out_row(fake_rows(s)[0])
    row[3] = "has\ttab"  # a tab inside a value must not split a TXT column
    o.write(row)
    o.write(s.out_row(fake_rows(s)[1]))
    sizes = o.finish()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["post_tran_leg_2026-01_USD.csv", "post_tran_leg_2026-01_USD.txt"]
    assert [n for n, _ in sizes] == ["post_tran_leg_2026-01_USD.csv", "post_tran_leg_2026-01_USD.txt"] and o.rows == 2
    c = list(csv.reader(open(tmp_path / "post_tran_leg_2026-01_USD.csv", encoding="utf-8", newline="")))
    t = [ln.rstrip("\r\n").split("\t") for ln in open(tmp_path / "post_tran_leg_2026-01_USD.txt", encoding="utf-8", newline="\n")]
    assert c[0] == t[0] == s.header and len(c) == 3 and {len(r) for r in c} == {len(s.header)}
    assert t[1][3] == "has tab" and c[1][3] == "has\ttab" and c[2] == t[2]


def run_main(monkeypatch, tmp_path, *extra):
    """main() against a fake Metabase: fetch_day answers one day with two USD rows and one ZWG row."""
    calls = []

    class Client:
        def __init__(self, *a, **k):
            pass

        def resolve(self, name):
            return 5

    def fake_fetch(mb, db, spec, window, label, sleep=None):
        calls.append(label)
        rows = [spec.out_row(["10", "50", "", "00", "0200", "00", "NmbIntellect", "NmbZswIss", "000001", c, "500", "*", "A", "B",
                              "2026-01-02 00:00:00.000"]) for c in ("840", "840", "924")]
        return {"840": rows[:2], "924": rows[2:]}

    monkeypatch.setattr(ex, "MetabaseClient", Client)
    monkeypatch.setattr(ex, "fetch_day", fake_fetch)
    monkeypatch.setenv("METABASE_HOST", "http://localhost")
    monkeypatch.setenv("METABASE_API_KEY", "x")
    code = ex.main(["--out", str(tmp_path), "--start", "2026-01-02 00:00:00", "--end", "2026-01-02 23:59:59",
                    "--env-file", str(tmp_path / "no.env"), *extra])
    return code, calls


def test_the_default_export_is_txt_only_with_no_csv_anywhere_not_even_the_manifest(monkeypatch, tmp_path):
    code, calls = run_main(monkeypatch, tmp_path)
    assert code == 0 and calls == ["2026-01-02"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["manifest.txt", "post_tran_leg_2026-01_USD.txt",
                                                          "post_tran_leg_2026-01_ZWG.txt"]
    lines = (tmp_path / "post_tran_leg_2026-01_USD.txt").read_text(encoding="utf-8").splitlines()
    assert lines[0].split("	") == ex.Spec().header and len(lines) == 3 and lines[1].split("	")[2] == "Zipit Send"
    manifest = (tmp_path / "manifest.txt").read_text(encoding="utf-8").splitlines()
    assert manifest[0] == "file	rows	bytes" and manifest[1].startswith("post_tran_leg_2026-01_USD.txt	2	")
    assert not list(tmp_path.glob("*.csv")) and not list(tmp_path.glob("*.part"))


def test_resume_reads_the_txt_manifest_and_skips_what_is_finished(monkeypatch, tmp_path, capsys):
    run_main(monkeypatch, tmp_path)
    code, calls = run_main(monkeypatch, tmp_path, "--resume")
    assert code == 0 and calls == [] and "2026-01: already complete, skipped" in capsys.readouterr().out


def test_csv_and_both_are_still_available_when_asked_for(monkeypatch, tmp_path):
    run_main(monkeypatch, tmp_path, "--formats", "both")
    assert sorted(p.suffix for p in tmp_path.iterdir() if p.name.startswith("post_tran_leg")) == [".csv", ".csv", ".txt", ".txt"]


def run_range(monkeypatch, tmp_path, start, end, *extra, fail_month=None, delay=0.0):
    """main() over a range of days with a fake database; optionally one month always fails."""
    import time as _time
    seen = []

    class Client:
        def __init__(self, *a, **k):
            pass

        def resolve(self, name):
            return 5

    def fake_fetch(mb, db, spec, window, label, sleep=None):
        seen.append(label)
        if delay:
            _time.sleep(delay)
        if fail_month and label.startswith(fail_month):
            raise RuntimeError(f"{label}: gave up after 6 attempts: timed out")
        row = lambda c: spec.out_row(["10", "50", "", "00", "0200", "00", "NmbIntellect", "NmbZswIss", "000001", c, "500", "*", "A",
                                      "B", f"{label} 00:00:00.000"])
        return {"840": [row("840")], "924": [row("924"), row("924")]}

    monkeypatch.setattr(ex, "MetabaseClient", Client)
    monkeypatch.setattr(ex, "fetch_day", fake_fetch)
    monkeypatch.setenv("METABASE_HOST", "http://localhost")
    monkeypatch.setenv("METABASE_API_KEY", "x")
    code = ex.main(["--out", str(tmp_path), "--start", start, "--end", end, "--env-file", str(tmp_path / "no.env"), *extra])
    return code, seen


def manifest_rows(folder):
    lines = (folder / "manifest.txt").read_text(encoding="utf-8").splitlines()[1:]
    return {ln.split("	")[0]: int(ln.split("	")[1]) for ln in lines}


def test_workers_download_months_at_the_same_time_into_one_complete_manifest(monkeypatch, tmp_path):
    code, seen = run_range(monkeypatch, tmp_path, "2026-01-30 00:00:00", "2026-04-02 23:59:59", "--workers", "4", delay=0.01)
    assert code == 0
    rows = manifest_rows(tmp_path)
    assert sorted(rows) == [f"post_tran_leg_2026-0{m}_{c}.txt" for m in (1, 2, 3, 4) for c in ("USD", "ZWG")]
    # January 30-31 (2 days), February (28), March (31), April 1-2 (2): USD one row a day, ZWG two
    assert rows["post_tran_leg_2026-01_USD.txt"] == 2 and rows["post_tran_leg_2026-02_ZWG.txt"] == 56
    assert rows["post_tran_leg_2026-03_USD.txt"] == 31 and rows["post_tran_leg_2026-04_ZWG.txt"] == 4
    assert len(seen) == 63 and len(set(seen)) == 63  # every day fetched once, none twice
    assert not list(tmp_path.glob("*.part")) and not list(tmp_path.glob("*.csv"))


def test_parallel_and_sequential_runs_write_the_same_files(monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    run_range(monkeypatch, a, "2026-01-30 00:00:00", "2026-03-02 23:59:59", "--workers", "3")
    run_range(monkeypatch, b, "2026-01-30 00:00:00", "2026-03-02 23:59:59", "--workers", "1")
    for f in sorted(p.name for p in a.iterdir() if p.name != "manifest.txt"):
        assert (a / f).read_bytes() == (b / f).read_bytes(), f


def test_one_failing_month_does_not_stop_the_others_and_is_reported_and_redone_on_resume(monkeypatch, tmp_path, capsys):
    code, _ = run_range(monkeypatch, tmp_path, "2026-01-30 00:00:00", "2026-03-02 23:59:59", "--workers", "3", fail_month="2026-02")
    out = capsys.readouterr().out
    assert code == 1 and "FAILED 2026-02" in out and "finished with 1 failed month(s): 2026-02" in out
    assert sorted(manifest_rows(tmp_path)) == ["post_tran_leg_2026-01_USD.txt", "post_tran_leg_2026-01_ZWG.txt",
                                               "post_tran_leg_2026-03_USD.txt", "post_tran_leg_2026-03_ZWG.txt"]
    assert not list(tmp_path.glob("*.part")) and not list(tmp_path.glob("*2026-02*"))  # nothing half-written is left
    code, seen = run_range(monkeypatch, tmp_path, "2026-01-30 00:00:00", "2026-03-02 23:59:59", "--workers", "3", "--resume")
    assert code == 0 and len(seen) == 28 and all(d.startswith("2026-02") for d in seen)  # only February is redone
    assert len(manifest_rows(tmp_path)) == 6


def test_a_low_disk_stops_every_worker_and_leaves_no_part_files(monkeypatch, tmp_path, capsys):
    code, _ = run_range(monkeypatch, tmp_path, "2026-01-30 00:00:00", "2026-03-02 23:59:59", "--workers", "3",
                        "--min-free-gb", "999999999")
    assert code == 2 and "STOPPED: only" in capsys.readouterr().out and not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("post_tran_leg_*"))
