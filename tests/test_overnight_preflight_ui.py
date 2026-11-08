import json
import os
import threading

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from test_multi_jobs import concurrent_window as concurrent_window
from test_multi_jobs import select, start_both

from username_finder.overnight_preflight_ui import OvernightPreflightDialog
from username_finder.windows_awake import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, AwakeRequest


def test_prepare_saved_overnight_jobs_without_starting_requests(concurrent_window, qtbot):
    window, _, _, calls, _ = concurrent_window
    for panel in (window.roblox, window.x_panel):
        panel.minimum_length.setValue(8)
        panel.maximum_length.setValue(8)
        panel.quantity.setText("4")
        panel.overnight.enabled.setChecked(True)
        panel.overnight.prepare_button.click()
        qtbot.waitUntil(lambda panel=panel: not panel.busy)
        assert panel.current_run
        assert panel.repo.progress(panel.current_run)["total"] == 4
        assert panel.overnight.repo.get(panel.current_run)["enabled"]
        assert not panel.overnight.repo.get(panel.current_run)["auto_resume"]
    assert not any(calls.values())


def test_preflight_reports_each_intended_job_and_never_starts_them(concurrent_window, qtbot):
    window, _, _, calls, ids = concurrent_window
    for name, panel in (("roblox", window.roblox), ("x", window.x_panel)):
        panel.current_run = ids[name]
        panel.overnight.enabled.setChecked(True)
    dialog = OvernightPreflightDialog(window.job_manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.check_now()
    qtbot.waitUntil(dialog.check_button.isEnabled)
    assert len(dialog.results) == 2 and not any(result.errors for result in dialog.results)
    assert all(identity in dialog.output.toPlainText() for identity in ids.values())
    assert not any(calls.values()) and not window.roblox.busy and not window.x_panel.busy
    dialog.checks["X"].setChecked(False)
    assert not dialog.results and not dialog.output.toPlainText()
    dialog.checks["Roblox"].setChecked(False)
    dialog.check_now()
    assert "محظورة" in dialog.summary.text()
    dialog.close()


def test_manager_selection_is_the_explicit_intended_identity(concurrent_window, qtbot):
    window, _, _, _, ids = concurrent_window
    select(window.job_manager, "Roblox", ids["roblox"])
    dialog = OvernightPreflightDialog(window.job_manager)
    qtbot.addWidget(dialog)
    assert dialog.identities["Roblox"] == ids["roblox"]


def test_x_restriction_is_blocked_without_blocking_roblox(concurrent_window, qtbot):
    window, _, _, calls, ids = concurrent_window
    for name, panel in (("roblox", window.roblox), ("x", window.x_panel)):
        panel.current_run = ids[name]
        panel.overnight.enabled.setChecked(True)
    with window.store.transaction() as db:
        db.execute("UPDATE x_control SET value=?", (json.dumps({"blocked": "unexpected_http_status:429"}),))
    dialog = OvernightPreflightDialog(window.job_manager)
    qtbot.addWidget(dialog)
    dialog.check_now()
    qtbot.waitUntil(dialog.check_button.isEnabled)
    by_platform = {result.platform: result for result in dialog.results}
    assert by_platform["X"].errors and not by_platform["Roblox"].errors
    assert "1/2" in dialog.summary.text()
    assert not any(calls.values())


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_progress_burst_coalesces_expensive_refreshes(concurrent_window, qtbot, monkeypatch, platform):
    window, _, _, _, _ = concurrent_window
    panel = window.job_manager.panels[platform]
    calls = []
    original = panel.repo.counts

    def counted():
        calls.append(1)
        return original()

    monkeypatch.setattr(panel.repo, "counts", counted)
    for _ in range(25):
        panel.progress_changed.emit()
    assert not calls
    qtbot.waitUntil(lambda: bool(calls))
    assert len(calls) == 1


def test_sleep_request_is_scoped_to_opted_in_active_jobs_and_gui_thread(concurrent_window, qtbot):
    window, events, entered, _, ids = concurrent_window
    manager = window.job_manager
    calls = []
    gui_thread = threading.get_ident()
    manager.awake = AwakeRequest(api=lambda flags: calls.append((flags, threading.get_ident())) or 1)
    manager.keep_awake.setChecked(True)
    assert not calls
    for panel in manager.panels.values():
        panel.overnight.enabled.setChecked(True)
    start_both(qtbot, window, entered, ids)
    qtbot.waitUntil(lambda: manager.awake.active)
    window.tabs.setCurrentWidget(window.roblox)
    assert calls == [(ES_CONTINUOUS | ES_SYSTEM_REQUIRED, gui_thread)]
    window.roblox.stop()
    events["roblox"].set()
    qtbot.waitUntil(lambda: not window.roblox.busy)
    manager.update_power()
    assert manager.awake.active
    window.x_panel.stop()
    events["x"].set()
    qtbot.waitUntil(lambda: not window.x_panel.busy)
    manager.update_power()
    assert calls[-1] == (ES_CONTINUOUS, gui_thread) and not manager.awake.active


def test_nonnight_jobs_and_disabled_option_do_not_request_power(concurrent_window, qtbot):
    window, events, entered, _, ids = concurrent_window
    calls = []
    window.job_manager.awake = AwakeRequest(api=lambda flags: calls.append(flags) or 1)
    window.job_manager.keep_awake.setChecked(True)
    start_both(qtbot, window, entered, ids)
    window.job_manager.update_power()
    assert not calls
    window.roblox.overnight.enabled.setChecked(True)
    qtbot.waitUntil(lambda: window.job_manager.awake.active)
    window.job_manager.keep_awake.setChecked(False)
    assert calls[-1] == ES_CONTINUOUS
    for event in events.values():
        event.set()
    qtbot.waitUntil(lambda: not window.roblox.busy and not window.x_panel.busy)


def test_readiness_manager_and_dialog_windows_sizes(concurrent_window, qtbot, tmp_path):
    window, _, _, _, ids = concurrent_window
    window.roblox.current_run = ids["roblox"]
    window.roblox.overnight.enabled.setChecked(True)
    window.job_manager.refresh()
    dialog = OvernightPreflightDialog(window.job_manager)
    qtbot.addWidget(dialog)
    for width, height in ((900, 650), (1180, 850), (1500, 1000)):
        window.resize(width, height)
        window.tabs.setCurrentWidget(window.job_manager)
        qtbot.wait(20)
        assert window.width() <= width and window.height() <= height
        for control in (
            window.job_manager.preflight_button,
            window.job_manager.keep_awake,
            window.job_manager.start_button,
            window.job_manager.stop_button,
        ):
            assert (
                window.centralWidget()
                .rect()
                .contains(QRect(control.mapTo(window.centralWidget(), QPoint()), control.size()))
            )
        assert window.job_manager.layoutDirection() == Qt.LayoutDirection.RightToLeft
        if os.environ.get("UFB_SCREENSHOTS"):
            path = tmp_path / f"preflight-manager-{width}.png"
            window.grab().save(str(path))
            print(f"PREFLIGHT_DPR={window.devicePixelRatioF()} SCREENSHOT={path}")
    dialog.show()
    dialog.check_now()
    qtbot.waitUntil(dialog.check_button.isEnabled)
    assert dialog.width() <= 900 and dialog.height() <= 650
    assert dialog.layoutDirection() == Qt.LayoutDirection.RightToLeft
    if os.environ.get("UFB_SCREENSHOTS"):
        path = tmp_path / "preflight-dialog.png"
        dialog.grab().save(str(path))
        print(f"PREFLIGHT_DIALOG={path}")
    dialog.close()


@pytest.mark.skipif(not os.environ.get("UFB_NATIVE_POWER"), reason="Explicit native power smoke test only")
def test_native_windows_power_for_mocked_night_job(concurrent_window, qtbot):
    window, events, entered, _, ids = concurrent_window
    manager = window.job_manager
    assert manager.awake.api is not None
    window.roblox.overnight.enabled.setChecked(True)
    manager.keep_awake.setChecked(True)
    window.roblox._start_run(ids["roblox"])
    qtbot.waitUntil(entered["roblox"].is_set)
    qtbot.waitUntil(lambda: manager.awake.active)
    assert not manager.awake.error
    events["roblox"].set()
    qtbot.waitUntil(lambda: not window.roblox.busy)
    manager.update_power()
    assert not manager.awake.active and not manager.awake.error
