"""Offscreen UI controls for explicit individual concurrency consent."""

import threading

import test_roblox_concurrent as worker_fixtures
import test_roblox_ui as ui_fixtures
from PySide6.QtCore import QTimer
from test_roblox import response

from username_finder.roblox_activity import RobloxActivity
from username_finder.roblox_concurrent import configure_execution, execution_policy

window = ui_fixtures.window
accelerated = worker_fixtures.accelerated


def test_default_and_historical_restriction_controls(window):
    panel = window.roblox
    assert not panel.parallel.isChecked()
    assert not panel.workers.isEnabled() and not panel.request_rate.isEnabled()
    panel.parallel.setChecked(True)
    assert panel.workers.isEnabled() and panel.request_rate.isEnabled()
    marker = RobloxActivity(panel.repo.store.path).block
    marker.write_bytes(b"http_429")
    panel.controls()
    assert not panel.start_button.isEnabled() and not panel.resume_button.isEnabled()
    identity = panel.repo.create_run(["Alpha"])
    panel._start_run(identity)
    assert not panel.test_calls and marker.read_bytes() == b"http_429"
    assert execution_policy(panel.repo, identity)["workers"] == 1


def test_saved_parallel_run_requires_explicit_opt_in_and_no_automatic_start(window):
    panel = window.roblox
    panel.repo.set_preferred_interval(0)
    identity = panel.repo.create_run(["Alpha"])
    configure_execution(panel.repo, identity, workers=2, rate=1)
    panel._start_run(identity)
    assert not panel.busy and not panel.test_calls
    panel.parallel.setChecked(True)
    panel._start_run(identity, automatic=True)
    assert not panel.busy and not panel.test_calls


def test_parallel_ui_responsive_cancel_and_session_metrics(window, qtbot, accelerated):
    panel = window.roblox
    panel.repo.set_preferred_interval(0)
    identity = panel.repo.create_run(["Alpha", "Bravo", "Charlie", "Delta"])
    panel.parallel.setChecked(True)
    panel.workers.setValue(2)
    entered = threading.Barrier(2)
    release = threading.Event()

    def fetch(url):
        entered.wait(3)
        assert release.wait(3)
        return response(1)

    panel.fetcher = fetch
    beats = []
    timer = QTimer(panel)
    timer.setInterval(5)
    timer.timeout.connect(lambda: beats.append(True))
    timer.start()
    panel._start_run(identity)
    try:
        qtbot.waitUntil(
            lambda: (
                panel.runner is not None
                and panel.runner.execution is not None
                and panel.runner.execution.snapshot()["active"] == 2
            ),
            timeout=3000,
        )
        qtbot.waitUntil(lambda: len(beats) >= 3, timeout=1000)
        panel.refresh()
        assert not panel.parallel.isEnabled() and not panel.workers.isEnabled()
        assert panel.stop_button.isEnabled() and not panel.overnight.enabled.isEnabled()
        assert "2" in panel.execution_status.text()
        panel.stop()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not panel.busy, timeout=4000)
        timer.stop()
    assert panel.repo.progress(identity)["counts"] == {"Done": 2, "Pending": 2}
    assert panel.last_execution[1]["accepted"] == 2
    assert panel.last_execution[1]["active"] == panel.last_execution[1]["outstanding"] == 0
    assert "Taken: 2" in panel.execution_status.text()
