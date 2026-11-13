"""Streaming filter correctness and atomic safe export, exclusively synthetic evidence."""

import csv
import hashlib
import json
import threading
import tracemalloc
from dataclasses import asdict

import pytest

from username_finder import rovalid
from username_finder.rovalid_filters import ResultFilters, parse_filters


@pytest.mark.parametrize(
    "values,yes,no",
    [
        ({"length": "3"}, "Ab1", "abcd"),
        ({"length": "4"}, "Ab12", "abc"),
        ({"length": "5"}, "Ab123", "abcd"),
        ({"length": "6"}, "Ab1234", "abcde"),
        ({"length": "7+"}, "Ab12345", "abc123"),
        ({"length": "exact", "exact_length": 20}, "a" * 20, "a" * 19),
        ({"underscore": "yes"}, "Ab_c", "abcd"),
        ({"underscore": "no"}, "abcd", "ab_c"),
        ({"digits": "none"}, "abcd", "abc1"),
        ({"digits": "some"}, "abc1", "abcd"),
        ({"digits": "only"}, "1234", "123a"),
        ({"digit_count": "0"}, "abcd", "abc1"),
        ({"digit_count": "1"}, "abc1", "ab12"),
        ({"digit_count": "2"}, "ab12", "a123"),
        ({"digit_count": "3+"}, "a123", "ab12"),
        ({"repetition": "none"}, "abc1", "aBcA"),
        ({"repetition": "any"}, "abCa", "abc1"),
        ({"repetition": "adjacent"}, "abBC", "abca"),
        ({"repetition": "abab"}, "aBaB", "aaaa"),
        ({"repetition": "identical"}, "aAaA", "abab"),
        ({"start": "letter"}, "abc1", "1abc"),
        ({"start": "digit"}, "1abc", "abc1"),
        ({"end": "letter"}, "1abc", "abc1"),
        ({"end": "digit"}, "abc1", "1abc"),
        ({"prefix": "AB"}, "ab12", "cab1"),
        ({"suffix": "CD"}, "abCd", "cdab"),
        ({"text": "BC", "text_mode": "contains"}, "abcd", "abdc"),
        ({"text": "AB", "text_mode": "starts"}, "ab12", "cab1"),
        ({"text": "CD", "text_mode": "ends"}, "abCd", "cdab"),
    ],
)
def test_each_filter(values, yes, no):
    predicate = ResultFilters(**values)
    assert predicate.matches([yes, "Available", "source"])
    assert not predicate.matches([no, "Available", "source"])


@pytest.mark.parametrize("name", ["aaaa", "abcabc", "ababa", "abba", "ababab", "abc", "a", ""])
def test_abab_exact_pattern(name):
    assert not ResultFilters(repetition="abab").matches([name, "Unknown"])


def test_and_logic_and_classification_no_dedup_confusion():
    values = dict(
        length="4",
        underscore="no",
        digits="some",
        digit_count="2",
        start="letter",
        end="digit",
        prefix="a",
        suffix="1",
        repetition="abab",
        text="A1",
        text_mode="starts",
        classification="Available",
    )
    predicate = ResultFilters(**values)
    assert predicate.matches(["a1A1", "Available"])
    assert not predicate.matches(["a1A1", "Unknown"])
    assert not predicate.matches(["a1b1", "Available"])
    assert not ResultFilters(digits="none", digit_count="1").matches(["a1a1", "Available"])


@pytest.mark.parametrize(
    "values",
    [
        dict(length="2"),
        dict(exact_length=21),
        dict(exact_length=True),
        dict(repetition="duplicate"),
        dict(classification="Reserved"),
        dict(digits="bad"),
        dict(prefix=1),
        dict(text="x" * 257),
        dict(unknown=True),
        [],
        dict(underscore=None),
    ],
)
def test_invalid_filters_fail_closed(values):
    with pytest.raises(ValueError):
        parse_filters(values)


def dataset(tmp_path, count=1200):
    directory = tmp_path / "run"
    (directory / "results").mkdir(parents=True)
    hits = directory / "results/hits.txt"
    hits.write_text("".join(f"Test{i:04d}\n" for i in range(count)), encoding="utf8")
    (directory / "results/unresolved.txt").write_text("aBaB\n1234\n=CMD()\nUnicodeاسم\n", encoding="utf8")
    return directory


def test_pages_beyond_500_and_export_share_all_filters(tmp_path):
    directory = dataset(tmp_path)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (directory / "results").iterdir()}
    values = asdict(ResultFilters(length="7+", prefix="test", text="0", classification="Available"))
    first, total = rovalid.result_rows(directory, filters=values)
    assert len(first) == 500 and total > 500
    second, total2 = rovalid.result_rows(directory, filters=values, offset=500)
    assert second and total2 == total and second[0] != first[0]
    all_rows = []
    for offset in range(0, total, 500):
        all_rows.extend(rovalid.result_rows(directory, filters=values, offset=offset)[0])
    out = tmp_path / "matching.csv"
    assert rovalid.export_results(directory, out, filters=values) == total
    with out.open(encoding="utf-8-sig", newline="") as file:
        exported = list(csv.reader(file))
    assert [r[:3] for r in exported[1:]] == all_rows
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}
    assert rovalid.result_rows(directory, filters=dict(prefix="absent")) == ([], 0)
    # Repeated lines are source evidence; repetition refers to characters, never row deduplication.
    with (directory / "results/unresolved.txt").open("a", encoding="utf8") as file:
        file.write("aBaB\n")
    rows, total = rovalid.result_rows(directory, filters=dict(repetition="abab"))
    assert len(rows) == total == 2 and all(row[1] == "Unknown" for row in rows)


def test_export_formula_safety_and_atomic_failure_or_cancellation(tmp_path, monkeypatch):
    directory = dataset(tmp_path, 1)
    source = directory / "results/unresolved.txt"
    source.write_text("=SUM(1,2)\n+formula\n-formula\n@formula\n'quoted\n", encoding="utf8")
    before = source.read_bytes()
    out = tmp_path / "export.csv"
    rovalid.export_results(directory, out, classification="Unknown")
    with out.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert [json.loads(r["username"]) for r in rows] == source.read_text().splitlines()
    assert all(r["classification"] == "Unknown" for r in rows)
    original = out.read_bytes()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(rovalid.SearchCancelled):
        rovalid.export_results(directory, out, cancelled=cancel.is_set)
    assert out.read_bytes() == original
    original_row = rovalid.spreadsheet_row

    def fail(row, fields):
        if row[1] == "Unknown":
            raise OSError("synthetic write failure")
        return original_row(row, fields)

    monkeypatch.setattr(rovalid, "spreadsheet_row", fail)
    with pytest.raises(OSError):
        rovalid.export_results(directory, out)
    assert out.read_bytes() == original and source.read_bytes() == before
    assert not list(tmp_path.glob(".rovalid-export-*"))


@pytest.mark.parametrize(
    "kwargs", [dict(offset=-1), dict(limit=0), dict(limit=501), dict(offset=True), dict(limit=None)]
)
def test_invalid_pagination(tmp_path, kwargs):
    with pytest.raises(ValueError):
        rovalid.result_rows(tmp_path, **kwargs)


def test_large_stream_memory_is_bounded_and_rows_are_never_reclassified(tmp_path):
    directory = dataset(tmp_path, count=30000)
    source = directory / "results/hits.txt"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    tracemalloc.start()
    try:
        rows, total = rovalid.result_rows(directory, offset=29000, filters=dict(prefix="test"))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(rows) == 500 and total == 30000 and all(row[1] == "Available" for row in rows)
    assert peak < 2 * 1024 * 1024
    assert before == hashlib.sha256(source.read_bytes()).hexdigest()


def test_source_change_during_export_fails_without_publishing_partial_file(tmp_path, monkeypatch):
    directory = dataset(tmp_path, count=5)
    target = tmp_path / "existing.csv"
    target.write_bytes(b"previous complete export")
    original = rovalid.spreadsheet_row
    changed = False

    def change(row, fields):
        nonlocal changed
        if not changed:
            changed = True
            with (directory / "results/hits.txt").open("a", encoding="utf8") as file:
                file.write("NewName\n")
        return original(row, fields)

    monkeypatch.setattr(rovalid, "spreadsheet_row", change)
    with pytest.raises(ValueError, match="changed"):
        rovalid.export_results(directory, target)
    assert target.read_bytes() == b"previous complete export"
    assert not list(tmp_path.glob(".rovalid-export-*"))
