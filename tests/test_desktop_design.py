"""Presentation checks use temporary stores and never contact platform providers."""

import os
import time

import pytest
from legacy_roblox_shell import MainWindow
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import QMessageBox

from username_finder.saved_results_ui import SavedResultsDialog


@pytest.fixture
def desktop(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    window = MainWindow(tmp_path / "desktop.db")
    monkeypatch.setattr(window.instagram.analysis, "activate", lambda: None)
    window.timer.stop()
    qtbot.addWidget(window)
    window.show()
    yield window
    window.close()
    qtbot.waitUntil(lambda: window.closed, timeout=5000)


def test_sidebar_default_rtl_and_page_identity(desktop, qtbot):
    window = desktop
    assert window.tabs.currentWidget() is window.dashboard
    assert not window.tabs.tabBar().isVisible()
    assert window.tabs.count() == 8
    assert (
        sum(
            window.navigation.item(i).data(Qt.ItemDataRole.UserRole) is not None
            for i in range(window.navigation.count())
        )
        == 8
    )
    assert window.palette().window().color().lightness() > 200
    pages = [window.search_tabs.widget(i) for i in range(4)]
    window.roblox.manual.setPlainText("KeepThisDraft")
    for row in range(window.navigation.count()):
        item = window.navigation.item(row)
        if item.data(Qt.ItemDataRole.UserRole) is None:
            continue
        qtbot.mouseClick(
            window.navigation.viewport(),
            Qt.MouseButton.LeftButton,
            pos=window.navigation.visualItemRect(item).center(),
        )
        assert window.tabs.currentIndex() == item.data(Qt.ItemDataRole.UserRole)
    assert pages == [window.search_tabs.widget(i) for i in range(4)]
    assert window.roblox.manual.toPlainText() == "KeepThisDraft"
    assert window.sidebar.mapTo(window, QPoint()).x() > window.tabs.mapTo(window, QPoint()).x()
    window.tabs.setCurrentWidget(window.job_manager)
    assert window.navigation.currentItem().data(Qt.ItemDataRole.UserRole) == 6
    window.navigation.setFocus()
    qtbot.keyClick(window.navigation, Qt.Key.Key_Down)
    assert window.tabs.currentWidget() is window.file_generator


def test_dashboard_refresh_has_no_database_or_provider_work(desktop, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dashboard must use bounded existing view data")

    for panel in (desktop.roblox, desktop.x_panel):
        monkeypatch.setattr(panel.repo, "progress", forbidden)
        monkeypatch.setattr(panel.repo, "control", forbidden)
        panel.fetcher = forbidden
    monkeypatch.setattr(desktop.job_manager, "snapshot", forbidden)
    desktop.dashboard.refresh()
    assert "غير نشط" in desktop.dashboard.activity.text()
    assert "لا توجد" in desktop.dashboard.recent.text()
    assert desktop.dashboard.update_timer.isSingleShot()


def test_dashboard_activity_refresh_is_coalesced(desktop, qtbot):
    desktop.roblox.progress_changed.emit()
    assert desktop.dashboard.update_timer.isActive()
    qtbot.waitUntil(lambda: not desktop.dashboard.update_timer.isActive())
    desktop.tabs.setCurrentWidget(desktop.file_generator)
    desktop.roblox.progress_changed.emit()
    assert not desktop.dashboard.update_timer.isActive()


@pytest.mark.parametrize("target", ["snapchat", "roblox", "x_panel", "instagram", "bulk", "saved"])
def test_results_views_only_change_columns(desktop, qtbot, target):
    dialog = None
    if target == "saved":
        panel = dialog = SavedResultsDialog(desktop)
        qtbot.addWidget(dialog)
    else:
        panel = (
            desktop
            if target == "snapchat"
            else desktop.roblox.bulk
            if target == "bulk"
            else getattr(desktop, target)
        )
    view = panel.results_presentation
    table = view.table
    desktop.fill_table(table, [[f"Evidence {column}" for column in range(table.columnCount())]])
    table.setCurrentCell(0, 0)
    table.setColumnWidth(0, 215)
    items = [table.item(0, column) for column in range(table.columnCount())]
    changes = desktop.store.db.total_changes
    for _ in range(3):
        view.mode.setCurrentIndex(1)
        assert all(not table.isColumnHidden(c) for c in range(table.columnCount()))
        view.mode.setCurrentIndex(0)
        assert {c for c in range(table.columnCount()) if not table.isColumnHidden(c)} == view.simple_columns
    assert table.columnWidth(0) == 215
    assert table.currentRow() == 0
    assert items == [table.item(0, column) for column in range(table.columnCount())]
    assert desktop.store.db.total_changes == changes
    if dialog:
        dialog.reject()


def test_navigation_wheel_does_not_select_pages(desktop):
    from test_ui_quality import wheel

    desktop.navigation.setCurrentRow(0)
    for delta in (-120, 120):
        wheel(desktop.navigation.viewport(), delta)
        assert desktop.navigation.currentRow() == 0
        assert desktop.tabs.currentWidget() is desktop.dashboard


def test_prefilter_steps_keep_authorization_guards(desktop):
    panel = desktop.roblox.prefilter
    assert list(panel.steps) == ["import", "config", "prepare", "pilot", "review", "full", "export"]
    assert not panel.buttons["pilot"].isEnabled()
    assert not panel.buttons["full"].isEnabled()
    assert not panel.buttons["stop"].isEnabled()
    assert not desktop.roblox.repo.runs()
    assert not desktop.x_panel.repo.runs()


@pytest.mark.parametrize("width,height", [(900, 650), (1180, 850)])
def test_desktop_visual_sizes(desktop, qtbot, tmp_path, width, height):
    desktop.resize(width, height)
    qtbot.wait(20)
    assert desktop.navigation.verticalScrollBar().maximum() == 0
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start(10)
    start = time.perf_counter()
    for name in (
        "dashboard",
        "snapchat_tabs",
        "roblox",
        "x_panel",
        "instagram",
        "file_generator",
        "file_search",
        "job_manager",
    ):
        page = getattr(desktop, name)
        desktop.tabs.setCurrentWidget(page)
        qtbot.wait(25)
        assert desktop.size().width() == width
        assert page.isVisible()
        assert desktop.navigation.isVisible()
        if hasattr(page, "horizontalScrollBar"):
            assert page.horizontalScrollBar().maximum() == 0
            assert page.widget().grab().toImage().pixelColor(3, 3).lightness() > 200
        if os.environ.get("UFB_SCREENSHOTS"):
            path = tmp_path / f"{name}-{width}.png"
            assert desktop.grab().save(str(path))
            print("GUI_SCREENSHOT=" + str(path))
    timer.stop()
    assert len(ticks) >= 7
    print(
        f"GUI_NAVIGATION width={width} elapsed={time.perf_counter() - start:.4f}s "
        f"ticks={len(ticks)} DPR={desktop.devicePixelRatioF()}"
    )


@pytest.mark.parametrize("width,height", [(900, 650), (1180, 850)])
def test_prefilter_scrolling_keeps_stop_visible(desktop, qtbot, tmp_path, width, height):
    desktop.resize(width, height)
    desktop.tabs.setCurrentWidget(desktop.roblox)
    desktop.roblox.checking_mode.setCurrentIndex(2)
    panel = desktop.roblox.prefilter
    qtbot.wait(30)
    assert desktop.roblox.horizontalScrollBar().maximum() == 0
    assert panel.steps_area.horizontalScrollBar().maximum() == 0
    panel.steps_area.verticalScrollBar().setValue(panel.steps_area.verticalScrollBar().maximum())
    assert panel.buttons["stop"].isVisible()
    point = panel.buttons["stop"].mapTo(desktop.roblox.viewport(), panel.buttons["stop"].rect().center())
    assert desktop.roblox.viewport().rect().contains(point)
    assert not panel.buttons["pilot"].isEnabled() and not panel.buttons["full"].isEnabled()
    if os.environ.get("UFB_SCREENSHOTS"):
        path = tmp_path / f"prefilter-{width}.png"
        assert desktop.grab().save(str(path))
        print("GUI_SCREENSHOT=" + str(path))


@pytest.mark.parametrize("width,height", [(900, 650), (1180, 850)])
def test_populated_results_and_tool_modes_sizes(desktop, qtbot, tmp_path, width, height):
    from test_roblox import response

    from username_finder.roblox_runner import RobloxRunner

    panel = desktop.roblox
    calls = []
    identity = panel.repo.create_run(["MockUserOne", "MockUserTwo"])
    RobloxRunner(
        panel.repo, fetcher=lambda url: (calls.append(url), response())[1], wait=lambda _: False
    ).run(identity)
    assert len(calls) == 2
    panel.current_run = identity
    panel.refresh()
    desktop.job_manager.refresh()
    desktop.resize(width, height)
    for name, page, configure in (
        ("results-simple", panel, lambda: panel.workspace.setCurrentIndex(1)),
        ("results-advanced", panel, lambda: panel.results_presentation.mode.setCurrentIndex(1)),
        ("operations-populated", desktop.job_manager, lambda: None),
        ("bulk-frozen", panel, lambda: panel.checking_mode.setCurrentIndex(1)),
        ("snapscore", desktop.snapchat_tabs, lambda: desktop.snapchat_tabs.setCurrentIndex(1)),
        ("osintgram", desktop.instagram, lambda: desktop.instagram.modes.setCurrentIndex(1)),
    ):
        desktop.tabs.setCurrentWidget(page)
        configure()
        qtbot.wait(25)
        assert desktop.width() == width
        if name.startswith("results-"):
            table = panel.result_table
            area = panel.workspace.currentWidget()
            point = table.viewport().mapTo(area.viewport(), table.visualItemRect(table.item(0, 0)).center())
            assert area.viewport().rect().contains(point)
            if name == "results-advanced":
                assert all(not table.isColumnHidden(c) for c in range(table.columnCount()))
                assert table.columnWidth(1) < table.viewport().width() // 2
        if hasattr(page, "horizontalScrollBar"):
            assert page.horizontalScrollBar().maximum() == 0
        if os.environ.get("UFB_SCREENSHOTS"):
            path = tmp_path / f"{name}-{width}.png"
            assert desktop.grab().save(str(path))
            print("GUI_SCREENSHOT=" + str(path))
    assert len(calls) == 2  # Presentation and navigation never recheck.
