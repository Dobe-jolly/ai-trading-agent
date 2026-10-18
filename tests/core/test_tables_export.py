"""tools/export_tables.py: month windows, currency labels, the count check and the files. No database; data invented."""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location("export_tables", ROOT / "tools" / "export_tables.py")
et = importlib.util.module_from_spec(spec)
sys.modules["export_tables"] = et
spec.loader.exec_module(et)


def test_month_windows_cover_the_range_exactly():
    w = list(et.month_windows(date(2025, 12, 1), date(2026, 2, 15)))
    assert w == [("2025-12", "2025-12-01", "2026-01-01"), ("2026-01", "2026-01-01", "2026-02-01"),
                 ("2026-02", "2026-02-01", "2026-02-16")]


def test_currency_label_names_zig_as_zwg_and_blank_as_none():
    assert [et.currency_label(x) for x in ("USD", "zig", "", None, " ZAR ")] == ["USD", "ZWG", "NONE", "NONE", "ZAR"]


@pytest.mark.parametrize("name", sorted(et.SOURCES))
def test_sources_have_columns_without_surprises(name):
    s = et.Source(name)
    assert s.columns[s.cur_at] == "currency" and s.date_col in s.columns and s.order_col in s.columns
    assert "ORDER BY" in s.select("2026-01-01", "2026-02-01")


class FakeMb:
    def __init__(self, counts):
        self.counts = counts

    def query(self, db, sql):
        return [{"cur": k, "n": v} for k, v in self.counts.items()]


def test_fetch_month_groups_by_currency_and_retries_a_short_download(monkeypatch):
    src = et.Source("zimpost")
    row = lambda cur: [cur if c == "currency" else "x" for c in src.columns]  # noqa: E731
    calls = []

    def stream(mb, db, sql, header):
        calls.append(1)
        yield row("USD")
        if len(calls) > 1:
            yield row("ZIG")

    monkeypatch.setattr(et.ex, "stream_rows", stream)
    got = et.fetch_month(FakeMb({"USD": 1, "ZIG": 1}), 1, src, "2026-01-01", "2026-02-01", "t", sleep=lambda s: None)
    assert len(calls) == 2 and sorted(got) == ["USD", "ZWG"]


def test_fetch_month_gives_up_when_counts_never_agree(monkeypatch):
    src = et.Source("vbs")
    monkeypatch.setattr(et.ex, "stream_rows", lambda *a: iter([]))
    with pytest.raises(RuntimeError, match="gave up"):
        et.fetch_month(FakeMb({"USD": 3}), 1, src, "2026-01-01", "2026-02-01", "t", sleep=lambda s: None)


def test_write_file_is_tab_separated_and_flattens_whitespace(tmp_path):
    p = tmp_path / "a.txt"
    et.write_file(p, ["a", "b"], [["1", "x\ty\nz"]])
    assert p.read_bytes() == b"a\tb\r\n1\tx y z\r\n" and not list(tmp_path.glob("*.part"))
