import threading
import tracemalloc

import pytest
from test_roblox import response

from username_finder.roblox import generation_spec, input_spec, prepare_inputs
from username_finder.roblox_runner import RobloxRunner
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store


@pytest.fixture
def repo(tmp_path):
    store = Store(tmp_path / "large.db")
    yield RobloxStore(store)
    store.close()


def names_file(path, count):
    with path.open("w", encoding="utf-8") as stream:
        if path.suffix == ".csv":
            stream.write("username\n")
        for index in range(count):
            stream.write(f"Name{index:08d}\n")
    return path


@pytest.mark.parametrize("extension,count", [("txt", 36_504), ("csv", 120_000)])
def test_large_operation_streams_without_count_ceiling(repo, tmp_path, extension, count):
    path = names_file(tmp_path / f"names.{extension}", count)
    tracemalloc.start()
    try:
        identity = repo.create_run(spec=input_spec(files=[path]))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    result = repo.progress(identity)
    assert result["state"] == "Ready"
    assert result["total"] == result["input_cursor"] == count
    assert peak < 10 * 1024 * 1024
    assert repo.results()[1] == 0


def test_large_preview_has_exact_counts_bounded_examples(tmp_path):
    path = names_file(tmp_path / "preview.txt", 36_504)
    result = prepare_inputs(files=[path])
    assert result.total == 36_504
    assert len(result.names) == 20


def test_dedup_invalid_blank_skip_and_generation_above_old_limit(repo, tmp_path):
    first = repo.create_run(["Name00000000"])
    RobloxRunner(repo, fetcher=lambda _: response(1), wait=lambda _: False).run(first)
    path = names_file(tmp_path / "names.txt", 36_504)
    with path.open("a") as stream:
        stream.write("NAME00000000\ninvalid-name\nINVALID-NAME\n\n")
    identity = repo.create_run(spec=input_spec(files=[path]))
    saved = repo.progress(identity)
    assert saved["total"] == 36_504 and saved["skipped"] == 1
    assert saved["duplicates"] == 2 and saved["invalid"] == 1 and saved["blank"] == 1
    generated = repo.create_run(spec={"generation": generation_spec(4, 36_504)})
    assert repo.progress(generated)["total"] == 36_504


def test_preparation_cancel_restart_and_check_resume_never_duplicate_requests(repo, tmp_path):
    path = names_file(tmp_path / "resume.txt", 36_504)
    cancel = threading.Event()
    identity = repo.create_run(spec=input_spec(files=[path]), cancel=cancel, notify=lambda _: cancel.set())
    assert repo.progress(identity)["state"] == "PreparationPaused"
    assert repo.progress(identity)["input_cursor"] == 1000
    calls = []
    RobloxRunner(repo, fetcher=lambda url: calls.append(url)).run(identity)
    assert not calls
    # Recover as if the process exited after committing its preparation batch.
    database = repo.store.path
    repo.store.close()
    reopened = Store(database)
    try:
        other = RobloxStore(reopened)
        other.recover()
        other.create_run(run_id=identity)
        assert other.progress(identity)["total"] == 36_504
        runner = RobloxRunner(
            other, fetcher=lambda url: (calls.append(url), response())[1], wait=lambda _: False
        )
        runner.updated = lambda: runner.cancel.set() if len(calls) == 2 else None
        runner.run(identity)
        assert other.progress(identity)["state"] == "Paused"
        assert len(calls) == len(set(calls)) == other.results()[1] == 2
    finally:
        reopened.close()


def test_malformed_partial_import_blocks_checking_and_preserves_batches(repo, tmp_path):
    path = names_file(tmp_path / "bad.txt", 1500)
    with path.open("ab") as stream:
        stream.write(b"\xff")
    with pytest.raises(ValueError):
        repo.create_run(spec=input_spec(files=[path]))
    saved = repo.runs()[0]
    assert saved["state"] == "PreparationError"
    assert 0 <= saved["input_cursor"] < 1501
    assert not repo.start(saved["id"])


def test_source_change_blocks_resume_and_disk_failure_preserves_checkpoint(repo, tmp_path, monkeypatch):
    path = names_file(tmp_path / "changed.txt", 2500)
    cancel = threading.Event()
    identity = repo.create_run(spec=input_spec(files=[path]), cancel=cancel, notify=lambda _: cancel.set())
    with path.open("a") as stream:
        stream.write("NewName\n")
    with pytest.raises(ValueError):
        repo.create_run(run_id=identity)
    assert repo.progress(identity)["input_cursor"] == 1000

    from username_finder import roblox_storage

    monkeypatch.setattr(roblox_storage.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 0})())
    with pytest.raises(ValueError):
        repo.create_run(spec={"generation": generation_spec(4, 36_504)})
    assert repo.progress(identity)["input_cursor"] == 1000


def test_failed_sqlite_batch_rolls_back_and_can_resume_without_duplicates(repo, tmp_path):
    import sqlite3

    path = names_file(tmp_path / "write_failure.txt", 2500)
    with repo.store.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_queue BEFORE INSERT ON roblox_queue "
            "WHEN NEW.position=1100 BEGIN SELECT RAISE(ABORT,'disk full'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="disk full"):
        repo.create_run(spec=input_spec(files=[path]))
    identity = repo.runs()[0]["id"]
    assert repo.progress(identity)["input_cursor"] == repo.progress(identity)["total"] == 1000
    assert repo.progress(identity)["state"] == "PreparationError"
    with repo.store.transaction() as db:
        db.execute("DROP TRIGGER fail_queue")
    repo.create_run(run_id=identity)
    assert repo.progress(identity)["total"] == 2500
