"""tools/export_post_tran_leg.py --relabel: new names applied to finished files from the codes already in them, nothing else
changed. No database. All data invented."""
from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location("export_post_tran_leg", ROOT / "tools" / "export_post_tran_leg.py")
ex = importlib.util.module_from_spec(spec)
sys.modules["export_post_tran_leg"] = ex  # a dataclass needs its module registered
spec.loader.exec_module(ex)

NAME = "post_tran_leg_2026-01_USD.txt"


def row_of(tran_type, message_type, source="NmbVNodeSrc", sink="NmbZswAcq"):
    # the account value holds a backslash on purpose: relabelling must not touch the other columns
    return ["10", tran_type, "", "00", message_type, "00", sink, source, "000001", "840", "500", "*", "A\\B", "B",
            "2026-01-02 00:00:00.000"]


def write_txt(folder, rows, name=NAME):
    s = ex.Spec()
    lines = ["\t".join(s.header)] + ["\t".join(s.out_row(r)) for r in rows]
    (folder / name).write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")


def test_relabel_applies_new_names_to_finished_files_and_changes_nothing_else(tmp_path):
    rows = [row_of("32", "0100"), row_of("50", "0200", "NmbZswIss", "NmbIntellect"), row_of("39", "0100", "NmbAtmSrc", "NmbIntellect")]
    write_txt(tmp_path, rows)
    before = (tmp_path / NAME).read_text(encoding="utf-8").splitlines()
    assert before[1].split("\t")[2] == "Type 32 (unnamed)"
    (tmp_path / "manifest.txt").write_text(f"file\trows\tbytes\r\n{NAME}\t3\t1\r\n", encoding="utf-8")
    names = {"tran_types": {"32": "Zipit Validation"}}
    assert ex.relabel_folder(tmp_path, names) == 0
    after = (tmp_path / NAME).read_text(encoding="utf-8").splitlines()
    assert after[1].split("\t")[2] == "Zipit Validation" and after[3].split("\t")[2] == "Type 39 (unnamed)"  # 39 is still not guessed
    assert after[2] == before[2]  # a row whose names did not change is byte-identical (including its backslash)
    changed_cols = [i for i, (x, y) in enumerate(zip(before[1].split("\t"), after[1].split("\t"))) if x != y]
    assert changed_cols == [2]  # only the transaction_type column of that row changed
    man = (tmp_path / "manifest.txt").read_text(encoding="utf-8").splitlines()
    assert man[1].split("\t")[:2] == [NAME, "3"] and int(man[1].split("\t")[2]) == (tmp_path / NAME).stat().st_size
    assert not list(tmp_path.glob("*.relabel"))
    assert ex.relabel_folder(tmp_path, names) == 0  # again: nothing more to change
    assert (tmp_path / NAME).read_text(encoding="utf-8").splitlines() == after
    assert (tmp_path / NAME).read_bytes().endswith(b"\r\n")  # the same line endings


def test_relabel_handles_csv_and_leaves_a_file_it_did_not_write_alone(tmp_path):
    s = ex.Spec()
    with open(tmp_path / "post_tran_leg_2026-01_USD.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(s.header)
        w.writerow(s.out_row(row_of("32", "0100")))
    rows, changed = ex.relabel_file(tmp_path / "post_tran_leg_2026-01_USD.csv", {"tran_types": {"32": "Zipit Validation"}})
    assert (rows, changed) == (1, 1)
    out = list(csv.reader(open(tmp_path / "post_tran_leg_2026-01_USD.csv", encoding="utf-8", newline="")))
    assert out[1][2] == "Zipit Validation" and out[1][s.header.index("from_account_id")] == "A\\B"
    other = tmp_path / "post_tran_leg_2026-02_USD.txt"
    other.write_text("a\tb\r\n1\t2\r\n", encoding="utf-8", newline="")
    with pytest.raises(ValueError, match="not a file this tool wrote"):
        ex.relabel_file(other, None)
    assert other.read_bytes() == b"a\tb\r\n1\t2\r\n"  # left exactly as it was
    assert not list(tmp_path.glob("*.relabel"))


def test_relabel_on_an_empty_folder_says_so_and_main_wires_it_up(tmp_path, capsys):
    assert ex.relabel_folder(tmp_path, None) == 1 and "no post_tran_leg_* files" in capsys.readouterr().out
    write_txt(tmp_path, [row_of("32", "0100")])
    names = tmp_path / "names.json"
    names.write_text('{"tran_types": {"32": "Zipit Validation"}}', encoding="utf-8")
    assert ex.main(["--relabel", str(tmp_path), "--names", str(names)]) == 0
    assert (tmp_path / NAME).read_text(encoding="utf-8").splitlines()[1].split("\t")[2] == "Zipit Validation"
    assert "relabelled 1 of 1 rows in 1 files" in capsys.readouterr().out
