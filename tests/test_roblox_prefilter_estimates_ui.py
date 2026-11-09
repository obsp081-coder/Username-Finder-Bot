import os
import time

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog
from test_roblox_bulk import Clock, record, response
from test_roblox_prefilter import no_live as no_live
from test_roblox_prefilter_estimates import snapshot
from test_roblox_ui import window as window

from username_finder.roblox_activity import RobloxActivity


def test_fields_refresh_cached_estimates_without_database_or_file_work(window, monkeypatch):
    ui = window.roblox.prefilter
    window.tabs.setCurrentWidget(window.roblox)
    window.roblox.checking_mode.setCurrentIndex(2)
    assert ui.batch.maximum() == 20
    assert ui.calculation["latency_source"] == "illustrative_assumption"
    assert "افتراض" in ui.estimate_note.text()
    assert ui.calculation["pilot_requests"] == 5
    changes = window.store.db.total_changes

    def forbidden(*args, **kwargs):
        pytest.fail("Numeric changes must not read SQL, files, or start work")

    with monkeypatch.context() as guard:
        guard.setattr(ui, "new_service", forbidden)
        guard.setattr(window.roblox.repo, "control", forbidden)
        for delay in (10, 2, 0.5, 0.001):
            ui.delay.setValue(delay)
            assert ui.calculation["names_per_minute"] == pytest.approx(1200 / (delay + 0.5))
        assert ui.warning.isVisible()
        ui.batch.setValue(7)
        ui.pilot_size.setValue(22)
        assert ui.calculation["pilot_requests"] == 4
        ui.snapshot = snapshot(valid=1_700_000, delay=0.001, pilot=100)
        ui.refresh_estimates()
        assert ui.calculation["remaining_requests"] == 85_000
        assert ui.calculation["config"]["batch_size"] == 20
        assert window.store.db.total_changes == changes and ui.service is None
    ui.snapshot = None


def test_saved_shared_restriction_is_visible_before_any_import(window):
    ui = window.roblox.prefilter
    with RobloxActivity(window.roblox.repo.store.path) as guard:
        guard.restrict("http_429")
    before = RobloxActivity(window.roblox.repo.store.path).block.read_bytes()
    window.tabs.setCurrentWidget(window.roblox)
    window.roblox.checking_mode.setCurrentIndex(2)
    ui.controls()
    assert ui.restriction_notice.isVisible() and "http_429" in ui.restriction_notice.text()
    ui.delay.setValue(0.001)
    assert ui.calculation["restricted"] and "محظور" in ui.estimate_note.text()
    assert ui.service is None and not window.roblox.test_calls
    assert RobloxActivity(window.roblox.repo.store.path).block.read_bytes() == before


@pytest.mark.parametrize("width,height", [(900, 650), (1180, 850), (1500, 1000)])
def test_restriction_calculator_saved_settings_and_native_layout(
    window, qtbot, tmp_path, monkeypatch, width, height
):
    ui = window.roblox.prefilter
    window.resize(width, height)
    window.tabs.setCurrentWidget(window.roblox)
    window.roblox.checking_mode.setCurrentIndex(2)
    window.statusBar().showMessage("اختبار محلي بردود مصطنعة فقط — لا طلبات Roblox حية")

    def capture(name, focus=None):
        qtbot.wait(30)
        if focus:
            ui.steps_area.ensureWidgetVisible(focus)
            qtbot.wait(20)
        assert window.width() == width
        assert window.roblox.horizontalScrollBar().maximum() == 0
        assert ui.steps_area.horizontalScrollBar().maximum() == 0
        stop = ui.buttons["stop"]
        assert (
            window.roblox.viewport()
            .rect()
            .contains(stop.mapTo(window.roblox.viewport(), stop.rect().center()))
        )
        if os.environ.get("UFB_SCREENSHOTS"):
            target = tmp_path / f"bulk-calculator-{name}-{width}.png"
            assert window.grab().save(str(target))
            print("BULK_CALCULATOR_SCREENSHOT=" + str(target))

    ui.delay.setValue(0.001)
    ui.batch.setValue(20)
    ui.pilot_size.setValue(21)
    capture("draft", ui.estimates)
    path = tmp_path / "names.txt"
    path.write_text("\n".join(f"User{i:05}" for i in range(80)), encoding="utf8")
    ui.source.setText(str(path))
    ui.prepare()
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    before = ui.service.manifest_path.read_bytes()
    assert ui.calculation["planned_requests"] == 5
    assert not ui.delay.isEnabled() and not ui.batch.isEnabled()
    clock = Clock()
    calls = []

    def fetch(request):
        calls.append(request)
        clock.at += 0.25
        if len(calls) == 3:
            return response(status=429, headers={"retry-after": "10"})
        return response([record(n, uid=int(n[4:]) + 1) for n in request["usernames"]])

    ui.service.transport = fetch
    ui.service.clock, ui.service.wall, ui.service.wait = clock.now, clock.wall, clock.wait
    monkeypatch.setattr(ui, "confirm", lambda _: True)
    ui.start("pilot")
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    assert ui.calculation["latency_source"] == "measured_safe_responses"
    assert ui.calculation["latency_samples"] == 2
    assert ui.calculation["observed_names_per_minute"] is not None
    capture("measured", ui.estimates)
    ui.start("full")
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    assert len(calls) == 3
    assert ui.restriction_notice.isVisible() and "http_429" in ui.restriction_notice.text()
    assert "2 ردًا آمنًا" in ui.status.text() and "20" in ui.status.text()
    assert "محظور" in ui.estimate_note.text()
    assert ui.calculation["restricted"]
    assert not ui.buttons["pilot"].isEnabled() and not ui.buttons["full"].isEnabled()
    capture("restricted", ui.estimates)
    directory = ui.service.directory
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: str(directory))
    ui.open_run()
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    assert "http_429" in ui.status.text()
    assert ui.service.manifest_path.read_bytes() == before
    ui.delay.setValue(0.5)
    assert ui.calculation["config"]["delay"] == 0.001
    assert ui.service.manifest_path.read_bytes() == before
    assert not window.roblox.repo.runs() and not window.roblox.test_calls


def test_repeated_calculation_changes_leave_qt_responsive(window, qtbot):
    ui = window.roblox.prefilter
    window.tabs.setCurrentWidget(window.roblox)
    window.roblox.checking_mode.setCurrentIndex(2)
    ui.snapshot = snapshot(valid=1_700_000)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    start = time.perf_counter()
    for i in range(120):
        ui.delay.setValue(0.001 if i % 2 else 10)
        if i % 10 == 0:
            qtbot.wait(5)
    qtbot.waitUntil(lambda: len(ticks) >= 3, timeout=1000)
    timer.stop()
    print(f"GUI calculation changes=120 elapsed={time.perf_counter() - start:.4f}s ticks={len(ticks)}")
    assert ui.service is None and not window.roblox.repo.runs()
    ui.snapshot = None
