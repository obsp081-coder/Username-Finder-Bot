import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_roblox_file_scale import window as window
from test_roblox_file_scale import write_names

from username_finder import roblox, roblox_storage
from username_finder.gui import MainWindow
from username_finder.roblox import input_spec, prepare_inputs
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store


@pytest.fixture
def repo(tmp_path):
    store = Store(tmp_path / "failure.db")
    yield RobloxStore(store)
    store.close()


@pytest.mark.parametrize("extension", ["txt", "csv"])
def test_preview_is_explicitly_partial_and_full_import_rejects_bad_tail(repo, tmp_path, extension):
    path = write_names(tmp_path / f"bad.{extension}", 2500)
    with path.open("ab") as stream:
        stream.write(b"\xff" if extension == "txt" else b'"unterminated')
    sample = prepare_inputs(files=[path], preview=True)
    assert not sample.complete and len(sample.names) == sample.scanned == 20
    with pytest.raises(ValueError):
        repo.create_run(spec=input_spec(files=[path]))
    saved = repo.runs()[0]
    assert saved["state"] == "PreparationError" and not repo.start(saved["id"])
    assert repo.results()[1] == 0


def test_duplicate_or_invalid_only_preview_is_bounded(tmp_path):
    path = tmp_path / "duplicates.txt"
    path.write_text("invalid-name\n" * 10000, encoding="utf-8")
    sample = prepare_inputs(files=[path], preview=True)
    assert not sample.complete and sample.scanned == 2000 and not sample.names
    assert sample.invalid_count == 1 and sample.duplicates == 1999
    path.write_text(("x" * 100000 + "\n") * 10, encoding="utf-8")
    sample = prepare_inputs(files=[path], preview=True)
    assert not sample.complete and sample.scanned == 1


@pytest.mark.parametrize("content", [b"", b"\n\n", b"bad-name\n", "aaaa".encode("utf-16"), b"\xff"])
def test_empty_invalid_and_unsupported_encoding_never_become_ready(repo, tmp_path, content):
    path = tmp_path / "empty.txt"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        repo.create_run(spec=input_spec(files=[path]))
    saved = repo.runs()[0]
    assert saved["state"] == "PreparationError" and not repo.start(saved["id"])


def test_changed_source_during_partial_preview_is_reported(tmp_path):
    path = write_names(tmp_path / "change.txt", 100)
    spec = input_spec(files=[path])

    def known(_):
        with path.open("a", encoding="utf-8") as stream:
            stream.write("zzzz\n")
        return False

    with pytest.raises(ValueError, match="تغير"):
        prepare_inputs(spec=spec, known=known, preview=True)


@pytest.mark.parametrize("failure", ["changed", "deleted", "disk", "locked"])
def test_failure_after_first_batch_preserves_checkpoint_and_blocks_checks(
    repo, tmp_path, monkeypatch, failure
):
    path = write_names(tmp_path / "partial.txt", 2500)
    missing = threading.Event()
    original_stat = Path.stat
    original_disk = roblox_storage.shutil.disk_usage
    external = sqlite3.connect(repo.store.path)
    repo.store.db.execute("PRAGMA busy_timeout=20")

    def stat(target, *args, **kwargs):
        if target == path and missing.is_set():
            raise FileNotFoundError("simulated source disappearance")
        return original_stat(target, *args, **kwargs)

    if failure == "deleted":
        monkeypatch.setattr(Path, "stat", stat)

    def notify(_):
        if missing.is_set():
            return
        missing.set()
        if failure == "changed":
            with path.open("a", encoding="utf-8") as stream:
                stream.write("zzzz\n")
        elif failure == "disk":
            monkeypatch.setattr(
                roblox_storage.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 0})()
            )
        elif failure == "locked":
            external.execute("BEGIN IMMEDIATE")

    try:
        with pytest.raises((ValueError, sqlite3.OperationalError)):
            repo.create_run(spec=input_spec(files=[path]), notify=notify)
    finally:
        external.rollback()
        external.close()
        missing.clear()
        monkeypatch.setattr(roblox_storage.shutil, "disk_usage", original_disk)
    identity = repo.runs()[0]["id"]
    before = repo.progress(identity)
    assert 1000 <= before["input_cursor"] <= 2000
    assert before["input_cursor"] == before["total"] and before["state"] != "Ready"
    repo.recover()
    assert not repo.start(identity)
    if failure != "changed":
        repo.create_run(run_id=identity)
        assert repo.progress(identity)["total"] == 2500
    else:
        with pytest.raises(ValueError):
            repo.create_run(run_id=identity)
        assert repo.progress(identity)["input_cursor"] == before["input_cursor"]
    assert repo.store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_cancelled_source_is_closed_and_missing_file_cannot_resume(repo, tmp_path):
    path = write_names(tmp_path / "removed.txt", 2500)
    cancel = threading.Event()
    identity = repo.create_run(spec=input_spec(files=[path]), cancel=cancel, notify=lambda _: cancel.set())
    path.unlink()  # Only a temporary test input; also verifies the file handle was released.
    with pytest.raises((ValueError, OSError)):
        repo.create_run(run_id=identity)
    assert repo.progress(identity)["input_cursor"] == 1000
    assert not repo.start(identity)


def test_missing_source_has_a_clear_local_error(tmp_path):
    with pytest.raises(ValueError, match="تعذر الوصول"):
        input_spec(files=[tmp_path / "missing.txt"])


def test_preview_cancel_is_responsive_and_does_not_stop_an_older_job(window, qtbot, monkeypatch):
    panel = window.roblox
    identity = panel.repo.create_run(["OldSavedName"])
    panel.current_run = identity
    panel.overnight.repo.enable(identity, True)
    before = panel.overnight.repo.get(identity)
    entered = threading.Event()

    def slow(_):
        for index in range(10000):
            if index == 40:
                entered.set()
            time.sleep(0.001)
            yield "invalid-name\n"

    monkeypatch.setattr(roblox, "input_values", slow)
    panel.modes.setCurrentIndex(1)
    panel.manual.setPlainText("ignored")
    panel.preview()
    qtbot.waitUntil(entered.is_set)
    panel.stop()
    qtbot.waitUntil(lambda: not panel.busy, timeout=1000)
    assert panel.overnight.repo.get(identity) == before
    assert not panel.test_calls and "أُلغيت" in panel.status.text()


def test_shutdown_during_local_preparation_reopens_without_http(window, qtbot, tmp_path, monkeypatch):
    panel = window.roblox
    path = write_names(tmp_path / "shutdown.txt", 4000)
    panel.modes.setCurrentIndex(1)
    panel.files.addItem(str(path))
    entered = threading.Event()
    original = panel.preparation_notice

    def notice(identity):
        original(identity)
        entered.set()
        panel.preparation_cancel.wait(5)

    monkeypatch.setattr(panel, "preparation_notice", notice)
    panel.overnight.prepare_button.click()
    qtbot.waitUntil(entered.is_set)
    database = window.store.path
    window.close()
    qtbot.waitUntil(lambda: window.closed)
    reopened = MainWindow(database)
    qtbot.addWidget(reopened)
    reopened.roblox.fetcher = lambda _: pytest.fail("Local recovery must not send HTTP")
    reopened.show()
    identity = reopened.roblox.repo.runs()[0]["id"]
    saved = reopened.roblox.repo.progress(identity)
    assert saved["state"] == "PreparationPaused" and saved["input_cursor"] == 1000
    reopened.roblox.current_run = identity
    reopened.roblox.resume()
    qtbot.waitUntil(lambda: not reopened.roblox.busy)
    assert reopened.roblox.repo.progress(identity)["state"] == "Ready"
    assert reopened.roblox.repo.results()[1] == 0
    reopened.close()
    qtbot.waitUntil(lambda: reopened.closed)


@pytest.mark.parametrize("phase", ["committed", "uncommitted"])
def test_abrupt_preparation_exit_recovers_exact_batches(tmp_path, phase):
    path = write_names(tmp_path / "crash.txt", 4000)
    database = tmp_path / "crash.db"
    script = r"""
import os,sys
from types import SimpleNamespace
from username_finder import roblox_storage
from username_finder.roblox import input_spec
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store
store=Store(sys.argv[1]);repo=RobloxStore(store)
roblox_storage.uuid4=lambda:SimpleNamespace(hex='PrepCrash')
def trace(sql):
    if sql.startswith('INSERT INTO roblox_queue') and ',1100,' in sql:os._exit(29)
if sys.argv[3]=='uncommitted':store.db.set_trace_callback(trace)
def notify(_):
    if sys.argv[3]=='committed':os._exit(29)
repo.create_run(spec=input_spec(files=[sys.argv[2]]),notify=notify)
"""
    child = subprocess.run(
        [sys.executable, "-B", "-c", script, str(database), str(path), phase], capture_output=True, timeout=15
    )
    assert child.returncode == 29, child.stderr
    store = Store(database)
    try:
        repo = RobloxStore(store)
        repo.recover()
        assert repo.progress("PrepCrash")["input_cursor"] == repo.progress("PrepCrash")["total"] == 1000
        repo.create_run(run_id="PrepCrash")
        saved = repo.progress("PrepCrash")
        assert saved["state"] == "Ready" and saved["total"] == saved["input_cursor"] == 4000
        assert repo.results()[1] == 0 and not store.db.execute("PRAGMA foreign_key_check").fetchall()
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        store.close()
