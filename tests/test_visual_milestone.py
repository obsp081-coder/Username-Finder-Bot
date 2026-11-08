"""Milestone-one presentation, bounded-data, safety, and native visual coverage."""

import os
import threading

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel
from test_desktop_design import desktop as desktop
from test_roblox import response
from test_ui_quality import wheel

from username_finder import roblox_ui
from username_finder.roblox_runner import RobloxRunner


def test_dashboard_cards_metrics_and_safe_shortcuts(desktop, qtbot):
    dashboard = desktop.dashboard
    assert [m.value.text() for m in dashboard.metrics] == ["0"] * 4
    assert dashboard.active_empty.isVisible()
    for action in dashboard.quick_actions:
        assert action.sizeHint().height() >= 96
        assert all(label.height() > 0 for label in action.findChildren(QLabel))
    qtbot.mouseClick(dashboard.quick_actions[0], Qt.MouseButton.LeftButton)
    assert desktop.tabs.currentWidget() is desktop.roblox
    assert desktop.roblox.modes.currentIndex() == 1
    assert not desktop.roblox.repo.runs()
    dashboard.quick_actions[2].click()
    assert desktop.roblox.checking_mode.currentIndex() == 2
    assert not desktop.roblox.prefilter.service
    assert not desktop.roblox.repo.runs()


def test_mode_strip_keeps_original_enabled_guard_and_wheel_protection(desktop, qtbot):
    panel = desktop.roblox
    desktop.tabs.setCurrentWidget(panel)
    panel.checking_mode.setCurrentIndex(0)
    for delta in (-120, 120):
        wheel(panel.mode_strip, delta)
        assert panel.checking_mode.currentIndex() == 0
    panel.checking_mode.setEnabled(False)
    assert not panel.mode_strip.isEnabled()
    qtbot.mouseClick(panel.mode_strip, Qt.MouseButton.LeftButton, pos=panel.mode_strip.tabRect(2).center())
    assert panel.checking_mode.currentIndex() == 0
    panel.checking_mode.setEnabled(True)
    qtbot.mouseClick(panel.mode_strip, Qt.MouseButton.LeftButton, pos=panel.mode_strip.tabRect(2).center())
    assert panel.checking_mode.currentIndex() == 2
    assert not panel.repo.runs()


def test_dashboard_stop_is_explicit_and_routes_to_correct_tool(desktop, monkeypatch):
    panel = desktop.roblox
    card = desktop.dashboard.operation_cards[0]
    called = []
    monkeypatch.setattr(panel.prefilter, "stop", lambda: called.append("prefilter"))
    panel.prefilter.busy = True
    try:
        desktop.dashboard.refresh()
        assert not called and card.stop_button.isEnabled()
        card.stop_button.click()
        assert called == ["prefilter"]
    finally:
        panel.prefilter.busy = False
    desktop.dashboard.refresh()
    assert not card.stop_button.isEnabled()


def test_operation_columns_fill_space_and_keep_manual_width(desktop, qtbot):
    manager = desktop.job_manager
    desktop.roblox.repo.create_run(["ExampleOne", "ExampleTwo"])
    desktop.tabs.setCurrentWidget(manager)
    manager.refresh()
    desktop.resize(1500, 1000)
    qtbot.waitUntil(
        lambda: sum(manager.table.columnWidth(c) for c in range(6)) >= manager.table.viewport().width() - 2
    )
    manager.table.setColumnWidth(2, 310)
    manager.refresh()
    qtbot.wait(25)
    assert manager.table.columnWidth(2) == 310
    manager.table.setCurrentCell(0, 0)
    assert manager.start_button.isEnabled()
    assert not manager.stop_button.isEnabled()


def test_advanced_results_do_not_keep_simple_stretched_width(desktop, qtbot):
    panel = desktop.roblox
    identity = panel.repo.create_run(["ExampleOne"])
    RobloxRunner(panel.repo, fetcher=lambda _: response(), wait=lambda _: False).run(identity)
    desktop.resize(1500, 1000)
    desktop.tabs.setCurrentWidget(panel)
    panel.workspace.setCurrentIndex(1)
    panel.refresh()
    qtbot.wait(30)
    panel.refresh()
    panel.results_presentation.mode.setCurrentIndex(1)
    qtbot.wait(30)
    assert panel.result_table.columnWidth(1) < panel.result_table.viewport().width() // 2
    assert (
        sum(panel.result_table.columnWidth(c) for c in range(7)) >= panel.result_table.viewport().width() - 2
    )


@pytest.mark.parametrize("width,height", [(900, 650), (1180, 850), (1500, 1000)])
def test_milestone_rendered_workflows(desktop, qtbot, tmp_path, monkeypatch, width, height):
    window, panel = desktop, desktop.roblox
    path = tmp_path / "local-sample.txt"
    path.write_text("ExampleOne\nExampleTwo\nExampleThree\nExampleFour", encoding="utf8")
    completed = panel.repo.create_run(["HistoryOne", "HistoryTwo"])
    RobloxRunner(panel.repo, fetcher=lambda _: response(), wait=lambda _: False).run(completed)
    panel.refresh()
    panel.repo.create_run(["PendingOne", "PendingTwo"])
    active = panel.repo.create_run([f"ActiveName{i}" for i in range(5)])
    release, entered = threading.Event(), threading.Event()
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) == 3:
            entered.set()
            assert release.wait(45), "The test must release its mock request"
        return response()

    panel.fetcher = fetch
    monkeypatch.setattr(
        roblox_ui, "RobloxRunner", lambda *a, **kw: RobloxRunner(*a, wait=lambda _: False, **kw)
    )
    panel._start_run(active)

    def capture(name):
        qtbot.wait(60)
        assert window.width() == width
        assert window.sidebar.mapTo(window, QPoint()).x() > window.tabs.mapTo(window, QPoint()).x()
        if os.environ.get("UFB_SCREENSHOTS"):
            evidence = tmp_path / f"{name}-{width}.png"
            assert window.grab().save(str(evidence))
            print("MILESTONE_SCREENSHOT=" + str(evidence))

    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        window.resize(width, height)
        window.statusBar().showMessage("معاينة ببيانات اختبار محاكية محلية — لا طلبات فحص حية")
        panel.refresh()
        window.job_manager.refresh()
        window.tabs.setCurrentWidget(window.dashboard)
        window.dashboard.refresh()
        assert window.dashboard.metrics[0].value.text() == "1"
        assert window.dashboard.operation_cards[0].progress.value() == 400
        capture("dashboard-active")
        window.tabs.setCurrentWidget(panel)
        panel.workspace.setCurrentIndex(0)
        capture("roblox-individual")
        assert panel.speed.height() >= 30
        for action in (panel.start_button, panel.stop_button):
            point = action.mapTo(panel.viewport(), action.rect().center())
            assert panel.viewport().rect().contains(point)
        panel.workspace.setCurrentIndex(1)
        capture("results-simple")
        panel.results_presentation.mode.setCurrentIndex(1)
        capture("results-advanced")
        window.tabs.setCurrentWidget(window.job_manager)
        manager = window.job_manager
        row = next(i for i, r in enumerate(manager.rows) if r["id"] == active)
        manager.table.setCurrentCell(row, 0)
        capture("operations-populated")
        assert manager.stop_button.isEnabled() and not manager.start_button.isEnabled()
        assert manager.table.cellWidget(row, 3).width() <= manager.table.columnWidth(3)
        point = manager.stop_button.mapTo(manager, manager.stop_button.rect().center())
        assert manager.rect().contains(point)
    finally:
        panel.stop()
        release.set()
        qtbot.waitUntil(lambda: not panel.busy, timeout=5000)
    assert len(calls) == 3
    window.tabs.setCurrentWidget(panel)
    panel.checking_mode.setCurrentIndex(2)
    prefilter = panel.prefilter
    prefilter.source.setText(str(path))
    prefilter.prepare()
    qtbot.waitUntil(lambda: not prefilter.busy, timeout=5000)
    prefilter.steps_area.verticalScrollBar().setValue(0)
    capture("roblox-prefilter")
    assert prefilter.snapshot["counts"]["attempts"] == 0
    assert not prefilter.buttons["full"].isEnabled()
    assert panel.horizontalScrollBar().maximum() == 0
