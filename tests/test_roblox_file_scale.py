"""Large local TXT/CSV workflows; no external platform requests."""

import itertools
import json
import os
import string
import threading
import time
from types import SimpleNamespace

import pytest
from legacy_roblox_shell import MainWindow
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtWidgets import QMessageBox
from test_multi_jobs import concurrent_window as concurrent_window
from test_reliability_stress import memory_bytes
from test_roblox import response
from test_x import Clock

from username_finder import roblox_ui
from username_finder.overnight_preflight_ui import OvernightPreflightDialog
from username_finder.roblox import input_spec
from username_finder.roblox_runner import RobloxRunner
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store

FOUR_COUNT = 1_772_928


def four_names():
    alphabet = string.ascii_lowercase + string.digits
    for first in alphabet:
        for middle1 in alphabet + "_":
            for middle2 in alphabet + "_":
                if middle1 == middle2 == "_":
                    continue
                for last in alphabet:
                    yield first + middle1 + middle2 + last


def write_names(path, count):
    with path.open("w", encoding="utf-8", newline="") as stream:
        if path.suffix == ".csv":
            stream.write("username\n")
        names = iter(itertools.islice(four_names(), count))
        while batch := list(itertools.islice(names, 10000)):
            stream.writelines(name + "\n" for name in batch)
    return path


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    ui = MainWindow(tmp_path / "scale.db")
    ui.roblox.test_calls = []
    ui.roblox.fetcher = lambda url: (ui.roblox.test_calls.append(url), response())[1]
    qtbot.addWidget(ui)
    ui.show()
    yield ui
    if not ui.closed:
        ui.roblox.stop()
        qtbot.waitUntil(lambda: not ui.roblox.busy, timeout=10000)
        ui.close()
        qtbot.waitUntil(lambda: ui.closed, timeout=10000)


def select_file(window, path):
    panel = window.roblox
    panel.modes.setCurrentIndex(1)
    panel.files.addItem(str(path))
    panel.files.setVisible(True)
    panel._fit_mode()
    window.tabs.setCurrentWidget(panel)
    return panel


def test_preview_reads_a_bounded_sample_instead_of_the_whole_file(window, qtbot, tmp_path, monkeypatch):
    panel = select_file(window, write_names(tmp_path / "preview.txt", 10000))
    lookups = []
    original = panel.repo.is_known
    monkeypatch.setattr(panel.repo, "is_known", lambda key: lookups.append(key) or original(key))
    panel.preview()
    qtbot.waitUntil(lambda: not panel.busy)
    assert len(lookups) <= 20
    assert "عينة" in panel.preview_label.text()
    assert not panel.repo.runs() and not panel.test_calls


def test_preparation_progress_does_not_recount_queue_on_gui_thread(window, monkeypatch):
    panel = window.roblox
    calls = []
    original = panel.repo.progress
    monkeypatch.setattr(panel.repo, "progress", lambda identity: calls.append(identity) or original(identity))
    panel.preparing = panel.busy = True
    try:
        # Progress snapshots must be cheap even as the durable queue grows.
        panel.preparation_updated(
            {
                "id": "local-job",
                "input_cursor": 100000,
                "duplicates": 0,
                "invalid": 0,
                "blank": 0,
                "skipped": 0,
                "state": "Preparing",
            }
        )
        assert not calls
    finally:
        panel.preparing = panel.busy = False
        panel.current_run = None


def test_preparation_history_lookup_is_batched(tmp_path):
    store = Store(tmp_path / "batch.db")
    repo = RobloxStore(store)
    statements = []
    store.db.set_trace_callback(statements.append)
    try:
        identity = repo.create_run(spec=input_spec(files=[write_names(tmp_path / "batch.txt", 1000)]))
        lookups = [sql for sql in statements if "FROM roblox_names WHERE username_key" in sql]
        assert len(lookups) <= 4
        assert repo.progress(identity)["total"] == 1000
    finally:
        store.close()


def test_first_progress_for_a_new_job_is_never_throttled_as_the_previous_job(window, monkeypatch):
    panel = window.roblox
    monkeypatch.setattr(roblox_ui, "time", SimpleNamespace(monotonic=lambda: 100.0))
    first = panel.repo.create_run(["FirstLocal"])
    second = panel.repo.create_run(["SecondLocal"])
    panel.preparation_notice(first)
    panel.preparation_notice(second)
    assert panel.current_run == second


def test_resuming_local_preparation_never_starts_http(window, qtbot, tmp_path, monkeypatch):
    panel = window.roblox
    cancel = threading.Event()
    identity = panel.repo.create_run(
        spec=input_spec(files=[write_names(tmp_path / "paused.txt", 2000)]),
        cancel=cancel,
        notify=lambda _: cancel.set(),
    )
    panel.current_run = identity
    clock = Clock()
    monkeypatch.setattr(
        roblox_ui, "RobloxRunner", lambda *a, **kw: RobloxRunner(*a, clock=clock.now, wait=clock.wait, **kw)
    )
    panel.resume()
    qtbot.waitUntil(lambda: not panel.busy, timeout=10000)
    assert panel.repo.progress(identity)["state"] == "Ready"
    assert not panel.test_calls


def test_x_continues_while_large_roblox_preparation_is_cancelled_and_resumed(
    concurrent_window, qtbot, tmp_path, monkeypatch
):
    window, events, entered, calls, ids = concurrent_window
    window.x_panel._start_run(ids["x"])
    qtbot.waitUntil(entered["x"].is_set)
    panel = select_file(window, write_names(tmp_path / "concurrent.txt", 100000))
    preparing = threading.Event()
    original = panel.preparation_notice

    def notice(identity):
        original(identity)
        if not preparing.is_set():
            preparing.set()
            panel.preparation_cancel.wait(5)

    monkeypatch.setattr(panel, "preparation_notice", notice)
    panel.prepare(save_job=True)
    qtbot.waitUntil(preparing.is_set)
    events["x"].set()
    qtbot.waitUntil(lambda: not window.x_panel.busy)
    assert panel.busy and window.x_panel.repo.results()[1] == 2
    window.job_manager.refresh()
    preparing_row = next(
        row for row in window.job_manager.rows if row["platform"] == "Roblox" and row["state"] == "Preparing"
    )
    assert preparing_row["total"] is None
    panel.stop()
    qtbot.waitUntil(lambda: not panel.busy)
    assert panel.repo.progress(panel.current_run)["state"] == "PreparationPaused"
    panel.resume()
    qtbot.waitUntil(lambda: not panel.busy, timeout=10000)
    assert panel.repo.progress(panel.current_run)["total"] == 100000
    assert panel.repo.progress(panel.current_run)["state"] == "Ready" and not calls["roblox"]


def test_preview_and_preparation_layout_at_supported_windows_sizes(window, qtbot, tmp_path, monkeypatch):
    panel = select_file(window, write_names(tmp_path / "layout.txt", 10000))
    panel.preview()
    qtbot.waitUntil(lambda: not panel.busy)
    assert "عينة" in panel.preview_label.text()
    for width, height in ((900, 650), (1180, 850), (1500, 1000)):
        window.resize(width, height)
        panel.workspace.setCurrentIndex(0)
        page = panel.workspace.currentWidget()
        page.ensureWidgetVisible(panel.preview_label)
        qtbot.wait(20)
        assert window.width() <= width and window.height() <= height
        assert page.horizontalScrollBar().maximum() == 0
        assert panel.layoutDirection() == Qt.LayoutDirection.RightToLeft
        for control in (panel.start_button, panel.stop_button, panel.activity_status):
            assert (
                panel.viewport()
                .rect()
                .contains(QRect(control.mapTo(panel.viewport(), QPoint()), control.size()))
            )
        if os.environ.get("UFB_SCREENSHOTS"):
            path = tmp_path / f"file-preview-{width}.png"
            window.grab().save(str(path))
            print(f"FILE_GUI_DPR={window.devicePixelRatioF()} SCREENSHOT={path}")
    entered = threading.Event()
    original = panel.preparation_notice

    def notice(identity):
        original(identity)
        entered.set()
        panel.preparation_cancel.wait(5)

    monkeypatch.setattr(panel, "preparation_notice", notice)
    panel.prepare(save_job=True)
    qtbot.waitUntil(entered.is_set)
    qtbot.waitUntil(lambda: bool(getattr(panel, "_preparation_snapshot", None)))
    window.tabs.setCurrentWidget(window.job_manager)
    for width, height in ((900, 650), (1180, 850), (1500, 1000)):
        window.resize(width, height)
        window.job_manager.refresh()
        qtbot.wait(20)
        assert any(row["total"] is None for row in window.job_manager.rows)
        assert window.width() <= width and window.height() <= height
        if os.environ.get("UFB_SCREENSHOTS"):
            path = tmp_path / f"file-preparing-manager-{width}.png"
            window.grab().save(str(path))
            print(f"FILE_GUI_DPR={window.devicePixelRatioF()} SCREENSHOT={path}")
    panel.stop()
    qtbot.waitUntil(lambda: not panel.busy)


@pytest.mark.parametrize("count", [100, 10000, 100000, FOUR_COUNT])
@pytest.mark.parametrize("extension", ["txt", "csv"])
def test_gui_preview_and_durable_preparation_at_scale(window, qtbot, tmp_path, monkeypatch, count, extension):
    if count == FOUR_COUNT and not os.environ.get("UFB_LARGE_FILES"):
        pytest.skip("Explicit 1.77-million-name local scale run only")
    path = write_names(tmp_path / f"four.{extension}", count)
    panel = select_file(window, path)
    if count == FOUR_COUNT:
        panel.speed.setCurrentIndex(panel.speed.findData(0))
    ticks = [time.perf_counter()]
    timer = QTimer(window)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start(20)
    memory_before = memory_bytes()
    at = time.perf_counter()
    panel.preview()
    qtbot.waitUntil(lambda: not panel.busy, timeout=10000)
    preview_seconds = time.perf_counter() - at
    assert not panel.test_calls and not panel.repo.runs()
    panel.overnight.enabled.setChecked(True)
    at = time.perf_counter()
    panel.overnight.prepare_button.click()
    try:
        qtbot.waitUntil(lambda: not panel.busy, timeout=240000)
    finally:
        timer.stop()
    prepare_seconds = time.perf_counter() - at
    progress = panel.repo.progress(panel.current_run)
    assert progress["state"] == "Ready"
    assert progress["input_cursor"] == progress["total"] == count
    assert progress["counts"] == {"Pending": count}
    assert not panel.test_calls and panel.repo.results()[1] == 0
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    print(
        "ROBLOX_FILE_SCALE="
        + json.dumps(
            {
                "count": count,
                "source_bytes": path.stat().st_size,
                "preview_seconds": round(preview_seconds, 4),
                "prepare_seconds": round(prepare_seconds, 4),
                "gui_ticks": len(ticks) - 1,
                "max_gap_seconds": round(max(gaps, default=0), 4),
                "memory_before": memory_before,
                "memory_after": memory_bytes(),
                "database_bytes": window.store.path.stat().st_size,
            }
        )
    )
    assert ticks and (count < 10000 or len(ticks) >= 3)
    assert max(gaps, default=0) < 1.0
    assert window.store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    rows = window.store.db.execute(
        "SELECT position,username FROM roblox_queue WHERE run_id=? ORDER BY position", (panel.current_run,)
    )
    expected = iter(itertools.islice(four_names(), count))
    position = 0
    while batch := rows.fetchmany(1000):
        for row in batch:
            position += 1
            assert row[0] == position and row[1] == next(expected)
    assert position == count
    if count == FOUR_COUNT:
        preflight = OvernightPreflightDialog(window.job_manager)
        qtbot.addWidget(preflight)
        preflight.check_now()
        qtbot.waitUntil(preflight.check_button.isEnabled)
        assert len(preflight.results) == 1 and not preflight.results[0].errors
        preflight.close()
        clock, held, release = Clock(), threading.Event(), threading.Event()
        calls = []

        def fetch(url):
            calls.append(url)
            clock.seconds += 0.05
            if len(calls) == 1:
                raise TimeoutError
            if len(calls) == 3:
                held.set()
                assert release.wait(10)
            return response()

        monkeypatch.setattr(
            roblox_ui,
            "RobloxRunner",
            lambda *a, **kw: RobloxRunner(*a, clock=clock.now, wait=clock.wait, **kw),
        )
        panel.fetcher = fetch
        identity = panel.current_run
        panel._start_run(identity)
        try:
            qtbot.waitUntil(held.is_set, timeout=15000)
            panel.stop()
        finally:
            release.set()
        qtbot.waitUntil(lambda: not panel.busy, timeout=10000)
        assert len(calls) == 3 and len(set(calls)) == 2
        assert panel.repo.results()[1] == 2 and clock.seconds >= 30
        assert panel.repo.progress(identity)["interval_seconds"] == 0
        assert panel.overnight.repo.get(identity)["state"] == "Stopped"
        path_db = window.store.path
        window.close()
        qtbot.waitUntil(lambda: window.closed)
        reopened = MainWindow(path_db)
        qtbot.addWidget(reopened)
        reopened.roblox.fetcher = lambda _: pytest.fail("Stopped large job must not restart")
        reopened.show()
        qtbot.wait(50)
        saved = reopened.roblox.repo.progress(identity)
        assert saved["total"] == count and saved["counts"] == {"Done": 2, "Pending": count - 2}
        assert not reopened.roblox.overnight.repo.startup_candidates(reopened.roblox.repo)
        reopened.close()
        qtbot.waitUntil(lambda: reopened.closed)
