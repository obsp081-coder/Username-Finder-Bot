from dataclasses import replace

import pytest

from username_finder.domain import Filters, SearchConfig
from username_finder.sources import CandidateSource, MeaningLibrary, make_snapshot


def test_real_curated_packages_provenance_and_categories():
    library = MeaningLibrary()
    categories = tuple(p["category"] for p in library.manifest["packages"])
    entries = library.entries(categories)
    assert len(entries) == len({name for name, _ in entries})
    assert len(entries) > 100
    assert all(p["count"] > 0 for p in library.manifest["packages"])
    assert all(p["license"] and p["source_url"] and p["sha256"] for p in library.manifest["packages"])
    config = SearchConfig(filters=Filters(3, 15), generation_mode="meaningful")
    source = CandidateSource(config, make_snapshot(config))
    actual = []
    while record := source.next_record():
        actual.append(record.username)
        assert record.metadata["original"] == record.username
    assert set(actual) == {name for name, _ in entries}
    # Filters never manufacture derived names from a dictionary.
    with_digit = replace(config, filters=Filters(3, 15, required="7"))
    assert not any(with_digit.filters.matches(name) for name in actual)


def test_corrupt_missing_or_changed_library_blocks_resume(tmp_path):
    original = MeaningLibrary()
    for path in original.directory.iterdir():
        if path.is_file():
            (tmp_path / path.name).write_bytes(path.read_bytes())
    library = MeaningLibrary(tmp_path)
    config = SearchConfig(generation_mode="meaningful")
    snapshot = make_snapshot(config, library)
    source = CandidateSource(config, snapshot, library=library)
    record = source.next_record()
    package = library.manifest["packages"][0]
    file = tmp_path / package["file"]
    file.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        CandidateSource(config, snapshot, record.cursor, library=library)


def test_txt_long_line_strict_utf8_and_exact_resume(tmp_path):
    path = tmp_path / "names.txt"
    path.write_bytes(b"a" * 80000 + b"\r\nABC\r\n@Abc\n\xef\xbb\xbfdef\n")
    config = SearchConfig(source="txt", files=(str(path),))
    snapshot = make_snapshot(config)
    source = CandidateSource(config, snapshot)
    assert source.next_record().kind == "malformed"
    record = source.next_record()
    assert record.username == "abc"
    source.close()
    resumed = CandidateSource(config, snapshot, record.cursor)
    assert resumed.next_record().username == "@abc"
    assert resumed.next_record().username == "\ufeffdef"  # BOM is stripped only at start of file.
    assert resumed.next_record() is None
    resumed.close()


def test_change_during_txt_stream_is_detected(tmp_path):
    path = tmp_path / "input.txt"
    path.write_text("abc\nabd\n", encoding="utf-8")
    config = SearchConfig(source="txt", files=(str(path),))
    source = CandidateSource(config, make_snapshot(config))
    assert source.next_record().username == "abc"
    path.write_text("abc\nabe\nlonger\n", encoding="utf-8")
    with pytest.raises(ValueError):
        source.next_record()
    source.close()
