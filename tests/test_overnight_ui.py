import os
import threading

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QFileDialog, QMessageBox
from test_multi_jobs import concurrent_window as concurrent_window
from test_multi_jobs import select, start_both
from test_roblox import response as roblox_response
from test_x import Clock

from username_finder import roblox_ui, x_protocol
from username_finder.gui import MainWindow
from username_finder.roblox_runner import RobloxRunner
from username_finder.storage import Store
from username_finder.x_storage import XStore


def test_concurrent_overnight_outage_and_x_restriction_are_isolated(concurrent_window, qtbot):
    window, events, entered, calls, ids = concurrent_window
    window.roblox.overnight.enabled.setChecked(True)
    window.x_panel.overnight.enabled.setChecked(True)
    original = window.roblox.fetcher

    def fail_once(value):
        if not getattr(fail_once, "failed", False):
            fail_once.failed = True
            raise ConnectionResetError
        return original(value)

    window.roblox.fetcher = fail_once

    def restricted(*args):
        entered["x"].set()
        calls["x"].append(args[0])
        assert events["x"].wait(5)
        return x_protocol.Response(429, b"", {"retry-after": "600"})

    window.x_panel.fetcher = restricted
    start_both(qtbot, window, entered, ids)
    events["x"].set()
    qtbot.waitUntil(lambda: not window.x_panel.busy)
    assert window.roblox.busy
    assert window.x_panel.overnight.repo.get(ids["x"])["state"] == "Review"
    assert window.x_panel.repo.control()["blocked"]
    select(window.job_manager, "X", ids["x"])
    assert "يدوي" in window.job_manager.details.text()
    events["roblox"].set()
    qtbot.waitUntil(lambda: not window.roblox.busy)
    assert window.roblox.repo.progress(ids["roblox"])["state"] == "Completed"
    assert window.roblox.overnight.repo.get(ids["roblox"])["retries_performed"] == 1
    assert len(calls["roblox"]) == 2 and len(calls["x"]) == 1
    assert window.store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not window.store.db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("stopped", ["roblox", "x"])
def test_overnight_cancellation_is_independent_in_both_directions(concurrent_window, qtbot, stopped):
    window, events, entered, _, ids = concurrent_window
    panels = {"roblox": window.roblox, "x": window.x_panel}
    for panel in panels.values():
        panel.overnight.enabled.setChecked(True)
    start_both(qtbot, window, entered, ids)
    panels[stopped].stop()
    events[stopped].set()
    qtbot.waitUntil(lambda: not panels[stopped].busy)
    other = "x" if stopped == "roblox" else "roblox"
    assert panels[other].busy
    assert panels[stopped].overnight.repo.get(ids[stopped])["state"] == "Stopped"
    assert not panels[stopped].overnight.repo.startup_candidates(panels[stopped].repo)
    events[other].set()
    qtbot.waitUntil(lambda: not panels[other].busy)
    assert panels[other].overnight.repo.get(ids[other])["state"] == "Completed"


@pytest.mark.parametrize("manual", [False, True])
def test_shutdown_recovery_and_manual_stop_are_distinct(qtbot, tmp_path, monkeypatch, manual):
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    waiting = threading.Event()
    clock = Clock()

    def runner(*args, **kwargs):
        instance = RobloxRunner(*args, clock=clock.now, **kwargs)

        def wait(_):
            waiting.set()
            return instance.cancel.wait(5)

        instance.wait = wait
        return instance

    monkeypatch.setattr(roblox_ui, "RobloxRunner", runner)
    path = tmp_path / "restart.db"
    window = MainWindow(path)
    qtbot.addWidget(window)
    window.show()
    assert not window.roblox.overnight.enabled.isChecked()
    identity = window.roblox.repo.create_run(["NightFirst", "NightSecond"])
    window.roblox.overnight.enabled.setChecked(True)

    def timeout(_):
        raise TimeoutError

    window.roblox.fetcher = timeout
    window.roblox._start_run(identity)
    qtbot.waitUntil(waiting.is_set)
    select(window.job_manager, "Roblox", identity)
    assert "انتظار" in window.job_manager.selected()["display_state"]
    if os.environ.get("UFB_SCREENSHOTS"):
        window.tabs.setCurrentWidget(window.job_manager)
        for width, height in ((900, 650), (1180, 850), (1500, 1000)):
            window.resize(width, height)
            qtbot.wait(20)
            assert window.width() <= width and window.height() <= height
            for button in (window.job_manager.stop_button, window.job_manager.overnight_button):
                assert (
                    window.centralWidget()
                    .rect()
                    .contains(QRect(button.mapTo(window.centralWidget(), QPoint()), button.size()))
                )
            screenshot_path = tmp_path / f"overnight-waiting-manager-{width}.png"
            window.grab().save(str(screenshot_path))
            print(f"OVERNIGHT_WAITING_SCREENSHOT={screenshot_path}")
    if manual:
        window.roblox.stop()
        qtbot.waitUntil(lambda: not window.roblox.busy)
        assert window.roblox.overnight.repo.get(identity)["state"] == "Stopped"
    window.close()
    qtbot.waitUntil(lambda: window.closed)
    monkeypatch.setattr(
        roblox_ui, "RobloxRunner", lambda *a, **kw: RobloxRunner(*a, clock=clock.now, wait=clock.wait, **kw)
    )
    reopened = MainWindow(path)
    qtbot.addWidget(reopened)
    calls = []

    def valid(value):
        calls.append(value)
        return roblox_response()

    reopened.roblox.fetcher = valid
    reopened.show()
    if manual:
        qtbot.wait(100)
        assert not calls
        assert reopened.roblox.overnight.repo.get(identity)["state"] == "Stopped"
    else:
        qtbot.waitUntil(lambda: reopened.roblox.repo.progress(identity)["state"] == "Completed")
        assert len(calls) == 2 and clock.seconds >= 30
    assert reopened.tabs.currentWidget() is reopened.job_manager
    reopened.close()
    qtbot.waitUntil(lambda: reopened.closed)


def test_overnight_optout_cancels_wait_and_keeps_original_job(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    waiting = threading.Event()

    def runner(*args, **kwargs):
        instance = RobloxRunner(*args, **kwargs)
        instance.wait = lambda _: waiting.set() or instance.cancel.wait(5)
        return instance

    monkeypatch.setattr(roblox_ui, "RobloxRunner", runner)
    window = MainWindow(tmp_path / "optout.db")
    qtbot.addWidget(window)
    panel = window.roblox
    identity = panel.repo.create_run(["NightFirst", "NightSecond"])
    panel.overnight.enabled.setChecked(True)

    def timeout(_):
        raise TimeoutError

    panel.fetcher = timeout
    panel._start_run(identity)
    qtbot.waitUntil(waiting.is_set)
    panel.overnight.enabled.setChecked(False)
    qtbot.waitUntil(lambda: not panel.busy)
    assert panel.current_run == identity
    assert not panel.overnight.repo.get(identity)["enabled"]
    assert not panel.overnight.repo.startup_candidates(panel.repo)
    assert panel.repo.results()[1] == 0
    window.close()
    qtbot.waitUntil(lambda: window.closed)


def test_overnight_layout_and_manager_entry(concurrent_window, qtbot, tmp_path):
    window, _, _, _, ids = concurrent_window
    select(window.job_manager, "Roblox", ids["roblox"])
    window.job_manager.overnight_button.click()
    assert window.tabs.currentWidget() is window.roblox
    assert window.roblox.workspace.currentIndex() == 2
    for width, height in ((900, 650), (1180, 850), (1500, 1000)):
        window.resize(width, height)
        for name, panel in (("roblox", window.roblox), ("x", window.x_panel)):
            window.tabs.setCurrentWidget(panel)
            panel.workspace.setCurrentIndex(2)
            qtbot.wait(20)
            assert panel.horizontalScrollBar().maximum() == 0
            assert panel.workspace.currentWidget().horizontalScrollBar().maximum() == 0
            for control in (panel.start_button, panel.stop_button, panel.activity_status):
                assert (
                    panel.viewport()
                    .rect()
                    .contains(QRect(control.mapTo(panel.viewport(), QPoint()), control.size()))
                )
            if os.environ.get("UFB_SCREENSHOTS"):
                path = tmp_path / f"overnight-{name}-{width}.png"
                window.grab().save(str(path))
                print(f"OVERNIGHT_GUI_DPR={window.devicePixelRatioF()} SCREENSHOT={path}")


def test_restore_backup_without_overnight_metadata_remains_usable(qtbot, tmp_path, monkeypatch):
    backup = tmp_path / "old-backup.db"
    store = Store(backup)
    XStore(store)
    store.close()
    window = MainWindow(tmp_path / "current.db")
    qtbot.addWidget(window)
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **kw: (str(backup), ""))
    monkeypatch.setattr(window, "ask", lambda *a, **kw: QMessageBox.StandardButton.Yes)
    window.restore_database()
    qtbot.waitUntil(lambda: not window.busy)
    window.job_manager.refresh()
    assert window.roblox.overnight.repo.startup_candidates(window.roblox.repo) == []
    assert window.x_panel.overnight.repo.startup_candidates(window.x_panel.repo) == []
    window.close()
    qtbot.waitUntil(lambda: window.closed)
