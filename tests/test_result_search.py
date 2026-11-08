import csv
import json
import threading
import time
from dataclasses import replace

import pytest

from username_finder import result_search, x_protocol
from username_finder.result_search import (
    SYMBOL_RULES,
    ResultFilter,
    ResultSearch,
    SearchCancelled,
    export_fields,
)
from username_finder.roblox import syntax_error
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store
from username_finder.x_storage import XStore


def record(db, platform, name, classification="Available", *, previous=None, at="2026-11-08T12:00:00+00:00"):
    evidence = json.dumps({"api_reason": "fixture", "http_status": 200, "response_data": "fixture"})
    values = (name, name.casefold(), at, classification, evidence, previous, int(bool(previous)))
    if platform == "Roblox":
        row = db.execute(
            "INSERT INTO roblox_checks(username,username_key,checked_at,classification,evidence,"
            "previous_classification,changed,source,source_key,fingerprint,completed) "
            "VALUES(?,?,?,?,?,?,?,'fixture',hex(randomblob(16)),'fixture',1)",
            values,
        )
        db.execute(
            "INSERT INTO roblox_names VALUES(?,?,?,1) ON CONFLICT(username_key) DO UPDATE SET "
            "latest_id=excluded.latest_id,completed_checks=completed_checks+1",
            (name.casefold(), name, row.lastrowid),
        )
    else:
        row = db.execute(
            "INSERT INTO x_checks(username,username_key,checked_at,classification,evidence,"
            "previous_classification,changed) VALUES(?,?,?,?,?,?,?)",
            values,
        )
        db.execute(
            "INSERT INTO x_names VALUES(?,?,1) ON CONFLICT(username_key) DO UPDATE SET "
            "latest_id=excluded.latest_id,completed_checks=completed_checks+1",
            (name.casefold(), row.lastrowid),
        )


@pytest.fixture
def dataset(tmp_path):
    store = Store(tmp_path / "results.db")
    XStore(store)
    samples = [
        ("Roblox", "aba", "Available"),
        ("Roblox", "abcd", "Taken"),
        ("Roblox", "ab12", "Available"),
        ("Roblox", "a11b", "Available"),
        ("Roblox", "1234", "Invalid"),
        ("Roblox", "a_b", "Taken"),
        ("Roblox", "_bad", "Invalid"),
        ("Roblox", "a.b", "Invalid"),
        ("Roblox", "a-b", "Invalid"),
        ("Roblox", "aa", "Invalid"),
        ("Roblox", "AaBb", "Available"),
        ("Roblox", "block", "Reserved"),
        ("Roblox", "mystery", "Unknown"),
        ("X", "abca", "Available"),
        ("X", "aabb", "Available"),
        ("X", "ab11", "Available"),
        ("X", "abc_de", "Taken"),
        ("X", "_abcd", "Taken"),
        ("X", "ab.cd", "Invalid"),
        ("X", "abc-def", "Invalid"),
        ("X", "plain", "Reserved"),
        ("X", "a1_b2", "Available"),
        ("X", "abc12", "Available"),
        ("X", "12345", "Invalid"),
    ]
    with store.transaction() as db:
        for sample in samples:
            record(db, *sample)
        record(db, "Roblox", "OldResult", "Available", at="2026-11-07T12:00:00+00:00")
        record(db, "Roblox", "oldresult", "Taken", previous="Available")
    yield store, ResultSearch(store.path), samples
    store.close()


def names(search, scope="All", **filters):
    rows, total, _ = search.page(scope, ResultFilter(**filters))
    assert len(rows) == total
    return {row["username"] for row in rows}


@pytest.mark.parametrize("scope,count", [("Roblox", 14), ("X", 11), ("All", 25)])
def test_scopes_latest_semantics(dataset, scope, count):
    _, search, _ = dataset
    rows, total, _ = search.page(scope, ResultFilter())
    assert len(rows) == total == count
    assert len({(row["platform"], row["username_key"]) for row in rows}) == count
    assert "OldResult" not in {row["username"] for row in rows}


@pytest.mark.parametrize(
    "mode,text,expected",
    [
        ("contains", "B1", {"ab12", "ab11"}),
        ("starts", "ABC", {"abcd", "abca", "abc_de", "abc-def", "abc12"}),
        ("ends", "B", {"a11b", "a_b", "a.b", "a-b", "AaBb", "aabb"}),
        ("exact", "AABB", {"AaBb", "aabb"}),
        ("contains", "", None),
    ],
)
def test_text_modes_casefold(dataset, mode, text, expected):
    _, search, _ = dataset
    actual = names(search, text=text, text_mode=mode)
    assert actual == expected if expected is not None else len(actual) == 25


def test_length_and_invalid_ranges(dataset):
    _, search, _ = dataset
    assert names(search, "Roblox", length_mode="exact", minimum=2) == {"aa"}
    assert "abca" in names(search, "X", length_mode="range", minimum=3, maximum=4)
    assert names(search, length_mode="exact", minimum=0) == set()
    with pytest.raises(ValueError, match="الطول"):
        names(search, length_mode="range", minimum=8, maximum=3)


@pytest.mark.parametrize(
    "filters,expected",
    [
        ({"composition": "digits"}, {"1234", "12345"}),
        ({"composition": "mixed_plain"}, {"ab12", "a11b", "ab11", "abc12"}),
        ({"composition": "mixed"}, {"ab12", "a11b", "ab11", "a1_b2", "abc12"}),
        ({"digit_mode": "exact", "digits": 2}, {"ab12", "a11b", "ab11", "a1_b2", "abc12"}),
        ({"digit_mode": "minimum", "digits": 4}, {"1234", "12345"}),
        ({"letter_mode": "exact", "letters": 0}, {"1234", "12345"}),
        ({"letter_mode": "minimum", "letters": 8}, {"oldresult"}),
        ({"repetition": "digits"}, {"a11b", "ab11"}),
        ({"repetition": "consecutive"}, {"a11b", "aa", "AaBb", "aabb", "ab11"}),
    ],
)
def test_composition_counts_repetitions(dataset, filters, expected):
    assert names(dataset[1], **filters) == expected


def test_precise_composition_and_repetition_semantics(dataset):
    search = dataset[1]
    letters = names(search, composition="letters")
    digits = names(search, composition="digits")
    assert names(search, composition="alphanumeric") == letters | digits | names(
        search, composition="mixed_plain"
    )
    assert "aba" in names(search, repetition="letters") and "abca" in names(search, repetition="anywhere")
    assert "abcd" in names(search, repetition="none") and "abcd" not in names(search, repetition="anywhere")
    assert "a1_b2" in names(search, composition="letter") & names(search, composition="digit")
    assert names(search, composition="symbols") == names(search, symbol_mode="present")
    assert names(search, composition="plain") == names(search, symbol_mode="absent")


def test_symbols_are_data_queries_not_validation(dataset):
    search = dataset[1]
    assert all(rule["symbols"] == "_" for rule in SYMBOL_RULES.values())
    assert syntax_error("_bad") and syntax_error("a.b")
    assert x_protocol.validate_syntax("_abcd")["valid"]
    assert not x_protocol.validate_syntax("ab.cd")["valid"]
    assert names(search, "Roblox", symbol_mode="contains", symbol="_") == {"a_b", "_bad"}
    assert names(search, symbol_mode="contains", symbol=".", classification="Invalid") == {"a.b", "ab.cd"}
    assert names(search, symbol_mode="contains", symbol="-", classification="Invalid") == {"a-b", "abc-def"}
    assert "_abcd" in names(search, "X", symbol_mode="contains", symbol="_")
    assert not names(search, symbol_mode="contains", symbol="_") & names(
        search, symbol_mode="excludes", symbol="_"
    )


@pytest.mark.parametrize("classification", ["Available", "Taken", "Reserved", "Invalid", "Unknown"])
def test_classification_and_history_latest_are_distinct(dataset, tmp_path, classification):
    store, search, _ = dataset
    before = store.db.total_changes
    rows, total, _ = search.page("All", ResultFilter(classification=classification))
    assert total > 0 and all(row["classification"] == classification for row in rows)
    if classification == "Available":
        assert "oldresult" not in {row["username"] for row in rows}
    path = tmp_path / "observations.csv"
    search.export(path, "All", ResultFilter(classification=classification), history=True)
    assert store.db.total_changes == before
    with path.open(encoding="utf-8-sig") as stream:
        exported = list(csv.DictReader(stream))
    assert all(row["classification"] == classification for row in exported)
    if classification == "Available":
        assert "OldResult" in {row["username"] for row in exported}


def test_combined_filters_and_sql_parameter_safety(dataset):
    search = dataset[1]
    assert names(
        search,
        "Roblox",
        length_mode="exact",
        minimum=3,
        composition="letters",
        repetition="letters",
        classification="Available",
    ) == {"aba"}
    assert names(
        search,
        "Roblox",
        length_mode="exact",
        minimum=4,
        digit_mode="exact",
        digits=2,
        symbol_mode="absent",
        classification="Available",
    ) == {"ab12", "a11b"}
    assert names(
        search,
        "X",
        length_mode="range",
        minimum=5,
        maximum=8,
        symbol_mode="contains",
        symbol="_",
        classification="Taken",
    ) == {"abc_de", "_abcd"}
    assert names(search, text="ab", repetition="none", classification="Available") == {
        "ab12",
        "abc12",
    }
    assert names(
        search,
        "Roblox",
        length_mode="exact",
        minimum=4,
        repetition="consecutive",
        digit_mode="minimum",
        digits=1,
        classification="Available",
    ) == {"a11b"}
    assert names(search, text="' OR 1=1 --") == set()
    assert names(search, text="%") == set()  # No accidental LIKE wildcard semantics.
    with pytest.raises(ValueError):
        search.page("x; DROP TABLE x_names", ResultFilter())


@pytest.mark.parametrize("scope", ["All", "Roblox", "X"])
def test_pages_export_all_matches_and_evidence(dataset, tmp_path, scope):
    store, search, _ = dataset
    with store.transaction() as db:
        for i in range(260):
            record(db, "Roblox" if i % 2 else "X", f"bulk{i:04}")
    filters = ResultFilter(text="bulk", digit_mode="exact", digits=4)
    first, total, _ = search.page(scope, filters)
    second, _, _ = search.page(scope, filters, offset=100)
    assert total == (260 if scope == "All" else 130)
    assert len(first) == 100 and len(second) == min(total - 100, 100)
    assert not {(r["platform"], r["username_key"]) for r in first} & {
        (r["platform"], r["username_key"]) for r in second
    }
    path = tmp_path / "all-pages.csv"
    assert search.export(path, scope, filters) == total
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        assert tuple(reader.fieldnames) == export_fields(scope)
        rows = list(reader)
    assert len(rows) == total and all(row["evidence"] for row in rows)
    if scope == "All":
        assert {row["platform"] for row in rows} == {"Roblox", "X"}
    assert all(row.get("independently_verified", "False") == "False" for row in rows)


def test_cancellation_readonly_and_export_safety(dataset, tmp_path):
    store, search, _ = dataset
    with search.connection() as db:
        with pytest.raises(Exception, match="readonly"):
            db.execute("DELETE FROM x_names")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(SearchCancelled):
        search.page("All", ResultFilter(), cancel=cancel)
    path = tmp_path / "preserved.csv"
    path.write_text("original", encoding="utf-8")
    with pytest.raises(SearchCancelled):
        search.export(path, "All", ResultFilter(), cancel=cancel)
    assert path.read_text() == "original" and not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError):
        search.export(store.path, "All", ResultFilter())


@pytest.mark.parametrize("scope", ["Roblox", "X"])
def test_existing_export_fields_and_rows_are_preserved(dataset, tmp_path, scope):
    store, search, _ = dataset
    original, advanced = tmp_path / "original.csv", tmp_path / "advanced.csv"
    if scope == "Roblox":
        RobloxStore(store).export_csv(original)
    else:
        XStore(store).export(original)
    search.export(advanced, scope, ResultFilter())
    with original.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        header, original_rows = reader.fieldnames, list(reader)
    with advanced.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == header
        advanced_rows = list(reader)
    assert sorted(original_rows, key=lambda r: r["username"]) == sorted(
        advanced_rows, key=lambda r: r["username"]
    )


def test_sqlite_progress_cancels_an_in_progress_query(dataset, monkeypatch):
    cancel = threading.Event()
    called = []
    register = result_search.register_functions

    def functions(db):
        register(db)

        def fold(name):
            called.append(name)
            cancel.set()
            return name.casefold()

        db.create_function("username_fold", 1, fold)

    monkeypatch.setattr(result_search, "register_functions", functions)
    with pytest.raises(SearchCancelled):
        dataset[1].page("All", ResultFilter(text="ab"), cancel=cancel)
    assert called


def test_large_dataset_streamed_filter_latency_and_pagination(tmp_path):
    store = Store(tmp_path / "large.db")
    XStore(store)
    try:
        with store.transaction() as db:
            for i in range(50000):
                record(db, "Roblox" if i % 2 else "X", f"ab{i:06}")
        search = ResultSearch(store.path)
        filters = ResultFilter(
            text="ab",
            length_mode="exact",
            minimum=8,
            composition="mixed_plain",
            repetition="digits",
            digit_mode="exact",
            digits=6,
        )
        started = time.perf_counter()
        rows, total, _ = search.page("All", filters, offset=100)
        latency = time.perf_counter() - started
        assert len(rows) == 100 and 0 < total <= 50000
        path = tmp_path / "large.csv"
        started = time.perf_counter()
        assert search.export(path, "All", filters) == total
        print(
            f"FILTER_PERFORMANCE records=50000 matches={total} query_seconds={latency:.4f} "
            f"export_seconds={time.perf_counter() - started:.4f}"
        )
        # A count and page are one snapshot, with a bounded result list instead of a full transfer.
        assert len(rows) <= 100
    finally:
        store.close()


def test_filter_validation_and_changed(dataset):
    search = dataset[1]
    assert names(search, changed=True) == {"oldresult"}
    for filters in (
        ResultFilter(text_mode="sql"),
        ResultFilter(digits=-1),
        ResultFilter(symbol_mode="contains", symbol="abc"),
    ):
        with pytest.raises(ValueError):
            search.page("All", filters)
    assert names(search, **replace(ResultFilter(), text="not-in-data").__dict__) == set()
