import sqlite3
import threading
import time

import pytest
from legacy_roblox_shell import MainWindow
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox
from test_multi_jobs import concurrent_window as concurrent_window
from test_multi_jobs import start_both
from test_roblox import response as roblox_response
from test_x import Clock
from test_x import response as x_response

from username_finder import roblox_ui, x, x_ui
from username_finder.overnight import OvernightStore
from username_finder.roblox_runner import RobloxRunner
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store
from username_finder.x_runner import XRunner
from username_finder.x_storage import XStore


@pytest.mark.parametrize("platform", ["roblox", "x"])
def test_enabling_overnight_during_manual_stop_does_not_rearm_automatic_restart(
    concurrent_window, qtbot, platform
):
    window, events, entered, _, ids = concurrent_window
    panels = {"roblox": window.roblox, "x": window.x_panel}
    start_both(qtbot, window, entered, ids)
    panel = panels[platform]
    panel.stop()
    # The in-flight mocked response is still held; enabling the mode is not an explicit Start.
    panel.overnight.enabled.setChecked(True)
    for event in events.values():
        event.set()
    qtbot.waitUntil(lambda: not window.roblox.busy and not window.x_panel.busy)
    assert not panel.overnight.repo.get(ids[platform])["auto_resume"]
    assert not panel.overnight.repo.startup_candidates(panel.repo)


@pytest.mark.parametrize("platform", ["roblox", "x"])
@pytest.mark.parametrize("action", ["stop", "disable"])
def test_manual_stop_during_database_contention_cancels_then_saves_intent(
    concurrent_window, qtbot, platform, action
):
    window, events, entered, _, ids = concurrent_window
    for panel in (window.roblox, window.x_panel):
        panel.overnight.enabled.setChecked(True)
    start_both(qtbot, window, entered, ids)
    panel = window.roblox if platform == "roblox" else window.x_panel
    writer = sqlite3.connect(window.store.path)
    window.store.db.execute("PRAGMA busy_timeout=20")
    writer.execute("BEGIN IMMEDIATE")
    try:
        if action == "stop":
            panel.stop()
        else:
            panel.overnight.enabled.setChecked(False)
        assert panel.runner.cancel.is_set()
    finally:
        writer.rollback()
        writer.close()
        window.store.db.execute("PRAGMA busy_timeout=1000")
        for event in events.values():
            event.set()
    qtbot.waitUntil(lambda: not window.roblox.busy and not window.x_panel.busy)
    data = panel.overnight.repo.get(ids[platform])
    assert data["state"] == "Stopped" and not data["auto_resume"]
    if action == "disable":
        assert not data["enabled"]


def test_restart_with_two_eligible_jobs_has_one_worker_per_platform(qtbot, tmp_path, monkeypatch):
    path = tmp_path / "restart-both.db"
    store = Store(path)
    repositories = {"Roblox": RobloxStore(store), "X": XStore(store)}
    ids = {}
    for platform, repo in repositories.items():
        identity = (
            repo.create_run(["ResumeOne", "ResumeTwo"])
            if platform == "Roblox"
            else repo.prepare(x.input_spec("ResumeOne\nResumeTwo"))
        )
        ids[platform] = identity
        night = OvernightStore(store, platform)
        night.enable(identity, True)
        repo.start(identity)
        night.begin(identity)
    store.close()
    clocks = {key: Clock() for key in ids}
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    monkeypatch.setattr(
        roblox_ui,
        "RobloxRunner",
        lambda *a, **kw: RobloxRunner(*a, clock=clocks["Roblox"].now, wait=clocks["Roblox"].wait, **kw),
    )
    monkeypatch.setattr(
        x_ui, "XRunner", lambda *a, **kw: XRunner(*a, clock=clocks["X"].now, wait=clocks["X"].wait, **kw)
    )
    window = MainWindow(path)
    qtbot.addWidget(window)
    entered = {key: threading.Event() for key in ids}
    release = threading.Event()
    calls = {key: [] for key in ids}

    def fetch(platform, value):
        calls[platform].append(value)
        entered[platform].set()
        assert release.wait(3)
        return roblox_response() if platform == "Roblox" else x_response()

    window.roblox.fetcher = lambda value: fetch("Roblox", value)
    window.x_panel.fetcher = lambda value, timeout: fetch("X", value)
    window.show()
    try:
        qtbot.waitUntil(lambda: all(event.is_set() for event in entered.values()))
        assert window.roblox.busy and window.x_panel.busy
        window.roblox.overnight.resume_after_launch()
        window.x_panel.overnight.resume_after_launch()
        assert all(len(values) == 1 for values in calls.values())
        release.set()
        qtbot.waitUntil(lambda: not window.roblox.busy and not window.x_panel.busy)
        for platform, panel in (("Roblox", window.roblox), ("X", window.x_panel)):
            assert panel.current_run == ids[platform]
            assert panel.repo.progress(ids[platform])["state"] == "Completed"
            assert len(calls[platform]) == 2 and panel.repo.results()[1] == 2
    finally:
        release.set()
        window.close()
        qtbot.waitUntil(lambda: window.closed)


def test_two_connectivity_waits_and_gui_events_remain_independent(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    clocks = {"Roblox": Clock(), "X": Clock()}
    waiting = {key: threading.Event() for key in clocks}
    release = {key: threading.Event() for key in clocks}

    def runner(cls, platform, *args, **kwargs):
        instance = cls(*args, clock=clocks[platform].now, **kwargs)

        def wait(seconds):
            if seconds >= 30:
                waiting[platform].set()
                deadline = time.monotonic() + 3
                while not release[platform].is_set():
                    if instance.cancel.wait(0.005):
                        return True
                    assert time.monotonic() < deadline
            clocks[platform].wait(seconds)
            return False

        instance.wait = wait
        return instance

    monkeypatch.setattr(roblox_ui, "RobloxRunner", lambda *a, **kw: runner(RobloxRunner, "Roblox", *a, **kw))
    monkeypatch.setattr(x_ui, "XRunner", lambda *a, **kw: runner(XRunner, "X", *a, **kw))
    window = MainWindow(tmp_path / "two-waits.db")
    qtbot.addWidget(window)
    attempts = {"Roblox": 0, "X": 0}

    def fetch(platform):
        attempts[platform] += 1
        if attempts[platform] == 1:
            raise TimeoutError
        return roblox_response() if platform == "Roblox" else x_response()

    window.roblox.fetcher = lambda value: fetch("Roblox")
    window.x_panel.fetcher = lambda value, timeout: fetch("X")
    r_id = window.roblox.repo.create_run(["WaitOne", "WaitTwo"])
    x_id = window.x_panel.repo.prepare(x.input_spec("WaitOne\nWaitTwo"))
    for panel in (window.roblox, window.x_panel):
        panel.overnight.enabled.setChecked(True)
    window.roblox._start_run(r_id)
    window.x_panel._start_run(x_id)
    ticks = []
    timer = QTimer(window)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)
    try:
        qtbot.waitUntil(lambda: all(event.is_set() for event in waiting.values()))
        qtbot.waitUntil(lambda: len(ticks) >= 4)
        assert attempts == {"Roblox": 1, "X": 1}
        release["Roblox"].set()
        qtbot.waitUntil(lambda: not window.roblox.busy)
        assert window.x_panel.busy and attempts["X"] == 1
        assert window.roblox.repo.results()[1] == 2
        window.x_panel.stop()
        qtbot.waitUntil(lambda: not window.x_panel.busy)
        assert window.x_panel.repo.results()[1] == 0
        assert window.x_panel.overnight.repo.get(x_id)["state"] == "Stopped"
    finally:
        for event in release.values():
            event.set()
        timer.stop()
        window.close()
        qtbot.waitUntil(lambda: window.closed)


def test_gui_reads_while_background_writer_waits_for_external_sqlite_lock(concurrent_window, qtbot):
    window, _, _, _, _ = concurrent_window
    external = sqlite3.connect(window.store.path)
    external.execute("BEGIN IMMEDIATE")
    attempted, done = threading.Event(), threading.Event()
    ticks, errors = [], []
    window.store.db.set_trace_callback(lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None)

    def write():
        try:
            with window.store.transaction() as db:
                db.execute("UPDATE roblox_runs SET error='brief contention'")
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    def read_tick():
        window.roblox.repo.results()
        ticks.append(time.perf_counter())

    worker = threading.Thread(target=write)
    worker.start()
    assert attempted.wait(1)
    timer = QTimer(window)
    timer.timeout.connect(read_tick)
    timer.start(10)
    try:
        qtbot.waitUntil(lambda: len(ticks) >= 3, timeout=2000)
        external.rollback()
        qtbot.waitUntil(done.is_set)
        gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
        print(f"SQLITE_GUI_READ_TICKS={len(ticks)} MAX_GAP_SECONDS={max(gaps, default=0):.4f}")
        assert not errors
    finally:
        external.rollback()
        external.close()
        timer.stop()
        worker.join(2)
        window.store.db.set_trace_callback(None)
