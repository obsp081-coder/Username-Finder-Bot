import codecs
import hashlib
import os
import tracemalloc
from threading import Event

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from username_finder.file_search import (
    MAX_COPY_BYTES,
    PAGE_BYTES,
    WHOLE_VIEW_BYTES,
    FileInfo,
    Match,
    SearchFailure,
    inspect_line,
    read_page,
    search_files,
    validate_search,
)


def collect(paths, query, limit=100, **kwargs):
    rows = []
    summary = search_files(paths, query, limit, on_batch=rows.extend, **kwargs)
    return summary, rows


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    text=st.text(alphabet="aab.[]0 \n\rعé🙂", max_size=400),
    query=st.text(alphabet="ab.[]0 عé🙂", min_size=1, max_size=5),
    chunk=st.integers(min_value=4, max_value=29),
    limit=st.integers(min_value=1, max_value=20),
)
def test_streaming_matches_naive_utf8_reference(tmp_path, text, query, chunk, limit):
    path = tmp_path / "property.jsonl"
    raw, needle = text.encode(), query.encode()
    path.write_bytes(raw)
    expected = []
    cursor = 0
    while (pos := raw.find(needle, cursor)) >= 0 and len(expected) < limit:
        start = raw.rfind(b"\n", 0, pos) + 1
        expected.append((pos, raw.count(b"\n", 0, pos) + 1, start, pos - start + 1))
        cursor = pos + 1
    summary, rows = collect([path], query, limit, chunk_bytes=chunk)
    assert summary.status in ("complete", "limit")
    assert [(m.offset, m.line, m.line_start, m.byte_column) for m in rows] == expected


def test_literal_overlap_case_sensitive_crlf_and_multiple_files(tmp_path):
    first, second = tmp_path / "first.txt", tmp_path / "second.json"
    first.write_bytes(b"BANANA\r\nbanana a.b a.b\r\n")
    second.write_bytes(b"not valid json banana")
    summary, rows = collect([first, second, first], "ana", chunk_bytes=5)
    assert summary.status == "complete" and summary.progress.files_done == 2
    assert [(m.line, m.byte_column) for m in rows] == [(2, 2), (2, 4), (1, 17), (1, 19)]
    _, literal = collect([first], "a.b", chunk_bytes=4)
    assert [m.byte_column for m in literal] == [8, 12]


def test_limit_stops_validating_and_counting_at_last_occurrence(tmp_path):
    path = tmp_path / "early.txt"
    path.write_bytes(b"aaa" + b"x" * 10000 + b"\xff")
    summary, rows = collect([path], "a", 2, chunk_bytes=1024)
    assert len(rows) == 2 and summary.status == "limit"
    assert summary.progress.processed == 2 and summary.progress.bytes_read <= 1028
    assert summary.progress.files_done == 0
    complete, _ = collect([path], "a", 10, chunk_bytes=1024)
    assert complete.status == "error" and complete.error_code == "encoding"
    path.write_bytes(b"aa")
    eof, _ = collect([path], "a", 2)
    assert eof.status == "complete" and eof.progress.files_done == 1


@pytest.mark.parametrize("query", ["", "\n", "a\rb", "\0", "\ud800", "x" * 65537])
def test_invalid_query_is_explicit(query):
    with pytest.raises(SearchFailure, match="حرفي"):
        validate_search(query, 100)


@pytest.mark.parametrize("limit", [0, -1, 1.5, True, 100001])
def test_invalid_limits_are_explicit(limit):
    with pytest.raises(SearchFailure):
        validate_search("x", limit)


@pytest.mark.parametrize(
    "raw", [b"\xffbad", b"\xff\xfeA\0", b"\xfe\xff\0A", b"\0\0\xfe\xff", b"a\0b", b"a\xe2\x82"]
)
def test_bad_encoding_never_silently_reports_no_results(tmp_path, raw):
    path = tmp_path / "bad.txt"
    path.write_bytes(raw)
    summary, _ = collect([path], "absent", chunk_bytes=4)
    assert summary.status == "error" and summary.error_code == "encoding"


def test_empty_bom_unicode_positions_and_complete_line_read(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.touch()
    assert collect([empty], "x")[0].status == "complete"
    path = tmp_path / "unicode.jsonl"
    line = '{"u":"ع🙂é","e":"x@example.com"}'
    raw = codecs.BOM_UTF8 + line.encode() + b"\r\nlast"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).digest()
    summary, rows = collect([path], "é", chunk_bytes=4)
    assert summary.status == "complete" and len(rows) == 1
    assert rows[0].offset == raw.find("é".encode())
    assert rows[0].byte_column == raw.find("é".encode()) - 3 + 1
    info = inspect_line(rows[0], Event())
    assert read_page(info, 0, Event()).text == line
    assert hashlib.sha256(path.read_bytes()).digest() == digest


def test_changed_deleted_and_inaccessible_sources(tmp_path, monkeypatch):
    path = tmp_path / "source.txt"
    path.write_text("match original", encoding="utf-8")
    _, rows = collect([path], "match")
    path.write_text("match changed source", encoding="utf-8")
    with pytest.raises(SearchFailure) as changed:
        inspect_line(rows[0], Event())
    assert changed.value.code == "changed"
    missing = tmp_path / "missing.txt"
    summary, _ = collect([missing], "x")
    assert summary.error_code == "access"
    assert collect([tmp_path], "x")[0].error_code == "not_file"
    monkeypatch.setattr(FileInfo, "open", lambda self: (_ for _ in ()).throw(SearchFailure("access")))
    assert collect([path], "x")[0].error_code == "access"


def test_hardlink_duplicate_identity(tmp_path):
    path, alias = tmp_path / "original.txt", tmp_path / "alias.txt"
    path.write_text("match", encoding="utf-8")
    try:
        os.link(path, alias)
    except OSError:
        pytest.skip("Hard links unavailable")
    summary, rows = collect([path, alias], "match")
    assert len(rows) == 1 and summary.progress.files_total == 1


def test_moved_source_cannot_display_a_stale_result(tmp_path):
    path = tmp_path / "original.txt"
    path.write_bytes(b"match original line\n")
    _, rows = collect([path], "match")
    moved = tmp_path / "moved.txt"
    path.rename(moved)
    with pytest.raises(SearchFailure) as missing:
        inspect_line(rows[0], Event())
    assert missing.value.code == "access"
    assert moved.read_bytes() == b"match original line\n"


def test_long_line_all_pages_cover_complete_unicode_without_gaps(tmp_path):
    text = "ع🙂z" * (WHOLE_VIEW_BYTES // 3)
    path = tmp_path / "long.jsonl"
    raw = text.encode()
    path.write_bytes(raw + b"\r\n")
    _, rows = collect([path], "🙂", 1)
    info = inspect_line(rows[0], Event())
    covered = 0
    for offset in range(0, len(raw), PAGE_BYTES):
        page = read_page(info, offset, Event())
        assert page.start <= covered and page.text.encode() == raw[page.start : page.end]
        assert len(page.text.encode()) <= PAGE_BYTES + 3
        covered = page.end
    assert covered == len(raw)
    assert read_page(info, 0, Event(), whole=True).text == text
    larger = tmp_path / "very-long.txt"
    with larger.open("wb") as stream:
        for _ in range(MAX_COPY_BYTES // PAGE_BYTES + 1):
            stream.write(b"x" * PAGE_BYTES)
    identity = FileInfo.capture(larger)
    line = inspect_line(Match(identity, 1, 0, 0), Event())
    with pytest.raises(SearchFailure) as error:
        read_page(line, 0, Event(), whole=True)
    assert error.value.code == "copy_size"
    assert read_page(line, line.length - 100, Event()).text == "x" * 100


def test_cancellation_and_bounded_scan_memory(tmp_path):
    path = tmp_path / "scan.txt"
    block = b'{"u":"synthetic","v":12345}\n' * 2048
    with path.open("wb") as stream:
        for _ in range(400):
            stream.write(block)
    tracemalloc.start()
    summary, rows = collect([path], "missing")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert summary.status == "complete" and not rows and peak < 16 * 1024 * 1024
    cancel = Event()
    progress = []

    def stop(value):
        progress.append(value)
        if value.processed:
            cancel.set()

    summary, _ = collect([path], "missing", cancel=cancel, on_progress=stop, chunk_bytes=1024)
    assert summary.status == "cancelled" and summary.progress.processed < path.stat().st_size


def test_changed_during_scan_and_line_load_cancel(tmp_path):
    path = tmp_path / "changing.txt"
    path.write_bytes(b"a\n" * 10000)
    cancel = Event()

    def alter(batch):
        with path.open("ab") as stream:
            stream.write(b"changed")

    summary = search_files([path], "a", 10000, on_batch=alter, chunk_bytes=1024)
    assert summary.status == "error" and summary.error_code == "changed"
    match = Match(FileInfo.capture(path), 1, 0, 0)
    cancel.set()
    from concurrent.futures import CancelledError

    with pytest.raises(CancelledError):
        inspect_line(match, cancel)
