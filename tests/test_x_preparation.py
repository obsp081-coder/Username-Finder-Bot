"""Bounded X local preparation, crash recovery and UI measurements; no external HTTP."""

import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from test_reliability_stress import memory_bytes
from test_x import response
from test_x_ui import window as window

from username_finder import x
from username_finder.gui import MainWindow
from username_finder.storage import Store
from username_finder.x_storage import XStore


def write_five(path, count):
    with path.open("w", encoding="utf-8", newline="") as stream:
        if path.suffix == ".csv":
            stream.write("username,note\n")
        for index in range(count):
            stream.write(f"{index:05x}" + (",fixture" if path.suffix == ".csv" else "") + "\n")
    return path


def select_file(window, path):
    panel = window.x_panel
    panel.modes.setCurrentIndex(1)
    panel.files.addItem(str(path))
    panel._fit_mode()
    return panel


@pytest.mark.parametrize("count", [100, 10000, 31785, 100000])
@pytest.mark.parametrize("extension", ["txt", "csv"])
def test_x_preparation_scale(window, qtbot, tmp_path, count, extension):
    panel = select_file(window, write_five(tmp_path / f"five.{extension}", count))
    ticks = [time.perf_counter()]
    timer = QTimer(window)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start(20)
    before = memory_bytes()
    started = time.perf_counter()
    panel.overnight.prepare_button.click()
    try:
        qtbot.waitUntil(lambda: not panel.busy, timeout=30000)
    finally:
        timer.stop()
    seconds = time.perf_counter() - started
    run = panel.repo.progress(panel.current_run)
    assert run["cursor"] == run["total"] == run["pending"] == count
    assert run["state"] == "Ready" and run["completed"] == 0 and run["interval_seconds"] == 5
    assert not panel.test_clock.calls
    rows = window.store.db.execute(
        "SELECT position,username FROM x_queue WHERE run_id=? ORDER BY position", (panel.current_run,)
    )
    for position, row in enumerate(rows, 1):
        assert tuple(row) == (position, f"{position - 1:05x}")
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert max(gaps, default=0) < 1
    assert count < 10000 or len(ticks) > 2
    print(
        "X_PREPARATION_SCALE="
        + json.dumps(
            dict(
                count=count,
                extension=extension,
                seconds=round(seconds, 4),
                max_gap=round(max(gaps, default=0), 4),
                ticks=len(ticks) - 1,
                memory_before=before,
                memory_after=memory_bytes(),
            )
        )
    )


def test_batch_filtering_order_checkpoint_and_frozen_settings(tmp_path):
    store = Store(tmp_path / "filter.db")
    repo = XStore(store)
    try:
        old = repo.prepare(x.input_spec("known"))
        task = repo.claim(old)
        repo.finish(task, x.result_for("known", response()))
        evidence = repo.history("known")
        repo.end(old, "Completed")
        path = write_five(tmp_path / "filter.txt", 31785)
        with path.open("a", encoding="utf-8") as stream:
            stream.write("00000\n00000\nKNOWN\nbad.name\nbad.name\naaaa\n\n")
        cancel = threading.Event()
        statements = []
        store.db.set_trace_callback(statements.append)

        def notice(identity):
            if repo.preparation_progress(identity)["cursor"] >= 1000:
                cancel.set()

        identity = repo.prepare(x.input_spec(files=[path]), cancel=cancel, notify=notice)
        paused = repo.progress(identity)
        assert paused["state"] == "PreparationPaused" and paused["cursor"] == paused["total"] == 1000
        assert sum("FROM x_names WHERE username_key IN" in sql for sql in statements) == 3
        assert sum(sql == "COMMIT" for sql in statements) <= 5
        repo.set_interval(0)
        repo.prepare(run_id=identity)
        run = repo.progress(identity)
        assert run["state"] == "Ready" and run["cursor"] == 31792
        assert run["total"] == 31785 and run["duplicates"] == 3 and run["invalid"] == 2
        assert run["skipped"] == run["blank"] == 1 and run["interval_seconds"] == 5
        assert repo.history("known") == evidence
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        store.close()


def test_active_progress_cannot_show_old_job_or_recount_queue(window, qtbot, monkeypatch):
    panel = window.x_panel
    old = panel.repo.prepare(x.input_spec("older"))
    panel.repo.end(old, "Completed")
    panel.current_run = old
    panel.refresh()
    panel.quantity.setText("10000")
    panel.minimum_length.setValue(5)
    panel.maximum_length.setValue(5)
    entered = threading.Event()
    original = panel.preparation_notice

    def notice(identity):
        original(identity)
        if panel.repo.preparation_progress(identity)["cursor"] >= 1000:
            entered.set()
            panel.cancel.wait(5)

    monkeypatch.setattr(panel, "preparation_notice", notice)
    panel.prepare()
    qtbot.waitUntil(entered.is_set)
    qtbot.waitUntil(lambda: panel.current_run != old)
    assert "قُرئ:" in panel.activity_status.text() and "مكتملة" not in panel.activity_status.text()
    original_progress = panel.repo.progress
    active = panel.current_run

    def progress(identity):
        if identity == active:
            pytest.fail("No queue scan during local preparation")
        return original_progress(identity)

    monkeypatch.setattr(panel.repo, "progress", progress)
    try:
        panel.refresh()
        panel.controls()
        window.job_manager.refresh()
        row = next(r for r in window.job_manager.rows if r["id"] == panel.current_run)
        assert row["total"] is None and row["cursor"] >= 1000
    finally:
        monkeypatch.setattr(panel.repo, "progress", original_progress)
        panel.stop()
    at = time.perf_counter()
    qtbot.waitUntil(lambda: not panel.busy, timeout=1000)
    assert time.perf_counter() - at < 1
    assert panel.repo.progress(panel.current_run)["state"] == "PreparationPaused"
    monkeypatch.setattr(panel, "preparation_notice", original)
    panel.resume()
    qtbot.waitUntil(lambda: not panel.busy, timeout=10000)
    assert panel.repo.progress(panel.current_run)["state"] == "Ready"
    assert panel.repo.progress(panel.current_run)["total"] == 10000 and not panel.test_clock.calls


def test_preparation_first_notice_for_each_job_is_visible(window, monkeypatch):
    panel = window.x_panel
    monkeypatch.setattr("username_finder.x_ui.time", SimpleNamespace(monotonic=lambda: 100.0))
    first, second = [panel.repo.prepare(x.input_spec(name)) for name in ("first", "other")]
    panel.preparing = True
    try:
        panel.preparation_notice(first)
        panel.preparation_notice(second)
        assert panel.current_run == second
    finally:
        panel.preparing = False


@pytest.mark.parametrize("phase", ["committed", "uncommitted"])
def test_process_termination_recovers_only_committed_x_batches(tmp_path, phase):
    path = write_five(tmp_path / "crash.txt", 4000)
    database = tmp_path / "crash.db"
    script = r"""
import os,sys
from types import SimpleNamespace
from username_finder import x,x_storage
from username_finder.x_storage import XStore
from username_finder.storage import Store
store=Store(sys.argv[1]);repo=XStore(store)
x_storage.uuid4=lambda:SimpleNamespace(hex='XCrash')
def trace(sql):
    if sql.startswith('INSERT INTO x_queue') and ',1100,' in sql:os._exit(29)
if sys.argv[3]=='uncommitted':store.db.set_trace_callback(trace)
def notify(identity):
    if sys.argv[3]=='committed' and repo.preparation_progress(identity)['cursor']==1000:os._exit(29)
repo.prepare(x.input_spec(files=[sys.argv[2]]),notify=notify)
"""
    child = subprocess.run(
        [sys.executable, "-B", "-c", script, str(database), str(path), phase], capture_output=True, timeout=15
    )
    assert child.returncode == 29, child.stderr
    store = Store(database)
    try:
        repo = XStore(store)
        repo.recover()
        assert repo.progress("XCrash")["state"] == "PreparationPaused"
        assert repo.progress("XCrash")["cursor"] == repo.progress("XCrash")["total"] == 1000
        repo.prepare(run_id="XCrash")
        assert repo.progress("XCrash")["total"] == repo.progress("XCrash")["cursor"] == 4000
        assert repo.progress("XCrash")["state"] == "Ready" and not repo.results()[1]
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        store.close()


def test_source_failure_keeps_committed_prefix_and_can_never_be_ready(tmp_path):
    store = Store(tmp_path / "bad.db")
    repo = XStore(store)
    path = write_five(tmp_path / "bad.txt", 1000)
    with path.open("ab") as stream:
        stream.write(b"\xff\n")
    try:
        with pytest.raises(ValueError):
            repo.prepare(x.input_spec(files=[path]))
        identity = repo.runs()[0]["id"]
        assert repo.progress(identity)["state"] == "PreparationError"
        assert repo.results()[1] == 0
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["disk", "changed", "deleted"])
def test_failed_resume_preserves_committed_candidates_and_reports_error(tmp_path, monkeypatch, failure):
    store = Store(tmp_path / "resume-error.db")
    repo = XStore(store)
    path = write_five(tmp_path / "resume.txt", 4000)
    cancel = threading.Event()

    def notify(identity):
        if repo.preparation_progress(identity)["cursor"] == 1000:
            cancel.set()

    try:
        identity = repo.prepare(x.input_spec(files=[path]), cancel=cancel, notify=notify)
        original = [tuple(r) for r in store.db.execute("SELECT * FROM x_queue ORDER BY position")]
        if failure == "disk":
            monkeypatch.setattr(
                "username_finder.x_storage.shutil.disk_usage", lambda _: SimpleNamespace(free=0)
            )
        elif failure == "changed":
            path.write_text("changed", encoding="utf-8")
        else:
            path.unlink()  # Only the test's temporary source.
        with pytest.raises(ValueError):
            repo.prepare(run_id=identity)
        assert repo.progress(identity)["state"] == "PreparationError"
        assert [tuple(r) for r in store.db.execute("SELECT * FROM x_queue ORDER BY position")] == original
        assert repo.progress(identity)["cursor"] == 1000
    finally:
        store.close()


def test_gui_shutdown_during_preparation_reopens_without_checks(qtbot, tmp_path, monkeypatch):
    database = tmp_path / "close.db"
    ui = MainWindow(database)
    qtbot.addWidget(ui)
    panel = select_file(ui, write_five(tmp_path / "close.txt", 4000))
    entered = threading.Event()
    original = panel.preparation_notice

    def notice(identity):
        original(identity)
        if panel.repo.preparation_progress(identity)["cursor"] == 1000:
            entered.set()
            panel.cancel.wait(5)

    monkeypatch.setattr(panel, "preparation_notice", notice)
    ui.show()
    try:
        panel.overnight.enabled.setChecked(True)
        panel.prepare()
        qtbot.waitUntil(entered.is_set)
        identity = panel.current_run
    finally:
        ui.close()
        qtbot.waitUntil(lambda: ui.closed, timeout=5000)
    reopened = MainWindow(database)
    qtbot.addWidget(reopened)
    reopened.x_panel.fetcher = lambda *args: pytest.fail("Preparation recovery must not send HTTP")
    try:
        reopened.show()
        qtbot.wait(30)
        panel = reopened.x_panel
        assert panel.repo.progress(identity)["state"] == "PreparationPaused"
        assert not panel.busy and not panel.overnight.repo.startup_candidates(panel.repo)
        panel.current_run = identity
        panel.resume()
        qtbot.waitUntil(lambda: not panel.busy, timeout=5000)
        assert panel.repo.progress(identity)["state"] == "Ready"
        assert panel.repo.progress(identity)["total"] == 4000 and panel.repo.results()[1] == 0
        assert not panel.overnight.repo.startup_candidates(panel.repo)
    finally:
        reopened.close()
        qtbot.waitUntil(lambda: reopened.closed, timeout=5000)


def test_native_x_preparation_layout(window, qtbot, tmp_path, monkeypatch):
    panel = select_file(window, write_five(tmp_path / "layout.txt", 31785))
    entered = threading.Event()
    original = panel.preparation_notice

    def notice(identity):
        original(identity)
        if panel.repo.preparation_progress(identity)["cursor"] == 1000:
            panel.preparation_progress.emit(panel.repo.preparation_progress(identity))
            entered.set()
            panel.cancel.wait(10)

    monkeypatch.setattr(panel, "preparation_notice", notice)
    panel.prepare()
    qtbot.waitUntil(entered.is_set)
    window.tabs.setCurrentWidget(panel)
    try:
        for width, height in ((900, 650), (1180, 850), (1500, 1000)):
            window.resize(width, height)
            panel.workspace.setCurrentIndex(2)
            qtbot.wait(30)
            page = panel.workspace.currentWidget()
            assert page.horizontalScrollBar().maximum() == 0
            assert window.width() == width and window.height() == height
            assert panel.layoutDirection() == Qt.LayoutDirection.RightToLeft
            for control in (panel.start_button, panel.stop_button, panel.activity_status):
                assert (
                    panel.viewport()
                    .rect()
                    .contains(QRect(control.mapTo(panel.viewport(), QPoint()), control.size()))
                )
            if os.environ.get("UFB_SCREENSHOTS"):
                screenshot = tmp_path / f"x-preparing-{width}.png"
                window.grab().save(str(screenshot))
                print(f"X_GUI_DPR={window.devicePixelRatioF()} SCREENSHOT={screenshot}")
    finally:
        panel.stop()
        qtbot.waitUntil(lambda: not panel.busy)
