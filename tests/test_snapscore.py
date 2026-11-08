from dataclasses import replace

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QMessageBox
from snapscore_fixture import POINTS, TARGET, FakeDesktop, advance_clock, calibrate

from username_finder.gui import MainWindow
from username_finder.snapscore_desktop import DesktopError
from username_finder.snapscore_ui import Plan, SnapscorePanel, SnapscoreSession


@pytest.fixture
def panel(qtbot):
    widget = SnapscorePanel(desktop=FakeDesktop())
    qtbot.addWidget(widget)
    widget.resize(900, 650)
    widget.show()
    yield widget
    widget.shutdown()
    widget.close()


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_finite_single_pass_and_separate_confirmation_for_every_click(qapp, limit):
    desktop = FakeDesktop()
    session = SnapscoreSession(desktop, clock=lambda: 0.0)
    plan = Plan(TARGET, POINTS, limit, 3)
    assert not session.start(plan) and not desktop.clicks
    assert session.start(plan, confirmed=True)
    assert not session.start(plan, confirmed=True)
    for index in range(limit):
        advance_clock(session)
        assert session.attempts == index + 1 and desktop.clicks == list(POINTS[: index + 1])
        if index + 1 < limit:
            assert session.state == "awaiting"
            session.tick()
            assert len(desktop.clicks) == index + 1
            assert not session.advance() and not session.advance(confirmed=1)
            assert session.advance(confirmed=True)
    assert session.state == "complete" and not session.timer.isActive() and desktop.callback is None
    session.tick()
    assert len(desktop.clicks) == limit and not session.advance(confirmed=True)
    assert "لم يُتحقق" in session.message


@pytest.mark.parametrize("limit,delay", [(0, 3), (4, 3), (True, 3), (1, 2), (1, 31), (1, True)])
def test_invalid_unbounded_plans_rejected(limit, delay):
    with pytest.raises(ValueError):
        Plan(TARGET, POINTS, limit, delay)


@pytest.mark.parametrize("points", [(POINTS[0],) * 3, ((-1, -1), *POINTS[1:]), POINTS[:2]])
def test_duplicate_or_outside_points_rejected(points):
    with pytest.raises(ValueError):
        Plan(TARGET, points, 3, 5)


@pytest.mark.parametrize("phase", ["countdown", "awaiting", "global_stop"])
def test_cancellation_releases_hotkey_without_further_input(qapp, phase):
    desktop = FakeDesktop()
    session = SnapscoreSession(desktop, clock=lambda: 0.0)
    session.start(Plan(TARGET, POINTS, 3, 3), confirmed=True)
    if phase == "awaiting":
        advance_clock(session)
    if phase == "global_stop":
        desktop.callback()
    else:
        session.stop()
    count = len(desktop.clicks)
    advance_clock(session)
    assert session.state == "stopped" and len(desktop.clicks) == count
    assert desktop.callback is None and not session.timer.isActive()


@pytest.mark.parametrize("failure", ["target", "changed", "point", "hotkey", "partial_input", "unexpected"])
def test_fail_closed_errors_and_attempt_counters(qapp, failure):
    desktop = FakeDesktop()
    session = SnapscoreSession(desktop, clock=lambda: 0.0)
    if failure == "hotkey":
        desktop.arm_problem = "hotkey"
    elif failure == "partial_input":
        desktop.click_problem = DesktopError("input")
    elif failure == "unexpected":
        desktop.click_problem = RuntimeError("password=DO_NOT_DISPLAY")
    else:
        desktop.problem = failure
    session.start(Plan(TARGET, POINTS, 3, 3), confirmed=True)
    advance_clock(session)
    assert session.state == "error" and session.errors == 1 and not desktop.clicks
    assert session.attempts == int(failure in ("partial_input", "unexpected"))
    assert "DO_NOT_DISPLAY" not in session.message and desktop.callback is None
    assert not session.timer.isActive()


@pytest.mark.parametrize("waiting", [True, False])
def test_idle_and_absolute_session_deadlines(qapp, waiting):
    desktop = FakeDesktop()
    session = SnapscoreSession(desktop, clock=lambda: 0.0)
    session.start(Plan(TARGET, POINTS, 3, 3), confirmed=True)
    if waiting:
        advance_clock(session)
        session.clock = lambda: 64.0
    else:
        session.clock = lambda: 301.0
    session.tick()
    assert session.state == "stopped" and desktop.callback is None
    assert len(desktop.clicks) == int(waiting)


def test_calibration_and_preview_never_inject_input(panel):
    assert not panel.start.isEnabled()
    panel.start_session()
    panel.preview.click()
    assert not panel.desktop.clicks and not panel.desktop.captures
    calibrate(panel)
    assert len(panel.calibrated) == 3 and panel.desktop.captures == 3
    panel.preview.click()
    assert panel.start.isEnabled() and panel.reviewed == panel.plan()
    assert "X=200, Y=200" in panel.plan_text.text() and not panel.desktop.clicks
    assert panel.layoutDirection() == Qt.LayoutDirection.RightToLeft
    assert panel.positions[0][0].layoutDirection() == Qt.LayoutDirection.LeftToRight
    panel.positions[0][0].setValue(201)
    assert panel.reviewed is None and not panel.start.isEnabled()


def test_calibration_cancel_error_and_target_change(panel):
    panel.calibrate(0)
    panel.stop.click()
    panel.capture_due = 0
    panel._capture_tick()
    assert not panel.desktop.captures
    panel.desktop.problem = "target"
    panel.calibrate(0)
    panel.capture_due = 0
    panel._capture_tick()
    assert not panel.calibrated and "Snapchat Web" in panel.status.text()
    panel.desktop.problem = None
    calibrate(panel)
    panel.desktop.target = replace(TARGET, dpi=144)
    panel.calibrate(1)
    panel.capture_due = 0
    panel._capture_tick()
    assert panel.calibrated == {1} and not panel.start.isEnabled()


def test_dialog_no_is_default_and_every_step_is_explicit(panel, monkeypatch):
    calibrate(panel)
    panel.preview.click()
    calls = []

    def decline(*args):
        calls.append(args)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", decline)
    panel.start.click()
    assert not panel.session.active and not panel.desktop.clicks
    assert calls[-1][-1] == QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    panel.start.click()
    assert panel.session.active and not panel.desktop.clicks and panel.stop.isEnabled()
    advance_clock(panel.session)
    assert panel.session.attempts == 1 and panel.next.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", decline)
    panel.next.click()
    assert panel.session.state == "awaiting" and len(panel.desktop.clicks) == 1
    panel.stop.click()
    assert not panel.session.active and "النقرات المحاوَلة: 1/3" in panel.counters.text()


def test_stale_confirmation_after_stop_does_not_resume(panel, monkeypatch):
    calibrate(panel)
    panel.preview.click()
    monkeypatch.setattr(panel, "confirm", lambda _: True)
    panel.start.click()
    advance_clock(panel.session)

    def stale(_):
        panel.stop_work()
        return True

    monkeypatch.setattr(panel, "confirm", stale)
    panel.next.click()
    assert panel.session.state == "stopped" and len(panel.desktop.clicks) == 1


def test_navigation_preserves_pages_state_and_stops_snapscore_on_hide(qtbot, tmp_path, monkeypatch):
    desktop = FakeDesktop()
    monkeypatch.setattr("username_finder.snapscore_ui.WindowsDesktop", lambda: desktop)
    window = MainWindow(tmp_path / "isolated.db")
    qtbot.addWidget(window)
    window.timer.stop()
    window.show()
    monkeypatch.setattr(window, "unsaved_choice", lambda: QMessageBox.StandardButton.Discard)
    assert [window.tabs.tabText(i) for i in range(3)] == ["سناب", "Instagram", "البحث داخل الملفات"]
    assert [window.snapchat_tabs.tabText(i) for i in range(2)] == ["البحث", "سناب سكور"]
    assert [window.search_tabs.tabText(i) for i in range(4)] == ["الإعدادات", "التشغيل", "النتائج", "السجل"]
    original_pages = [window.search_tabs.widget(i) for i in range(4)]
    window.prefix.setText("abc")
    before = window.editor_signature()
    assert "•" in window.search_tabs.tabText(0) and window.tabs.tabText(0) == "سناب"
    for top in range(3):
        window.tabs.setCurrentIndex(top)
    window.tabs.setCurrentIndex(0)
    for index in range(4):
        window.search_tabs.setCurrentIndex(index)
    assert original_pages == [window.search_tabs.widget(i) for i in range(4)]
    assert before == window.editor_signature()
    window.snapchat_tabs.setCurrentIndex(1)
    panel = window.snapscore
    panel.delay.setValue(6)
    panel.limit.setValue(2)
    calibrate(panel)
    panel.preview.click()
    monkeypatch.setattr(panel, "confirm", lambda _: True)
    panel.start.click()
    assert panel.session.active
    window.tabs.setCurrentWidget(window.file_search)
    assert not panel.session.active and not desktop.clicks and desktop.callback is None
    assert not window.store.jobs() and not window.instagram.start_button.isEnabled()
    assert before == window.editor_signature()
    window.close()
    qtbot.waitUntil(lambda: window.closed)


def test_close_cancels_calibration_and_running_session(panel, monkeypatch):
    calibrate(panel)
    panel.preview.click()
    monkeypatch.setattr(panel, "confirm", lambda _: True)
    panel.start.click()
    panel.shutdown()
    advance_clock(panel.session)
    assert not panel.session.active and not panel.desktop.clicks and panel.closing
    assert not panel.start.isEnabled() and panel.desktop.callback is None


@pytest.mark.parametrize("size", [(900, 650), (1180, 850)])
def test_running_status_stop_visibility_and_responsive_countdown(panel, qtbot, monkeypatch, size):
    panel.resize(*size)
    calibrate(panel)
    panel.preview.click()
    monkeypatch.setattr(panel, "confirm", lambda _: True)
    beats = []
    timer = QTimer(panel)
    timer.timeout.connect(lambda: beats.append(True))
    timer.start(5)
    panel.start.click()
    qtbot.wait(60)
    assert len(beats) >= 3 and panel.session.active and not panel.desktop.clicks
    assert "جارٍ التشغيل" in panel.status.text()
    for control in (panel.stop, panel.status):
        position = control.mapTo(panel.viewport(), control.rect().center())
        assert panel.viewport().rect().contains(position)
    assert panel.horizontalScrollBar().maximum() == 0
    panel.stop.click()
    timer.stop()
    assert not panel.session.active and panel.desktop.callback is None
