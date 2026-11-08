import os
import threading

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QMessageBox
from test_roblox import response as roblox_response
from test_x import response as x_response

from username_finder import roblox_ui, x, x_ui
from username_finder.gui import MainWindow
from username_finder.platform_workspace import ReadableColumns
from username_finder.roblox_runner import RobloxRunner
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store
from username_finder.x_runner import XRunner
from username_finder.x_storage import XStore


@pytest.fixture
def concurrent_window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    monkeypatch.setattr(
        roblox_ui, "RobloxRunner", lambda *a, **kw: RobloxRunner(*a, wait=lambda _: False, **kw)
    )
    monkeypatch.setattr(x_ui, "XRunner", lambda *a, **kw: XRunner(*a, wait=lambda _: False, **kw))
    window = MainWindow(tmp_path / "concurrent.db")
    qtbot.addWidget(window)
    window.show()
    events = {key: threading.Event() for key in ("roblox", "x")}
    entered = {key: threading.Event() for key in events}
    calls = {key: [] for key in events}

    def fetch(key, *args):
        calls[key].append(args[0])
        entered[key].set()
        assert events[key].wait(5), "Test must release the controlled response"
        return roblox_response() if key == "roblox" else x_response()

    window.roblox.fetcher = lambda *args: fetch("roblox", *args)
    window.x_panel.fetcher = lambda *args: fetch("x", *args)
    window.roblox.repo.set_preferred_interval(0)
    window.x_panel.repo.set_interval(0)
    identities = {
        "roblox": window.roblox.repo.create_run(["RobloxOne", "RobloxTwo"]),
        "x": window.x_panel.repo.prepare(x.input_spec("x_first\nx_second")),
    }
    yield window, events, entered, calls, identities
    for event in events.values():
        event.set()
    qtbot.waitUntil(lambda: not window.roblox.busy and not window.x_panel.busy, timeout=5000)
    window.close()
    qtbot.waitUntil(lambda: window.closed, timeout=5000)


def start_both(qtbot, window, entered, identities):
    window.roblox._start_run(identities["roblox"])
    qtbot.waitUntil(entered["roblox"].is_set)
    assert window.x_panel.start_button.isEnabled()
    window.x_panel._start_run(identities["x"])
    qtbot.waitUntil(entered["x"].is_set)
    assert window.roblox.busy and window.x_panel.busy
    assert not window.roblox.start_button.isEnabled() and not window.x_panel.start_button.isEnabled()


@pytest.mark.parametrize("stopped", ["roblox", "x"])
def test_existing_independent_workers_cancel_one_while_other_completes(concurrent_window, qtbot, stopped):
    window, events, entered, calls, ids = concurrent_window
    panels = {"roblox": window.roblox, "x": window.x_panel}
    start_both(qtbot, window, entered, ids)
    other = "x" if stopped == "roblox" else "roblox"
    panels[stopped].stop()
    events[stopped].set()
    qtbot.waitUntil(lambda: not panels[stopped].busy)
    assert panels[stopped].repo.progress(ids[stopped])["state"] == "Paused"
    assert len(calls[stopped]) == 1 and panels[other].busy
    events[other].set()
    qtbot.waitUntil(lambda: not panels[other].busy)
    assert panels[other].repo.progress(ids[other])["state"] == "Completed"
    assert len(calls[other]) == 2
    panels[stopped].resume()
    qtbot.waitUntil(lambda: not panels[stopped].busy)
    assert panels[stopped].repo.progress(ids[stopped])["state"] == "Completed"
    assert len(calls[stopped]) == len(set(calls[stopped])) == 2
    assert window.store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not window.store.db.execute("PRAGMA foreign_key_check").fetchall()


def select(manager, platform, identity):
    manager.refresh()
    index = next(
        i for i, row in enumerate(manager.rows) if row["platform"] == platform and row["id"] == identity
    )
    manager.table.setCurrentCell(index, 0)
    manager.show_selected()


def test_manager_independent_progress_and_stop_actions(concurrent_window, qtbot):
    window, events, entered, calls, ids = concurrent_window
    manager = window.job_manager
    window.tabs.setCurrentWidget(manager)
    start_both(qtbot, window, entered, ids)
    select(manager, "Roblox", ids["roblox"])
    assert len(manager.rows) == 2 and manager.stop_button.isEnabled()
    assert manager.selected()["done"] == 0 and manager.selected()["total"] == 2
    manager.stop_button.click()
    assert manager.selected()["display_state"] == "جارٍ الإيقاف"
    assert window.x_panel.busy and not window.x_panel.cancel.is_set()
    events["roblox"].set()
    qtbot.waitUntil(lambda: not window.roblox.busy)
    manager.refresh()
    assert manager.selected()["done"] == 1 and manager.selected()["classifications"] == {"Available": 1}
    assert manager.start_button.isEnabled() and "متاح أوليًا: 1" in manager.details.text()
    select(manager, "X", ids["x"])
    assert manager.selected()["done"] == 0 and manager.stop_button.isEnabled()
    events["x"].set()
    qtbot.waitUntil(lambda: not window.x_panel.busy)
    manager.refresh()
    assert manager.selected()["done"] == 2 and manager.selected()["classifications"] == {"Taken": 2}
    assert not manager.start_button.isEnabled() and not manager.stop_button.isEnabled()
    select(manager, "Roblox", ids["roblox"])
    manager.start_button.click()
    qtbot.waitUntil(lambda: not window.roblox.busy)
    assert len(calls["roblox"]) == 2 and len(set(calls["roblox"])) == 2


@pytest.mark.parametrize("failed", ["roblox", "x"])
def test_platform_failure_does_not_stop_other_platform(concurrent_window, qtbot, failed):
    window, events, entered, calls, ids = concurrent_window
    panels = {"roblox": window.roblox, "x": window.x_panel}

    def failure(*args):
        entered[failed].set()
        assert events[failed].wait(5)
        if failed == "x":
            return x.protocol.Response(429, b"", {"retry-after": "120"})
        raise TimeoutError

    panels[failed].fetcher = failure
    start_both(qtbot, window, entered, ids)
    events[failed].set()
    qtbot.waitUntil(lambda: not panels[failed].busy)
    other = "x" if failed == "roblox" else "roblox"
    assert panels[other].busy
    events[other].set()
    qtbot.waitUntil(lambda: not panels[other].busy)
    assert panels[other].repo.progress(ids[other])["state"] == "Completed"
    assert len(calls[other]) == 2 and not panels[other].repo.control().get("blocked")
    if failed == "x":
        assert window.x_panel.repo.control()["blocked"] == "unexpected_http_status:429"


def test_second_same_platform_start_cannot_replace_active_runner(concurrent_window, qtbot):
    window, events, entered, calls, ids = concurrent_window
    other_ids = {
        "roblox": window.roblox.repo.create_run(["OtherRoblox"]),
        "x": window.x_panel.repo.prepare(x.input_spec("other_x")),
    }
    start_both(qtbot, window, entered, ids)
    for key, panel in (("roblox", window.roblox), ("x", window.x_panel)):
        runner = panel.runner
        panel._start_run(other_ids[key])
        assert panel.runner is runner and panel.current_run == ids[key]
        with pytest.raises(ValueError):
            panel.repo.start(other_ids[key])
        assert panel.repo.progress(other_ids[key])["state"] == "Ready"
        assert len(calls[key]) == 1
    for event in events.values():
        event.set()
    qtbot.waitUntil(lambda: not window.roblox.busy and not window.x_panel.busy)


def test_both_active_jobs_close_durably_and_reopen_as_paused(concurrent_window, qtbot):
    window, events, entered, calls, ids = concurrent_window
    start_both(qtbot, window, entered, ids)
    path = window.store.path
    window.close()
    assert window.closing and not window.closed
    for event in events.values():
        event.set()
    qtbot.waitUntil(lambda: window.closed)
    store = Store(path)
    try:
        x_repo = XStore(store)
        roblox_repo = RobloxStore(store)
        x_repo.recover()
        roblox_repo.recover()
        for key, repo in (("roblox", roblox_repo), ("x", x_repo)):
            assert repo.progress(ids[key])["state"] == "Paused"
            assert repo.progress(ids[key])["interval_seconds"] == 0
            assert repo.results()[1] == 1 and len(calls[key]) == 1
        assert not x_repo.control().get("blocked")
    finally:
        store.close()


def test_interrupted_inflight_jobs_keep_uncertain_results_without_requests(qtbot, tmp_path, monkeypatch):
    path = tmp_path / "crashed.db"
    store = Store(path)
    roblox, x_repo = RobloxStore(store), XStore(store)
    r_id = roblox.create_run(["OldRoblox", "NextRoblox"])
    x_id = x_repo.prepare(x.input_spec("old_xname\nnext_xname"))
    roblox.start(r_id)
    x_repo.start(x_id)
    roblox.claim_next(r_id)
    x_repo.claim(x_id)
    store.close()
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    window = MainWindow(path)
    qtbot.addWidget(window)
    try:
        rows, _ = window.job_manager.snapshot()
        assert all(r["done"] == 1 and r["classifications"] == {"Unknown": 1} for r in rows)
        assert window.x_panel.repo.progress(x_id)["state"] == "Restricted"
        assert window.roblox.repo.progress(r_id)["state"] == "Paused"
        assert window.x_panel.repo.control()["blocked"] == "interrupted_delivery_uncertain"
        assert not window.x_panel.recovery.status()["eligible"]
        window.roblox.repo.recover()
        window.x_panel.repo.recover()
        assert window.roblox.repo.results()[1] == window.x_panel.repo.results()[1] == 1
    finally:
        window.close()
        qtbot.waitUntil(lambda: window.closed)


def test_manager_history_paging_and_open_preserve_selection(concurrent_window):
    window, _, _, _, ids = concurrent_window
    manager = window.job_manager
    manager.page_size = 1
    manager.refresh()
    first = manager.rows[0]["id"]
    manager.table.setCurrentCell(0, 0)
    manager.page(1)
    assert len(manager.rows) == 1 and manager.rows[0]["id"] != first
    manager.table.setCurrentCell(0, 0)
    selected = manager.selected()
    manager.open_selected()
    panel = manager.panels[selected["platform"]]
    assert panel.current_run == selected["id"]
    assert window.tabs.currentWidget() is panel and panel.workspace.currentIndex() == 2
    assert len(panel.repo.runs()) >= 1


def test_simultaneous_sqlite_writes_preserve_every_unique_result(concurrent_window, qtbot):
    window, _, _, _, _ = concurrent_window
    barrier = threading.Barrier(2)
    calls = {"Roblox": [], "X": []}

    def fetch(platform, value):
        calls[platform].append(value)
        barrier.wait(timeout=5)
        return roblox_response() if platform == "Roblox" else x_response()

    window.roblox.fetcher = lambda value: fetch("Roblox", value)
    window.x_panel.fetcher = lambda value, timeout: fetch("X", value)
    names = [f"Candidate{index:02}" for index in range(25)]
    r_id = window.roblox.repo.create_run(spec=x.input_spec("\n".join(names + [names[0].upper()])))
    x_id = window.x_panel.repo.prepare(x.input_spec("\n".join(names + [names[0].upper()])))
    window.roblox._start_run(r_id)
    window.x_panel._start_run(x_id)
    qtbot.waitUntil(lambda: not window.roblox.busy and not window.x_panel.busy, timeout=10000)
    for key, repo, identity in (("Roblox", window.roblox.repo, r_id), ("X", window.x_panel.repo, x_id)):
        assert len(calls[key]) == len(set(calls[key])) == 25
        assert repo.progress(identity)["state"] == "Completed" and repo.progress(identity)["duplicates"] == 1
        assert repo.results()[1] == 25
    assert window.store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not window.store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_workspace_navigation_supported_sizes_and_scrolling(concurrent_window, qtbot, tmp_path):
    window, _, _, _, _ = concurrent_window
    assert window.tabs.count() == 8
    assert window.job_manager.layoutDirection() == Qt.LayoutDirection.RightToLeft
    if os.environ.get("UFB_SCREENSHOTS"):
        print("GUI_DPR=" + str(window.devicePixelRatioF()))
    for width, height in ((900, 650), (1180, 850), (1500, 1000)):
        window.resize(width, height)
        for key, panel in (("roblox", window.roblox), ("x", window.x_panel)):
            window.tabs.setCurrentWidget(panel)
            for index in range(panel.workspace.count()):
                panel.workspace.setCurrentIndex(index)
                qtbot.wait(20)
                area = panel.workspace.currentWidget()
                assert panel.horizontalScrollBar().maximum() == 0
                assert area.horizontalScrollBar().maximum() == 0
                assert panel.start_button.isVisible() and panel.stop_button.isVisible()
                assert panel.activity_status.isVisible()
                for control in (panel.start_button, panel.stop_button, panel.activity_status):
                    bounds = QRect(control.mapTo(panel.viewport(), QPoint()), control.size())
                    assert panel.viewport().rect().contains(bounds)
            panel.workspace.setCurrentIndex(0)
            if os.environ.get("UFB_SCREENSHOTS"):
                path = tmp_path / f"{key}-{width}.png"
                window.grab().save(str(path))
                print("GUI_SCREENSHOT=" + str(path))
        window.tabs.setCurrentWidget(window.job_manager)
        qtbot.wait(20)
        assert window.job_manager.table.isVisible()
        if os.environ.get("UFB_SCREENSHOTS"):
            path = tmp_path / f"jobs-{width}.png"
            window.grab().save(str(path))
            print("GUI_SCREENSHOT=" + str(path))


def test_landing_page_and_compact_fields(concurrent_window, qtbot):
    window, _, _, _, _ = concurrent_window
    assert window.tabs.currentWidget() is window.dashboard
    for size in ((900, 650), (1180, 850), (1500, 1000)):
        window.resize(*size)
        for panel in (window.roblox, window.x_panel):
            window.tabs.setCurrentWidget(panel)
            panel.workspace.setCurrentIndex(0)
            qtbot.wait(20)
            group = panel.minimum_length.parentWidget()
            assert group is panel.maximum_length.parentWidget()
            assert group.width() <= 580
            assert group.geometry().right() > panel.modes.currentWidget().width() - 40
            assert abs(panel.minimum_length.y() - panel.maximum_length.y()) < 5
            assert panel.speed.width() < 500
            page_layout = panel.workspace.currentWidget().widget().layout()
            assert page_layout.alignment() & Qt.AlignmentFlag.AlignTop
            assert panel.preview_label.height() < 70


def test_manager_readable_columns_preserve_user_resize_and_selection(concurrent_window):
    window, _, _, _, _ = concurrent_window
    manager = window.job_manager
    manager.refresh()
    manager.table.setCurrentCell(0, 0)
    key = manager.selected()["id"]
    for column in range(manager.table.columnCount()):
        assert manager.table.columnWidth(column) >= manager.table.sizeHintForColumn(column)
        assert manager.table.item(0, column).toolTip()
    manager.table.setColumnWidth(2, 420)
    manager.refresh()
    assert manager.selected()["id"] == key
    assert manager.table.columnWidth(2) == 420


def test_manager_progress_uses_stored_counts(concurrent_window, qtbot):
    window, events, entered, _, ids = concurrent_window
    window.roblox._start_run(ids["roblox"])
    qtbot.waitUntil(entered["roblox"].is_set)
    manager = window.job_manager
    manager.refresh()
    index = next(i for i, row in enumerate(manager.rows) if row["id"] == ids["roblox"])
    progress = manager.table.cellWidget(index, 3)
    assert progress is not None
    assert progress.value() == 0 and progress.format() == "0 / 2"
    events["roblox"].set()
    qtbot.waitUntil(lambda: not window.roblox.busy)
    manager.refresh()
    assert manager.table.cellWidget(index, 3) is None


def test_long_table_content_scrolls_instead_of_being_capped(concurrent_window, qtbot):
    window, _, _, _, _ = concurrent_window
    table = window.make_table(["Platform", "Stored operation status"])
    qtbot.addWidget(table)
    columns = ReadableColumns(table, window.fill_table)
    text = "Paused - recoverable operation with a saved provider restriction"
    columns.fill([["X", text]])
    table.resize(350, 150)
    table.show()
    qtbot.wait(20)
    assert table.columnWidth(1) > 240
    assert table.columnWidth(1) >= table.sizeHintForColumn(1)
    assert table.horizontalScrollBar().maximum() > 0
    assert table.item(0, 1).toolTip() == text
