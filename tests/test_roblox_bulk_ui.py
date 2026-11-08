import socket
import threading

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFileDialog
from test_roblox_bulk import Clock, record, response
from test_roblox_ui import window as window


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Live networking forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def configure(panel):
    clock, calls = Clock(), []
    exp = panel.bulk.experiment
    exp.clock, exp.wall, exp.wait = clock.now, clock.wall, clock.wait

    def transport(request):
        calls.append(request)
        clock.at += 0.25
        return response([record(n, uid=int(n, 36) + 1) for n in request["usernames"]])

    exp.transport = transport
    return calls


def test_mode_default_rtl_sizes_local_preparation_and_preserved_individual(window, qtbot, tmp_path):
    panel = window.roblox
    calls = configure(panel)
    window.tabs.setCurrentWidget(panel)
    assert panel.checking_mode.currentIndex() == 0 and not panel.bulk.isVisible()
    assert panel.speed.currentData() == 5
    manual = panel.manual.toPlainText()
    before_jobs = panel.repo.runs()
    panel.checking_mode.setCurrentIndex(1)
    assert panel.bulk.isVisible() and not panel.workspace.isVisible()
    assert panel.bulk.layoutDirection() == Qt.LayoutDirection.RightToLeft
    panel.bulk.prepare()
    assert panel.bulk.experiment.manifest_path.exists()
    assert not calls and panel.repo.runs() == before_jobs
    assert panel.bulk.buttons["stage1"].isEnabled()
    assert not panel.bulk.buttons["stage2"].isEnabled()
    assert not panel.bulk.buttons["comparison"].isEnabled()
    for width, height in [(900, 650), (1180, 850)]:
        window.resize(width, height)
        qtbot.wait(40)
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.bulk.table.width() > 300
        window.grab().save(str(tmp_path / f"bulk-{width}x{height}.png"))
    panel.checking_mode.setCurrentIndex(0)
    assert panel.workspace.isVisible() and panel.individual_actions.isVisible()
    assert panel.manual.toPlainText() == manual
    assert panel.repo.counts() == {} or not any(panel.repo.counts().values())
    assert not calls


def test_explicit_confirmation_stage_boundary_exports_and_no_job_writes(window, qtbot, monkeypatch, tmp_path):
    panel, bulk = window.roblox, window.roblox.bulk
    calls = configure(panel)
    bulk.prepare()
    monkeypatch.setattr(bulk, "confirm", lambda _: False)
    bulk.start("stage1")
    assert not calls and not bulk.experiment.events()
    monkeypatch.setattr(bulk, "confirm", lambda _: True)
    bulk.start("stage1")
    qtbot.waitUntil(lambda: not bulk.busy, timeout=15000)
    assert len(calls) == 10
    assert bulk.progress.value() == 10
    assert bulk.table.rowCount() == 200
    assert not panel.repo.runs() and not panel.repo.counts()
    assert not bulk.buttons["stage1"].isEnabled()
    assert bulk.buttons["stage2"].isEnabled()
    assert bulk.buttons["comparison"].isEnabled()
    assert not panel.busy
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **kw: (str(tmp_path / "out.json"), ""))
    bulk.export("json")
    assert (tmp_path / "out.json").exists()
    assert len(calls) == 10  # Stage 2 never starts automatically.


def test_ui_responsive_stop_and_other_entry_points_blocked(window, qtbot, monkeypatch):
    panel, bulk = window.roblox, window.roblox.bulk
    bulk.prepare()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def transport(request):
        entered.set()
        calls.append(request)
        assert release.wait(5)
        return response([])

    bulk.experiment.transport = transport
    monkeypatch.setattr(bulk, "confirm", lambda _: True)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    bulk.start("stage1")
    try:
        qtbot.waitUntil(entered.is_set, timeout=3000)
        assert panel.busy and bulk.busy and not panel.checking_mode.isEnabled()
        qtbot.waitUntil(lambda: len(ticks) >= 3)
        panel.start()  # No production preparation or job while Bulk is active.
        assert not panel.repo.runs()
        bulk.stop()
    finally:
        release.set()
        timer.stop()
    qtbot.waitUntil(lambda: not bulk.busy, timeout=5000)
    assert len(calls) == 1
    assert bulk.experiment.report()["phases"]["stage1"]["state"] == "Stopped"
    assert not bulk.buttons["stage2"].isEnabled()


def test_window_close_waits_for_inflight_evidence_and_cleanup(window, qtbot, monkeypatch):
    bulk = window.roblox.bulk
    bulk.prepare()
    entered, release = threading.Event(), threading.Event()

    def transport(request):
        entered.set()
        assert release.wait(5)
        return response([])

    bulk.experiment.transport = transport
    monkeypatch.setattr(bulk, "confirm", lambda _: True)
    bulk.start("stage1")
    try:
        qtbot.waitUntil(entered.is_set, timeout=3000)
        window.close()
        assert not window.closed and window.roblox.busy
        assert bulk.experiment.cancel.is_set()
    finally:
        release.set()
    qtbot.waitUntil(lambda: window.closed, timeout=5000)
    report = bulk.experiment.report()
    assert report["dispatch_intents"] == report["complete_http_responses"] == 1
    assert report["phases"]["stage1"]["state"] == "Stopped"
