"""Large shared-store jobs with controlled clocks/HTTP and explicit manager actions."""

import json
import os
import threading
import time
from collections import Counter
from urllib.parse import parse_qs, urlsplit

import pytest
from legacy_roblox_shell import MainWindow
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox
from test_multi_jobs import select
from test_roblox import response as roblox_response
from test_roblox_file_scale import FOUR_COUNT, write_names
from test_x import Clock
from test_x import response as x_response
from test_x_preparation import select_file, write_five

from username_finder import roblox_ui, x, x_ui
from username_finder.overnight_preflight_ui import OvernightPreflightDialog
from username_finder.roblox_runner import RobloxRunner
from username_finder.x_runner import XRunner


def test_large_roblox_and_x_preflight_concurrent_stop_and_reopen(qtbot, tmp_path, monkeypatch):
    if not os.environ.get("UFB_LARGE_FILES"):
        pytest.skip("Explicit representative 1.77-million-row integration run only")
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    clocks = {name: Clock() for name in ("Roblox", "X")}
    calls = {name: [] for name in clocks}
    inflight, maximum = Counter(), Counter()
    both_first = {name: threading.Event() for name in clocks}
    release_first, release_last, last_roblox = threading.Event(), threading.Event(), threading.Event()
    mutex = threading.Lock()
    maximum_total = [0]

    def factory(cls, name, *args, **kwargs):
        return cls(*args, clock=clocks[name].now, wait=clocks[name].wait, **kwargs)

    monkeypatch.setattr(roblox_ui, "RobloxRunner", lambda *a, **kw: factory(RobloxRunner, "Roblox", *a, **kw))
    monkeypatch.setattr(x_ui, "XRunner", lambda *a, **kw: factory(XRunner, "X", *a, **kw))
    # A missing mock must fail locally, never fall through to a provider request.
    monkeypatch.setattr(x.protocol, "fetch", lambda *a, **kw: pytest.fail("No live X HTTP"))
    monkeypatch.setattr(
        "username_finder.roblox_protocol.fetch", lambda *a, **kw: pytest.fail("No live Roblox HTTP")
    )

    def fetch(platform, value):
        clock = clocks[platform]
        name = parse_qs(urlsplit(value).query)["Username"][0] if platform == "Roblox" else value
        with mutex:
            calls[platform].append((name, clock.seconds))
            count = len(calls[platform])
            inflight[platform] += 1
            maximum[platform] = max(maximum[platform], inflight[platform])
            maximum_total[0] = max(maximum_total[0], sum(inflight.values()))
        try:
            if count == 1:
                both_first[platform].set()
                assert release_first.wait(15)
            clock.seconds += 0.05
            if platform == "Roblox":
                if count == 1:
                    raise TimeoutError
                if count == 5:
                    last_roblox.set()
                    assert release_last.wait(15)
                return roblox_response()
            if count == 4:
                return x.protocol.Response(429, b"", {"retry-after": "120"})
            return x_response()
        finally:
            with mutex:
                inflight[platform] -= 1

    database = tmp_path / "concurrent-large.db"
    window = MainWindow(database)
    qtbot.addWidget(window)
    window.show()
    window.roblox.fetcher = lambda value: fetch("Roblox", value)
    window.x_panel.fetcher = lambda value, timeout: fetch("X", value)
    ticks = [time.perf_counter()]
    timer = QTimer(window)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start(20)
    identities = {}
    try:
        roblox = window.roblox
        roblox.modes.setCurrentIndex(1)
        roblox.files.addItem(str(write_names(tmp_path / "roblox-four.txt", FOUR_COUNT)))
        roblox.speed.setCurrentIndex(roblox.speed.findData(0))
        roblox.overnight.enabled.setChecked(True)
        roblox.overnight.prepare_button.click()
        qtbot.waitUntil(lambda: not roblox.busy, timeout=60000)
        identities["Roblox"] = roblox.current_run
        before = roblox.repo.progress(roblox.current_run)
        assert (
            before["total"] == FOUR_COUNT and before["state"] == "Ready" and before["interval_seconds"] == 0
        )
        panel = select_file(window, write_five(tmp_path / "x-five.csv", 31785))
        panel.overnight.enabled.setChecked(True)
        panel.overnight.prepare_button.click()
        qtbot.waitUntil(lambda: not panel.busy, timeout=15000)
        identities["X"] = panel.current_run
        assert roblox.repo.progress(roblox.current_run) == before
        assert panel.repo.progress(panel.current_run)["total"] == 31785
        assert panel.repo.progress(panel.current_run)["interval_seconds"] == 5
        assert not any(calls.values())
        dialog = OvernightPreflightDialog(window.job_manager)
        qtbot.addWidget(dialog)
        dialog.check_now()
        qtbot.waitUntil(dialog.check_button.isEnabled, timeout=15000)
        assert len(dialog.results) == 2 and not any(result.errors for result in dialog.results)
        dialog.close()
        for name, identity in identities.items():
            select(window.job_manager, name, identity)
            window.job_manager.start_selected()
        qtbot.waitUntil(lambda: all(event.is_set() for event in both_first.values()), timeout=15000)
        assert window.roblox.busy and window.x_panel.busy
        saved_runners = (window.roblox.runner, window.x_panel.runner)
        window.roblox._start_run(identities["Roblox"])
        window.x_panel._start_run(identities["X"])
        assert saved_runners == (window.roblox.runner, window.x_panel.runner)
        release_first.set()
        qtbot.waitUntil(last_roblox.is_set, timeout=15000)
        qtbot.waitUntil(lambda: not window.x_panel.busy, timeout=15000)
        assert window.roblox.busy and window.x_panel.repo.control()["blocked"]
        assert window.x_panel.repo.progress(identities["X"])["state"] == "Restricted"
        assert window.x_panel.repo.results()[1] == 4
        # Roblox remains active while X's server restriction is latched.
        window.roblox.stop()
        release_last.set()
        qtbot.waitUntil(lambda: not window.roblox.busy, timeout=10000)
        timer.stop()
        ticks.append(time.perf_counter())
        gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
        assert window.roblox.repo.results()[1] == 4
        assert window.roblox.overnight.repo.get(identities["Roblox"])["state"] == "Stopped"
        assert window.x_panel.overnight.repo.get(identities["X"])["state"] == "Review"
        assert window.roblox.overnight.repo.get(identities["Roblox"])["attempts"] == 1
        assert maximum == {"Roblox": 1, "X": 1} and maximum_total[0] == 2
        assert len(set(name for name, _ in calls["Roblox"])) == 4  # One safe pre-response retry.
        assert len(set(name for name, _ in calls["X"])) == 4
        assert clocks["Roblox"].waits == [30.0]
        assert clocks["X"].waits == [5.0, 5.0, 5.0]
        assert not window.store.db.execute("PRAGMA foreign_key_check").fetchall()
        assert window.store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        print(
            "CONCURRENT_LARGE="
            + json.dumps(
                dict(
                    candidates={"Roblox": FOUR_COUNT, "X": 31785},
                    completed={"Roblox": 4, "X": 4},
                    max_inflight=dict(maximum),
                    simultaneous=maximum_total[0],
                    waits={name: clock.waits for name, clock in clocks.items()},
                    max_gui_gap=round(max(gaps, default=0), 4),
                )
            )
        )
    finally:
        timer.stop()
        for panel in (window.roblox, window.x_panel):
            panel.stop()
        release_first.set()
        release_last.set()
        qtbot.waitUntil(lambda: not window.roblox.busy and not window.x_panel.busy, timeout=15000)
        window.close()
        qtbot.waitUntil(lambda: window.closed, timeout=10000)
    reopened = MainWindow(database)
    qtbot.addWidget(reopened)
    reopened.roblox.fetcher = lambda *a: pytest.fail("Stopped job must never restart automatically")
    reopened.x_panel.fetcher = lambda *a: pytest.fail("Restricted X must never restart automatically")
    try:
        reopened.show()
        qtbot.wait(60)
        assert not reopened.roblox.busy and not reopened.x_panel.busy
        for name, panel in reopened.job_manager.panels.items():
            run = panel.repo.progress(identities[name])
            assert run["total"] == (FOUR_COUNT if name == "Roblox" else 31785)
            assert panel.repo.results()[1] == 4
            assert not panel.overnight.repo.startup_candidates(panel.repo)
        assert reopened.x_panel.repo.control()["blocked"]
    finally:
        reopened.close()
        qtbot.waitUntil(lambda: reopened.closed, timeout=10000)
