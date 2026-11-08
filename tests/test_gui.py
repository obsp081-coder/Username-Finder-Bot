import os
from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtWidgets import QMessageBox

from username_finder.domain import Filters, JobState, SearchConfig
from username_finder.gui import MainWindow
from username_finder.scheduling import ScheduleConfig, Scheduler
from username_finder.sources import make_snapshot


@pytest.fixture(autouse=True)
def discard_editor_draft_on_workflow_test_close(monkeypatch):
    # These existing workflow cases are not preset tests; close their edited drafts deliberately.
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda self: QMessageBox.StandardButton.Discard)


def test_gui_all_tabs_create_start_results_and_reload(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path / "gui.db")
    qtbot.addWidget(window)
    window.show()
    assert [window.search_tabs.tabText(i) for i in range(4)] == ["الإعدادات", "التشغيل", "النتائج", "السجل"]
    assert all(key in check.text() for key, check in window.specials.items())
    window.max_length.setValue(3)
    window.prefix.setText("a")
    window.limit.setText("20")
    for check in window.specials.values():
        check.setChecked(True)
    monkeypatch.setattr(window, "ask", lambda *a, **k: QMessageBox.StandardButton.Ok)
    window.prepare_job()
    qtbot.waitUntil(lambda: window.current_job is not None, timeout=5000)
    assert window.tabs.currentWidget() is window.snapchat_tabs
    assert window.snapchat_tabs.currentWidget() is window.search_tabs
    assert window.search_tabs.currentIndex() == 1
    window.start_engine()
    qtbot.waitUntil(lambda: not window.is_running(), timeout=10000)
    qtbot.wait(30)
    assert window.store.job(window.current_job)["state"] == JobState.COMPLETED
    assert window.result_table.rowCount() == 20
    assert window.history_table.rowCount() == 20
    assert window.progress.value() == 1000
    if os.environ.get("UFB_SCREENSHOTS"):
        evidence = tmp_path / "screenshots"
        evidence.mkdir(exist_ok=True)
        for i, name in enumerate(["settings", "execution", "results", "history"]):
            window.search_tabs.setCurrentIndex(i)
            qtbot.wait(60)
            window.grab().save(str(evidence / f"{name}.png"))
        print("GUI_SCREENSHOTS=" + str(evidence))
    job_id = window.current_job
    window.close()
    reopened = MainWindow(tmp_path / "gui.db")
    qtbot.addWidget(reopened)
    reopened.load_job(job_id)
    assert reopened.result_table.rowCount() == 20
    reopened.close()


def test_default_manager_with_empty_history_and_restored_job(qtbot, tmp_path):
    path = tmp_path / "landing.db"
    window = MainWindow(path)
    qtbot.addWidget(window)
    window.show()
    assert window.tabs.currentWidget() is window.job_manager
    assert window.job_manager.empty.isVisible()
    assert not window.job_manager.start_button.isEnabled()
    config = SearchConfig(filters=Filters(3, 3), limit=5)
    job = window.store.create_job(config, make_snapshot(config))
    window.close()
    qtbot.waitUntil(lambda: window.closed)
    reopened = MainWindow(path)
    qtbot.addWidget(reopened)
    assert reopened.current_job == job
    assert reopened.tabs.currentWidget() is reopened.job_manager
    assert not reopened.is_running()
    reopened.close()


def test_gui_pause_resume_and_close_saves(qtbot, tmp_path):
    window = MainWindow(tmp_path / "gui.db")
    qtbot.addWidget(window)
    config = SearchConfig(filters=Filters(3, 3), limit=200, workers=1, queue_size=10)
    job = window.store.create_job(config, make_snapshot(config))
    window.load_job(job)
    window.start_engine()
    qtbot.waitUntil(lambda: window.store.job(job)["state"] == JobState.RUNNING)
    with qtbot.waitSignal(window.bridge.finished, timeout=5000):
        window.pause_engine()
    qtbot.waitUntil(lambda: not window.is_running())
    assert window.store.job(job)["state"] == JobState.PAUSED
    window.start_engine()
    qtbot.waitUntil(window.is_running)
    window.close()
    # The window is initially hidden; visibility cannot prove that shutdown drained.
    qtbot.waitUntil(lambda: window.closed, timeout=5000)


def test_gui_settings_read_and_schedule_independence(qtbot, tmp_path):
    window = MainWindow(tmp_path / "gui.db")
    qtbot.addWidget(window)
    window.daily_stop.setChecked(True)
    assert window.read_schedule().stop_enabled
    assert not window.read_schedule().start_enabled
    window.duration.setChecked(True)
    window.duration_hours.setValue(3)
    window.duration_minutes.setValue(30)
    assert window.read_schedule().duration_minutes == 210
    window.workers.setValue(1000)
    assert window.read_config().workers == 1000
    window.close()


def test_daily_schedule_starts_and_stops_loaded_job(qtbot, tmp_path, monkeypatch):
    import username_finder.gui as gui

    wall = datetime(2026, 11, 5, 5, 59, 59, tzinfo=UTC)  # 08:59:59 in Riyadh

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall.astimezone(tz) if tz else wall.replace(tzinfo=None)

    monkeypatch.setattr(gui, "datetime", FakeDatetime)
    window = MainWindow(tmp_path / "schedule.db")
    qtbot.addWidget(window)
    config = SearchConfig(filters=Filters(3, 3), limit=500, workers=1)
    schedule = Scheduler(ScheduleConfig(start_enabled=True, stop_enabled=True, stop_time="09:01"))
    job = window.store.create_job(config, make_snapshot(config), schedule.serialize())
    window.load_job(job)
    wall += timedelta(seconds=1)
    window.tick()
    qtbot.waitUntil(lambda: window.store.job(job)["state"] == JobState.RUNNING)
    wall += timedelta(minutes=1)
    window.tick()
    qtbot.waitUntil(lambda: not window.is_running())
    assert window.store.job(job)["state"] == JobState.STOPPED
    window.close()


def test_preview_and_export_keep_event_loop_responsive(qtbot, tmp_path, monkeypatch):
    import threading

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QFileDialog

    window = MainWindow(tmp_path / "responsive.db")
    qtbot.addWidget(window)
    config = SearchConfig(filters=Filters(3, 3), limit=5)
    job = window.store.create_job(config, make_snapshot(config))
    window.load_job(job)
    arrived = threading.Event()
    release = threading.Event()
    original = window.store.export

    def delayed(*args, **kwargs):
        arrived.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(window.store, "export", delayed)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: str(tmp_path / "export"))
    ticks = []
    timer = QTimer(window)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)
    window.export_job()
    qtbot.waitUntil(arrived.is_set)
    qtbot.waitUntil(lambda: len(ticks) >= 3)
    release.set()
    qtbot.waitUntil(lambda: (tmp_path / "export" / f"simulated_{job}_results.csv").exists())
    window.close()


def test_blocked_changed_input_cannot_be_unblocked(qtbot, tmp_path, monkeypatch):
    path = tmp_path / "input.txt"
    path.write_text("abc\n", encoding="utf-8")
    window = MainWindow(tmp_path / "recovery.db")
    qtbot.addWidget(window)
    config = SearchConfig(source="txt", files=(str(path),))
    job = window.store.create_job(config, make_snapshot(config))
    window.store.set_state(job, JobState.BLOCKED, "ValueError")
    window.load_job(job)
    path.write_text("abd\n", encoding="utf-8")
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: errors.append(a[-1]))
    window.recover_blocked_job()
    qtbot.waitUntil(lambda: bool(errors))
    assert window.store.job(job)["state"] == JobState.BLOCKED
    window.close()
