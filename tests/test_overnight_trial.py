"""GUI preparation/preflight/start/reopen at virtual overnight scale; no external HTTP."""

import json
import threading
import time
from collections import Counter
from urllib.parse import parse_qs, urlsplit

from PySide6.QtWidgets import QMessageBox
from test_multi_jobs import select
from test_roblox import response as roblox_response
from test_x import Clock
from test_x import response as x_response

from username_finder import roblox_ui, x, x_ui
from username_finder.gui import MainWindow
from username_finder.overnight_preflight_ui import OvernightPreflightDialog
from username_finder.roblox_runner import RobloxRunner
from username_finder.windows_awake import AwakeRequest
from username_finder.x_runner import XRunner


def test_gui_overnight_prepare_preflight_recover_complete_and_stop(qtbot, tmp_path, monkeypatch):
    started = time.perf_counter()
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    clocks = {name: Clock() for name in ("Roblox", "X")}
    waiting = {name: threading.Event() for name in clocks}
    calls = {name: Counter() for name in clocks}
    active, maximum = Counter(), Counter()
    mutex = threading.Lock()
    phase = [1]

    def runner(cls, platform, *args, **kwargs):
        instance = cls(*args, clock=clocks[platform].now, **kwargs)

        def wait(seconds):
            if seconds >= 30 and phase[0] in (1, 3):
                waiting[platform].set()
                return instance.cancel.wait(10)
            clocks[platform].wait(seconds)
            return False

        instance.wait = wait
        return instance

    monkeypatch.setattr(roblox_ui, "RobloxRunner", lambda *a, **kw: runner(RobloxRunner, "Roblox", *a, **kw))
    monkeypatch.setattr(x_ui, "XRunner", lambda *a, **kw: runner(XRunner, "X", *a, **kw))

    def fetch(platform, value):
        name = parse_qs(urlsplit(value).query)["Username"][0] if platform == "Roblox" else value
        with mutex:
            calls[platform][name] += 1
            count = sum(calls[platform].values())
            active[platform] += 1
            maximum[platform] = max(maximum[platform], active[platform])
        try:
            clocks[platform].seconds += 0.05
            if phase[0] == 1 and count == 11 or phase[0] == 2 and count == 600:
                raise TimeoutError
            if phase[0] == 3:
                if platform == "Roblox":
                    raise ConnectionResetError
                return x.protocol.Response(429, b"", {"retry-after": "120"})
            return roblox_response() if platform == "Roblox" else x_response()
        finally:
            with mutex:
                active[platform] -= 1

    path = tmp_path / "trial.db"

    def open_window():
        window = MainWindow(path)
        qtbot.addWidget(window)
        window.roblox.fetcher = lambda value: fetch("Roblox", value)
        window.x_panel.fetcher = lambda value, timeout: fetch("X", value)
        window.job_manager.awake = AwakeRequest(api=lambda flags: 1)
        window.job_manager.keep_awake.setChecked(True)
        window.show()
        return window

    window = open_window()
    identities = {}
    for platform, panel in window.job_manager.panels.items():
        panel.minimum_length.setValue(8)
        panel.maximum_length.setValue(8)
        panel.quantity.setText("1500")
        panel.speed.setCurrentIndex(panel.speed.findData(5 if platform == "Roblox" else 15))
        panel.overnight.enabled.setChecked(True)
        panel.overnight.prepare_button.click()
        qtbot.waitUntil(lambda panel=panel: not panel.busy, timeout=15000)
        identities[platform] = panel.current_run
        assert panel.repo.progress(panel.current_run)["total"] == 1500
    preflight = OvernightPreflightDialog(window.job_manager)
    qtbot.addWidget(preflight)
    preflight.check_now()
    qtbot.waitUntil(preflight.check_button.isEnabled)
    assert not any(result.errors for result in preflight.results)
    assert not any(calls.values())
    preflight.close()
    for platform, identity in identities.items():
        select(window.job_manager, platform, identity)
        window.job_manager.start_selected()
    qtbot.waitUntil(lambda: all(event.is_set() for event in waiting.values()), timeout=15000)
    assert window.roblox.repo.results()[1] == window.x_panel.repo.results()[1] == 10
    assert window.job_manager.awake.active
    window.close()  # Graceful shutdown preserves consent; explicit Stop is tested below.
    qtbot.waitUntil(lambda: window.closed)
    assert not window.job_manager.awake.active

    phase[0] = 2
    window = open_window()
    qtbot.waitUntil(
        lambda: all(
            panel.current_run == identities[name] for name, panel in window.job_manager.panels.items()
        )
    )
    qtbot.waitUntil(lambda: not window.roblox.busy and not window.x_panel.busy, timeout=60000)
    for platform, panel in window.job_manager.panels.items():
        identity = identities[platform]
        run = panel.repo.progress(identity)
        assert run["state"] == "Completed" and run["interval_seconds"] == (5 if platform == "Roblox" else 15)
        assert panel.repo.results()[1] == 1500
        assert len(calls[platform]) == 1500 and sum(calls[platform].values()) == 1502
        assert panel.overnight.repo.get(identity)["attempts"] == 2
        prefix = "roblox" if platform == "Roblox" else "x"
        assert window.store.db.execute(
            f"SELECT COUNT(DISTINCT username_key),COUNT(*) FROM {prefix}_checks WHERE run_id=?", (identity,)
        ).fetchone()[:] == (1500, 1500)
    assert maximum == {"Roblox": 1, "X": 1}
    assert clocks["Roblox"].seconds > 2 * 3600 and clocks["X"].seconds > 6 * 3600

    phase[0] = 3
    for event in waiting.values():
        event.clear()
    next_ids = {}
    for platform, panel in window.job_manager.panels.items():
        panel.modes.setCurrentIndex(1)
        panel.manual.setPlainText("TrialNextOne\nTrialNextTwo")
        panel.overnight.prepare_button.click()
        qtbot.waitUntil(lambda panel=panel: not panel.busy)
        next_ids[platform] = panel.current_run
        select(window.job_manager, platform, panel.current_run)
        window.job_manager.start_selected()
    qtbot.waitUntil(waiting["Roblox"].is_set)
    qtbot.waitUntil(lambda: not window.x_panel.busy)
    assert window.roblox.busy and window.x_panel.repo.control()["blocked"]
    assert window.x_panel.overnight.repo.get(next_ids["X"])["state"] == "Review"
    window.job_manager.update_power()
    assert window.job_manager.awake.active
    window.roblox.stop()
    qtbot.waitUntil(lambda: not window.roblox.busy)
    assert window.roblox.overnight.repo.get(next_ids["Roblox"])["state"] == "Stopped"
    assert window.roblox.repo.results()[1] == 1500 and window.x_panel.repo.results()[1] == 1501
    for panel in window.job_manager.panels.values():
        assert not panel.overnight.repo.startup_candidates(panel.repo)
    assert window.store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not window.store.db.execute("PRAGMA foreign_key_check").fetchall()
    window.job_manager.update_power()
    assert not window.job_manager.awake.active
    window.close()
    qtbot.waitUntil(lambda: window.closed)
    print(
        "OVERNIGHT_E2E="
        + json.dumps(
            {
                "completed": 3000,
                "restriction_observations": 1,
                "virtual_hours": {name: round(clock.seconds / 3600, 3) for name, clock in clocks.items()},
                "test_seconds": round(time.perf_counter() - started, 4),
                "maximum_inflight": dict(maximum),
            }
        )
    )
