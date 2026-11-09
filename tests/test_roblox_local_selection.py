import csv
import hashlib
import json
import socket
import sqlite3
from dataclasses import replace

import pytest

from username_finder.result_search import ResultFilter
from username_finder.roblox import prepare_inputs, syntax_error
from username_finder.roblox_local_selection import Cancelled, LocalSelection, Selection


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No live requests permitted")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def build(tmp_path, text, config=None, suffix="txt"):
    source = tmp_path / ("source." + suffix)
    source.write_text(text, encoding="utf-8", newline="")
    tool = LocalSelection(tmp_path / "run").create(source, config)
    return tool, source


def records(tool):
    with tool.connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM records ORDER BY ordinal")]


def test_disabled_preserves_source_order_exact_dedup_and_original_evidence(tmp_path):
    text = " NameOne \nnameone\nName_Two\ninvalid-name\n\nKool\nKool\n"
    tool, source = build(tmp_path, text)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    summary = tool.prepare()
    assert summary["counts"] == dict(
        SelectedValid=3, LowerPriorityValid=0, UserExcluded=0, LocalInvalid=2, Duplicate=1, Blank=1
    )
    output = tmp_path / "export"
    result = tool.export(output)
    assert result["complete"] and not result["availability_verified"] and not result["provider_results"]
    assert (output / "selected.txt").read_text().splitlines() == ["NameOne", "Name_Two", "Kool"]
    assert (output / "retained.txt").read_text() == ""
    with (output / "audit.csv").open(encoding="utf-8", newline="") as stream:
        audit = list(csv.DictReader(stream))
    assert [row["original_value"] for row in audit] == text.splitlines(keepends=True)
    assert audit[1]["first_record"] == "1" and audit[1]["local_category"] == "Duplicate"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest == result["sha256"]
    for filename, info in result["files"].items():
        assert hashlib.sha256((output / filename).read_bytes()).hexdigest() == info["sha256"]


def test_deterministic_user_priorities_budget_and_retention(tmp_path):
    config = Selection(
        budget=3,
        prioritize=True,
        shorter_first=True,
        fewer_digits_first=True,
        filters=ResultFilter(length_mode="range", minimum=3, maximum=8),
        preferred=ResultFilter(composition="letters"),
    )
    tool, source = build(tmp_path, "ab12\nLongerName\nZed\nBob\nabc1\nabcdef\nZED\n", config)
    first = tool.prepare()
    assert first == tool.prepare()
    assert first["counts"] == dict(
        SelectedValid=3, LowerPriorityValid=2, UserExcluded=1, LocalInvalid=0, Duplicate=1, Blank=0
    )
    output = tmp_path / "export"
    tool.export(output)
    assert (output / "selected.txt").read_text().splitlines() == ["Zed", "Bob", "abcdef"]
    assert (output / "retained.txt").read_text().splitlines() == ["ab12", "LongerName", "abc1"]
    assert source.read_text().startswith("ab12\n")
    assert first["budget_examples"] == {"10000": 5, "50000": 5, "100000": 5}


@pytest.mark.parametrize(
    "name,valid",
    [
        ("ab", False),
        ("abc", True),
        ("a" * 20, True),
        ("a" * 21, False),
        ("a_b", True),
        ("_abc", False),
        ("abc_", False),
        ("a_b_c", False),
        ("123", True),
        ("ab-c", False),
        ("Kab", False),
        ("ab\u200bc", False),
    ],
)
def test_existing_syntax_boundaries(tmp_path, name, valid):
    tool, _ = build(tmp_path, name + "\n")
    result = tool.prepare()
    assert bool(result["counts"]["SelectedValid"]) == valid == (syntax_error(name) is None)


@pytest.mark.parametrize(
    "filters,expected",
    [
        (ResultFilter(text="ab", text_mode="starts"), ["abc", "ab12"]),
        (ResultFilter(text="12", text_mode="ends"), ["ab12"]),
        (ResultFilter(digit_mode="exact", digits=2), ["ab12", "1ab2"]),
        (ResultFilter(symbol_mode="present"), ["a_b"]),
        (ResultFilter(repetition="consecutive"), ["aab"]),
        (ResultFilter(repetition="none"), ["abc", "ab12", "a_b", "1ab2"]),
    ],
)
def test_shared_filter_rules(tmp_path, filters, expected):
    tool, _ = build(tmp_path, "abc\nab12\na_b\naab\n1ab2", Selection(filters=filters))
    tool.prepare()
    tool.export(tmp_path / "output")
    assert (tmp_path / "output" / "selected.txt").read_text().splitlines() == expected


@pytest.mark.parametrize(
    "config",
    [
        Selection(budget=0),
        Selection(budget=True),
        Selection(budget=1.5),
        Selection(prioritize=1),
        Selection(filters=ResultFilter(classification="Available")),
        Selection(preferred=ResultFilter(changed=True)),
        Selection(filters=ResultFilter(text_mode="invented")),
    ],
)
def test_reject_bad_or_provider_criteria_before_creating_files(tmp_path, config):
    with pytest.raises(ValueError):
        build(tmp_path, "abc", config)
    assert not (tmp_path / "run").exists()


def test_import_cancellation_restart_and_exact_dedup_100k(tmp_path):
    tool, source = build(tmp_path, "".join(f"Name{i:06}\n" for i in range(100_000)))
    tool.notify = lambda _: tool.cancel.set()
    with pytest.raises(Cancelled):
        tool.prepare()
    tool.cancel.clear()
    assert tool.summary()["cursor"] == 1000
    resumed = LocalSelection(tool.directory)
    result = resumed.prepare()
    assert result["cursor"] == result["counts"]["SelectedValid"] == 100_000
    assert len(records(resumed)) == 100_000
    assert prepare_inputs(files=(source,)).total == 100_000


def test_cancelled_selection_transaction_rolls_back_and_recovers(tmp_path):
    tool, _ = build(tmp_path, "abc\ndef\nghi", Selection(budget=1, prioritize=True))
    # Simulate a crash after durable import but before selection's atomic commit.
    with tool.connect() as db:
        meta = tool.meta(db)
        for i, name in enumerate(("abc", "def", "ghi"), 1):
            db.execute(
                "INSERT INTO records(ordinal,key,raw,username,first_record,category,reason) "
                "VALUES(?,?,?,?,?,'LowerPriorityValid','')",
                (i, name, name, name, i),
            )
        meta.update(cursor=3, phase="Selecting")
        tool.save(db, meta)
    with pytest.raises(RuntimeError), tool.connect() as db:
        db.execute("UPDATE records SET category='UserExcluded'")
        raise RuntimeError("interrupted before commit")
    assert tool.prepare()["counts"]["SelectedValid"] == 1
    assert tool.summary()["counts"]["UserExcluded"] == 0


def test_export_cancellation_never_marks_complete_or_overwrites(tmp_path):
    tool, _ = build(tmp_path, "".join(f"Name{i:05}\n" for i in range(2000)))
    tool.prepare()
    tool.notify = lambda _: tool.cancel.set()
    incomplete = tmp_path / "incomplete"
    with pytest.raises(Cancelled):
        tool.export(incomplete)
    assert not (incomplete / "summary.json").exists()
    before = (incomplete / "audit.csv").read_bytes()
    tool.cancel.clear()
    tool.notify = lambda _: None
    with pytest.raises(FileExistsError):
        tool.export(incomplete)
    assert (incomplete / "audit.csv").read_bytes() == before
    assert tool.export(tmp_path / "fresh")["files"]["selected.txt"]["rows"] == 2000


@pytest.mark.parametrize(
    "text,suffix",
    [
        ("", "txt"),
        ("\n\n", "txt"),
        ("username\n", "csv"),
        ("username,extra\nabc", "csv"),
        ('username\n"unclosed', "csv"),
        ("ab\x00cd", "txt"),
        ("a" * (1024 * 1024 + 1), "txt"),
    ],
)
def test_empty_and_malformed_input(tmp_path, text, suffix):
    tool, source = build(tmp_path, text, suffix=suffix)
    original = source.read_bytes()
    if not text.strip() or text == "username\n":
        assert tool.prepare()["counts"]["SelectedValid"] == 0
    else:
        with pytest.raises(ValueError):
            tool.prepare()
        assert tool.summary()["phase"] == "Importing"
    assert source.read_bytes() == original


def test_csv_duplicate_heavy_and_resume_source_tamper(tmp_path):
    tool, source = build(tmp_path, "username,note\nAbc,x\nABC,y\nabc,z\na_b,q\n", suffix="csv")
    result = tool.prepare()
    assert result["counts"]["SelectedValid"] == result["counts"]["Duplicate"] == 2
    before = records(tool)
    source.write_text("username\nother", encoding="utf-8")
    with pytest.raises(ValueError, match="Source changed"):
        tool.prepare()
    assert records(tool) == before


def test_default_compatibility_with_existing_preparation(tmp_path):
    tool, source = build(tmp_path, "abc\ndef123\nABC\ninvalid-name\na_b\n\n")
    original = prepare_inputs(files=(source,))
    counts = tool.prepare()["counts"]
    assert counts["SelectedValid"] == original.total
    assert counts["Duplicate"] == original.duplicates
    assert counts["LocalInvalid"] == original.invalid_count
    assert counts["Blank"] == original.blank


def test_serialized_config_preserves_explicit_budget_and_criteria():
    original = Selection(
        budget=10000,
        prioritize=True,
        shorter_first=True,
        preferred=ResultFilter(text="aa", text_mode="starts"),
    )
    from dataclasses import asdict

    assert Selection.from_data(json.loads(json.dumps(asdict(original)))) == original
    assert replace(original, prioritize=False).order == "ordinal"


def test_sql_failure_retains_last_checkpoint(tmp_path, monkeypatch):
    tool, _ = build(tmp_path, "".join(f"Name{i:05}\n" for i in range(2000)))
    save = tool.save

    def fail(db, meta):
        if meta["cursor"] > 1000:
            raise sqlite3.OperationalError("disk full")
        save(db, meta)

    monkeypatch.setattr(tool, "save", fail)
    with pytest.raises(sqlite3.OperationalError):
        tool.prepare()
    assert tool.summary()["cursor"] == len(records(tool)) == 1000
    assert LocalSelection(tool.directory).prepare()["counts"]["SelectedValid"] == 2000


def test_resume_cli_uses_saved_settings_and_never_overwrites_exports(tmp_path, capsys):
    from tools.roblox_local_select import main

    source = tmp_path / "names.csv"
    source.write_text("username\nab12\nabc\nabc\ndef\n", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"budget": 1, "prioritize": True, "preferred": {"composition": "letters"}}),
        encoding="utf-8",
    )
    base = ["--run", str(tmp_path / "run")]
    assert (
        main([*base, "--source", str(source), "--config", str(config), "--export", str(tmp_path / "first")])
        == 0
    )
    assert main([*base, "--resume", "--export", str(tmp_path / "second")]) == 0
    assert (tmp_path / "second" / "selected.txt").read_text() == "abc\n"
    original = (tmp_path / "first" / "summary.json").read_bytes()
    assert main([*base, "--resume", "--export", str(tmp_path / "first")]) == 1
    assert (tmp_path / "first" / "summary.json").read_bytes() == original
    with pytest.raises(SystemExit):
        main([*base, "--resume", "--config", str(config), "--export", str(tmp_path / "third")])
    assert "saved source/criteria" in capsys.readouterr().err


def test_invalid_utf8_is_not_reinterpreted(tmp_path):
    tool, source = build(tmp_path, "abc")
    source.write_bytes(b"\xff\xfe")
    # Build an explicit new run for this byte stream; Unicode errors cannot become valid names.
    tool = LocalSelection(tmp_path / "bad").create(source)
    with pytest.raises(ValueError):
        tool.prepare()
    assert tool.summary()["cursor"] == 0


def test_cancel_in_sql_progress_handler(tmp_path):
    tool, _ = build(tmp_path, "".join(f"Name{i:05}\n" for i in range(2000)))
    tool.prepare()
    with pytest.raises(Cancelled), tool.connect() as db:
        tool.cancel.set()
        db.execute("SELECT count(*) FROM records a, records b").fetchone()
    tool.cancel.clear()
    assert tool.summary()["counts"]["SelectedValid"] == 2000


def test_offline_benchmark_fixture_and_audit(tmp_path):
    from tools.roblox_local_selection_benchmark import synthetic, worker

    source = tmp_path / "mixed.txt"
    source.write_text("".join(synthetic(i) + "\n" for i in range(100)), encoding="utf-8", newline="")
    measurements = [
        worker(mode, source, tmp_path / mode) for mode in ("existing", "source_order", "prioritized")
    ]
    assert measurements[0]["input_records"] == 100
    assert measurements[1]["counts"] == measurements[2]["counts"]
    assert measurements[2]["counts"]["Duplicate"] == measurements[0]["duplicates"] == 9
    assert all(m["network_attempts"] == 0 for m in measurements)


def test_fewer_digits_breaks_equal_length_priority_in_configured_order(tmp_path):
    tool, _ = build(
        tmp_path, "a123\nab12\nabc1\nabcd\n", Selection(budget=2, prioritize=True, fewer_digits_first=True)
    )
    tool.prepare()
    tool.export(tmp_path / "output")
    assert (tmp_path / "output" / "selected.txt").read_text().splitlines() == ["abcd", "abc1"]
    assert (tmp_path / "output" / "retained.txt").read_text().splitlines() == ["a123", "ab12"]


@pytest.mark.parametrize("budget", [10000, 50000])
def test_explicit_candidate_budget_boundaries(tmp_path, budget):
    tool, _ = build(tmp_path, "".join(f"Name{i:05}\n" for i in range(60000)), Selection(budget=budget))
    result = tool.prepare()
    assert result["counts"]["SelectedValid"] == budget
    assert result["counts"]["LowerPriorityValid"] == 60000 - budget
    with tool.connect() as db:
        selected_end = db.execute(
            "SELECT max(ordinal) FROM records WHERE category='SelectedValid'"
        ).fetchone()[0]
        assert selected_end == budget
