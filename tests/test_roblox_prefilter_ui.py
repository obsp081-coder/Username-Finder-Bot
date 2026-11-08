import json
import threading

from PySide6.QtCore import Qt, QTimer
from test_roblox_bulk import response
from test_roblox_ui import window as window


def test_mode_defaults_local_preview_and_no_automatic_traffic(window, qtbot, tmp_path, monkeypatch):
    panel = window.roblox
    ui = panel.prefilter
    assert panel.checking_mode.currentIndex() == 0
    assert panel.checking_mode.count() == 3
    assert ui.delay.value() == 10 and ui.batch.value() == 20 and ui.pilot_size.value() == 100
    window.tabs.setCurrentWidget(panel)
    panel.checking_mode.setCurrentIndex(2)
    assert ui.isVisible() and not panel.bulk.isVisible() and not panel.workspace.isVisible()
    assert ui.layoutDirection() == Qt.LayoutDirection.RightToLeft
    ui.delay.setValue(0.5)
    assert ui.warning.isVisible()
    path = tmp_path / "input.txt"
    path.write_text("SomeName\nsomename\nOtherName\ninvalid-name\n")
    ui.source.setText(str(path))
    ui.prepare()
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    assert ui.snapshot["counts"]["unique"] == 3
    assert ui.snapshot["counts"]["duplicates"] == 1
    assert ui.snapshot["counts"]["invalid"] == 1
    assert not ui.delay.isEnabled()
    assert not panel.repo.runs() and not panel.test_calls
    monkeypatch.setattr(ui, "confirm", lambda _: False)
    ui.start("pilot")
    assert not ui.busy and ui.snapshot["counts"]["attempts"] == 0
    assert not panel.repo.runs()
    for w, h in [(900, 650), (1180, 850)]:
        window.resize(w, h)
        qtbot.wait(30)
        assert panel.horizontalScrollBar().maximum() == 0
        window.grab().save(str(tmp_path / f"prefilter-{w}x{h}.png"))
    panel.checking_mode.setCurrentIndex(0)
    assert panel.workspace.isVisible() and panel.speed.currentData() == 5
    assert panel.bulk.experiment.events() == [] if panel.bulk.experiment.manifest_path.exists() else True


def test_large_local_import_keeps_qt_responsive_and_preview_bounded(window, qtbot, tmp_path):
    ui = window.roblox.prefilter
    path = tmp_path / "large.txt"
    with path.open("w") as f:
        for i in range(100_000):
            f.write(f"User{i:07}\n")
    ui.source.setText(str(path))
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    ui.prepare()
    try:
        qtbot.waitUntil(lambda: not ui.busy, timeout=30000)
    finally:
        timer.stop()
    assert len(ticks) >= 10
    assert ui.snapshot["counts"]["unique"] == 100_000
    assert len(ui.snapshot["samples"]) <= 30
    assert len(json.dumps(ui.snapshot)) < 20_000
    assert not window.roblox.repo.runs() and not window.roblox.test_calls
    print(
        f"Local GUI import: 100000 rows; timer ticks={len(ticks)}; preview rows={len(ui.snapshot['samples'])}"
    )


def test_close_during_prefilter_request_saves_reply_and_pending_work(window, qtbot, tmp_path, monkeypatch):
    ui = window.roblox.prefilter
    path = tmp_path / "input.txt"
    path.write_text("OneName\nTwoName\nThreeName\nFourName")
    ui.source.setText(str(path))
    ui.batch.setValue(2)
    ui.prepare()
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    entered, release = threading.Event(), threading.Event()

    def fetch(request):
        entered.set()
        assert release.wait(5)
        return response([])

    ui.service.transport = fetch
    monkeypatch.setattr(ui, "confirm", lambda _: True)
    ui.start("pilot")
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        window.close()
        assert not window.closed and window.roblox.busy
        assert ui.service.cancel.is_set()
    finally:
        release.set()
    qtbot.waitUntil(lambda: window.closed, timeout=5000)
    r = ui.service.summary()
    assert r["counts"]["attempts"] == r["counts"]["completed_requests"] == 1
    assert r["counts"]["processed"] == 2 and r["unprocessed"] == 2
    assert r["phases"]["pilot"]["state"] == "Paused"
