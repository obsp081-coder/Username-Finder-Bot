import csv
import os
import threading
import time

import pytest
from PySide6.QtCore import QPoint, QRect, QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox
from test_result_search import record

from username_finder.gui import MainWindow
from username_finder.result_filters_ui import ResultQueryController
from username_finder.result_search import ResultFilter, ResultSearch
from username_finder.saved_results_ui import SavedResultsDialog


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path / "filter-ui.db")
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "unsaved_choice", lambda: QMessageBox.StandardButton.Discard)
    with window.store.transaction() as db:
        for platform in ("Roblox", "X"):
            for i in range(240):
                record(db, platform, f"ab{i:04}", "Taken" if i % 2 else "Available")
            record(db, platform, "ab.ca", "Invalid")
            record(db, platform, "ab11", "Available")
    window.resize(900, 650)
    window.show()
    yield window
    window.close()
    qtbot.waitUntil(lambda: window.closed)


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_platform_filters_full_dataset_reset_export_and_history(
    window, platform, qtbot, tmp_path, monkeypatch
):
    panel = window.roblox if platform == "Roblox" else window.x_panel
    window.tabs.setCurrentWidget(panel)
    panel.workspace.setCurrentIndex(1)
    panel.refresh()
    assert len(panel.rows) == 100
    advanced = panel.advanced_filters
    advanced.toggle.click()
    assert advanced.advanced_scroll.isVisible()
    advanced.length_mode.setCurrentIndex(advanced.length_mode.findData("exact"))
    advanced.minimum.setValue(6)
    advanced.digit_mode.setCurrentIndex(advanced.digit_mode.findData("exact"))
    advanced.digits.setValue(4)
    panel.search.setText("ab")
    qtbot.waitUntil(lambda: len(panel.rows) == 100)
    assert "240" in panel.page_label.text()
    assert all(len(row["username"]) == 6 for row in panel.rows)
    panel.page(1)
    qtbot.waitUntil(lambda: len(panel.rows) == 100 and panel.offset == 100)
    path = tmp_path / f"{platform}.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **kw: (str(path), ""))
    panel.export()
    qtbot.waitUntil(lambda: not panel.busy, timeout=5000)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 240 and all(len(row["username"]) == 6 for row in rows)
    # Filtering controls never mutate stored observations or create any checking jobs.
    assert not panel.repo.runs()
    advanced.reset_button.click()
    assert panel.offset == 0 and len(panel.rows) == 100 and not panel.search.text()
    advanced.symbol_mode.setCurrentIndex(advanced.symbol_mode.findData("contains"))
    advanced.symbol.setCurrentText(".")
    qtbot.waitUntil(lambda: len(panel.rows) == 1)
    assert panel.rows[0]["username"] == "ab.ca"
    assert "Invalid" in panel.counters.text() and "1" in panel.page_label.text()
    advanced.maximum.setValue(2)
    advanced.minimum.setValue(8)
    advanced.length_mode.setCurrentIndex(advanced.length_mode.findData("range"))
    assert panel.rows == [] and "الأدنى" in panel.page_label.text()
    assert panel.repo.results()[1] == 242


def test_unified_scope_page_export_and_early_close(window, qtbot, tmp_path, monkeypatch):
    dialog = SavedResultsDialog(window)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: len(dialog.rows) == 100)
    dialog.results_presentation.mode.setCurrentIndex(1)
    for column in range(dialog.table.columnCount()):
        assert dialog.table.columnWidth(column) >= dialog.table.sizeHintForColumn(column)
    assert "484" in dialog.page_label.text()
    assert {row["platform"] for row in dialog.rows} <= {"Roblox", "X"}
    dialog.scope.setCurrentIndex(dialog.scope.findData("X"))
    qtbot.waitUntil(lambda: len(dialog.rows) == 100)
    assert {row["platform"] for row in dialog.rows} == {"X"}
    dialog.scope.setCurrentIndex(0)
    dialog.search.setText("ab11")
    dialog.filters.text_mode.setCurrentIndex(dialog.filters.text_mode.findData("exact"))
    qtbot.waitUntil(lambda: len(dialog.rows) == 2)
    path = tmp_path / "unified.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **kw: (str(path), ""))
    dialog.export()
    qtbot.waitUntil(lambda: not dialog.exporting)
    with path.open(encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2 and {row["platform"] for row in rows} == {"Roblox", "X"}
    dialog.search.setText("zero-matches")
    qtbot.waitUntil(lambda: "لا نتائج" in dialog.page_label.text())
    assert not dialog.next_button.isEnabled()
    dialog.reject()
    assert dialog.query.closed and dialog.export_cancel.is_set()
    assert window.tabs.currentWidget() is window.dashboard


def test_real_large_query_keeps_event_loop_responsive(window, qtbot, monkeypatch):
    with window.store.transaction() as db:
        for i in range(49516):
            record(db, "Roblox" if i % 2 else "X", f"bulk{i:06}")
    real_page = ResultSearch.page
    ticks, during = [], []
    timer = QTimer(window)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)

    def observed(self, *args, **kwargs):
        before = len(ticks)
        started = time.perf_counter()
        result = real_page(self, *args, **kwargs)
        during.append((len(ticks) - before, time.perf_counter() - started))
        return result

    monkeypatch.setattr(ResultSearch, "page", observed)
    controller = ResultQueryController(window)
    results = []
    controller.ready.connect(lambda rows, *_: results.append(rows))
    controller.request(
        window.store.path, "All", ResultFilter(composition="mixed_plain", repetition="digits"), 0
    )
    qtbot.waitUntil(lambda: bool(results), timeout=10000)
    controller.shutdown()
    timer.stop()
    assert len(results[0]) == 100 and during[0][0] > 0
    print(
        f"FILTER_UI_RESPONSIVENESS records=50000 ticks_during_query={during[0][0]} "
        f"query_seconds={during[0][1]:.4f}"
    )


def test_query_cancellation_stale_delivery_and_event_loop_responsiveness(window, qtbot, monkeypatch):
    entered, release, interrupted = threading.Event(), threading.Event(), threading.Event()
    real_page = ResultSearch.page

    def controlled(self, scope, filters, **kwargs):
        if filters.text == "old":
            entered.set()
            while not release.wait(0.005):
                if kwargs["cancel"].is_set():
                    interrupted.set()
                    break
            # Deliberately return stale rows despite cancellation to test generation guarding.
            self.last_counts = {"Taken": 1}
            return [{"username": "stale"}], 1, 0
        return real_page(self, scope, filters, **kwargs)

    monkeypatch.setattr(ResultSearch, "page", controlled)
    controller = ResultQueryController(window)
    results = []
    controller.ready.connect(lambda rows, total, offset, counts: results.append(rows))
    timer = QTimer(window)
    ticks = []
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start(5)
    controller.request(window.store.path, "All", ResultFilter(text="old"), 0)
    qtbot.waitUntil(entered.is_set)
    qtbot.waitUntil(lambda: len(ticks) > 5)
    controller.request(window.store.path, "All", ResultFilter(text="ab11", text_mode="exact"), 0)
    qtbot.waitUntil(lambda: bool(results))
    assert interrupted.is_set()
    assert len(results) == 1 and {row["username"] for row in results[0]} == {"ab11"}
    controller.request(window.store.path, "All", ResultFilter(text="old"), 0)
    controller.cancel()
    qtbot.wait(180)
    assert len(results) == 1
    controller.shutdown()
    timer.stop()
    release.set()


def test_filter_layout_windows_sizes_and_symbols(window, qtbot, tmp_path):
    if os.environ.get("UFB_SCREENSHOTS"):
        print("FILTER_GUI_DPR=" + str(window.devicePixelRatioF()))
    for width, height in ((900, 650), (1180, 850), (1500, 1000)):
        window.resize(width, height)
        for name, panel in (("roblox", window.roblox), ("x", window.x_panel)):
            window.tabs.setCurrentWidget(panel)
            panel.workspace.setCurrentIndex(1)
            panel.advanced_filters.toggle.setChecked(True)
            panel.refresh()
            qtbot.wait(30)
            area = panel.workspace.currentWidget()
            assert panel.horizontalScrollBar().maximum() == 0
            assert area.horizontalScrollBar().maximum() == 0
            assert panel.start_button.isVisible() and panel.stop_button.isVisible()
            assert panel.advanced_filters.symbol.itemText(0) == "_"
            if os.environ.get("UFB_SCREENSHOTS"):
                path = tmp_path / f"filters-{name}-{width}.png"
                window.grab().save(str(path))
                print("FILTER_GUI_SCREENSHOT=" + str(path))
        dialog = SavedResultsDialog(window)
        qtbot.addWidget(dialog)
        dialog.resize(width, height)
        dialog.filters.toggle.setChecked(True)
        dialog.show()
        qtbot.waitUntil(lambda current=dialog: bool(current.rows))
        assert dialog.width() <= width and dialog.height() <= height
        assert dialog.rect().contains(
            QRect(dialog.export_button.mapTo(dialog, QPoint()), dialog.export_button.size())
        )
        assert dialog.filters.advanced_scroll.horizontalScrollBar().maximum() == 0
        if os.environ.get("UFB_SCREENSHOTS"):
            path = tmp_path / f"filters-unified-{width}.png"
            dialog.grab().save(str(path))
            print("FILTER_GUI_SCREENSHOT=" + str(path))
        dialog.reject()
