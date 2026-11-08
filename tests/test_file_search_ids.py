import codecs
import json
import tracemalloc
from threading import Event

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from username_finder.file_search import FileInfo, inspect_line, read_page
from username_finder.file_search_ids import MAX_RECORD_BYTES, search_account_ids


def collect(path, identifier="123", **kwargs):
    rows = []
    result = search_account_ids([path], identifier, on_batch=rows.extend, **kwargs)
    return result, rows


def test_exact_top_level_ids_positions_bom_unicode_and_copy(tmp_path):
    lines = [
        '{"name":"عربي🙂","id":"123","other":"123"}',
        '{"id":123,"nested":{"id":"123"}}',
        '{"id":"1234"}',
        '{"id":1230}',
        '{"phone":"123","email":"123@x"}',
        '{"nested":{"id":"123"}}',
        '{"id":123.0}',
        '{"id":1.23e2}',
        '{"id":true}',
        '{"id":null}',
        '{"id":"0123"}',
        '{"id":"123","text":"123123"}',
    ]
    path = tmp_path / "records.txt"
    path.write_bytes(codecs.BOM_UTF8 + ("\r\n".join(lines)).encode())
    result, rows = collect(path, chunk_bytes=17)
    assert result.status == "complete" and len(rows) == 3
    assert [row.line for row in rows] == [1, 2, 12]
    raw = path.read_bytes()
    for row in rows:
        assert raw[row.offset : row.offset + 3] == b"123"
        assert raw[row.line_start : row.offset].endswith((b':"', b":"))
        page = read_page(inspect_line(row, Event()), 0, Event(), whole=True)
        assert page.text == lines[row.line - 1]
    assert result.progress.processed == len(raw)


@given(
    values=st.lists(st.sampled_from(["123", "1234", "0123", "other"]), min_size=1, max_size=25),
    chunk=st.integers(min_value=4, max_value=75),
    limit=st.integers(min_value=1, max_value=12),
)
@settings(max_examples=55, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_stream_boundaries_match_naive_reference(tmp_path, values, chunk, limit):
    path = tmp_path / "reference.jsonl"
    records = [json.dumps({"u": "ع🙂", "id": v, "email": "123@x"}, ensure_ascii=False) for v in values]
    raw = ("\n".join(records) + "\n").encode()
    path.write_bytes(raw)
    result, rows = collect(path, chunk_bytes=chunk, limit=limit)
    expected, start = [], 0
    for line, (record, value) in enumerate(zip(records, values, strict=True), 1):
        if value == "123":
            expected.append((line, start, start + record.encode().index(b'"123"') + 1))
        start += len(record.encode()) + 1
    expected = expected[:limit]
    assert [(r.line, r.line_start, r.offset) for r in rows] == expected
    assert result.progress.processed <= len(raw)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"id":"123",broken}\n', "json_record"),
        (b'{"id":"123"}\n{"id":"123"', "json_record"),
        (b'{"id":"123","id":"456"}\n', "json_structure"),
        (b'[ {"id":"123"} ]\n', "json_structure"),
        (b"ordinary text 123\n", "json_structure"),
        (b'{\n"id":"123"\n}\n', "json_record"),
        (b'{"id":"123","bad":NaN}\n', "json_record"),
        (b'{"id":"123"} trailing\n', "json_record"),
        (b'{"id":"\\u0031\\u0032\\u0033"}\n', "json_escape"),
        (b'{"id":"123","bad":"\xff"}', "encoding"),
        (b"\xff\xfe1\x002\x003\x00", "encoding"),
    ],
)
def test_bad_candidates_and_unsupported_inputs_are_explicit(tmp_path, raw, code):
    path = tmp_path / "unsupported.txt"
    path.write_bytes(raw)
    result, _ = collect(path, chunk_bytes=7)
    assert result.status == "error" and result.error_code == code


def test_candidate_filter_is_not_a_full_json_audit(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_bytes(b'{}\nmalformed noncandidate\n{"id":123}\n')
    result, rows = collect(path)
    assert result.status == "complete" and rows[0].line == 3


def test_limits_eof_hardlink_dedup_and_cancellation(tmp_path):
    path = tmp_path / "records.jsonl"
    raw = b'{"id":"123"}\n'
    path.write_bytes(raw * 10000)
    result, rows = collect(path, limit=2)
    assert result.status == "limit" and len(rows) == 2
    assert result.progress.processed == 2 * len(raw)
    assert result.progress.processed < result.progress.total_bytes
    path.write_bytes(raw)
    result, rows = collect(path, limit=1)
    assert result.status == "complete" and len(rows) == 1
    rows = []
    result = search_account_ids([path, path], "123", on_batch=rows.extend)
    assert result.status == "complete" and len(rows) == 1
    cancel = Event()
    path.write_bytes(raw * 10000)
    result = search_account_ids(
        [path], "123", 10000, cancel=cancel, on_batch=lambda _: cancel.set(), chunk_bytes=4096
    )
    assert result.status == "cancelled" and result.progress.processed < path.stat().st_size


def test_bounded_records_memory_and_changed_source(tmp_path):
    path = tmp_path / "large.jsonl"
    path.write_bytes(b"{}\n" + b'{"text":"' + b"x" * MAX_RECORD_BYTES + b'"}\n')
    result, rows = collect(path)
    assert result.error_code == "json_size" and not rows
    path.write_bytes(b'{"id":"456","text":"padding"}\n' * 300000)
    before = FileInfo.capture(path)
    tracemalloc.start()
    result, _ = collect(path)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert result.status == "complete" and peak < 18 * 1024**2
    assert FileInfo.capture(path) == before


def test_source_mutation_rejected_after_candidate_batch(tmp_path):
    path = tmp_path / "changed.jsonl"
    path.write_bytes(b'{"id":123}\n' * 500)

    def change(_):
        with path.open("ab") as stream:
            stream.write(b"{}\n")

    result = search_account_ids([path], "123", 500, on_batch=change)
    assert result.status == "error" and result.error_code == "changed"
