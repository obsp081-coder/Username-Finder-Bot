import time

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QScrollArea, QSpinBox, QVBoxLayout

from tools.pagination_profile import seed
from username_finder.gui import MainWindow
from username_finder.ui_helpers import status_text


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    ui = MainWindow(tmp_path / "ui.db")
    qtbot.addWidget(ui)
    monkeypatch.setattr(ui, "unsaved_choice", lambda: QMessageBox.StandardButton.Discard)
    ui.resize(900, 650)
    ui.tabs.setCurrentWidget(ui.snapchat_tabs)
    ui.show()
    yield ui
    ui.close()


@pytest.mark.parametrize(
    "name,advanced",
    [
        ("min_length", None),
        ("source", None),
        ("ttl", 2),
        ("start_time", 3),
        ("repeat_count", 1),
        ("workers", 2),
    ],
)
def test_wheel_scrolls_page_not_values_even_when_focused(window, qtbot, name, advanced):
    if advanced is not None:
        window.advanced_toggle.setChecked(True)
        window.advanced_tabs.setCurrentIndex(advanced)
    window.repeat.setChecked(True)
    window.daily_start.setChecked(True)
    control = getattr(window, name)
    control.setFocus()
    qtbot.wait(15)
    parent = control.parentWidget()
    while parent and not isinstance(parent, QScrollArea):
        parent = parent.parentWidget()
    # Make actual scroll motion observable even for a short advanced subpage.
    parent.verticalScrollBar().setRange(0, 500)
    parent.verticalScrollBar().setValue(50)
    before = window.editor_signature()
    position = control.rect().center()
    wheel = QWheelEvent(
        QPointF(position),
        QPointF(control.mapToGlobal(position)),
        QPoint(),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(control, wheel)
    assert window.editor_signature() == before
    assert parent.verticalScrollBar().value() > 50


def test_wheel_over_spin_line_edit_and_owned_dialog_and_keyboard_edit(window, qtbot):
    control = window.min_length
    line = control.lineEdit()
    before = control.value()
    pos = line.rect().center()
    event = QWheelEvent(
        QPointF(pos),
        QPointF(line.mapToGlobal(pos)),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(line, event)
    assert control.value() == before
    control.setFocus()
    qtbot.keyClick(control, Qt.Key.Key_Up)
    assert control.value() == before + 1
    window.source.setFocus()
    qtbot.keyClick(window.source, Qt.Key.Key_Down)
    assert window.source.currentData() == "txt"
    dialog = QDialog(window)
    layout = QVBoxLayout(dialog)
    spin = QSpinBox()
    spin.setValue(10)
    layout.addWidget(spin)
    dialog.show()
    pos = spin.rect().center()
    event = QWheelEvent(
        QPointF(pos),
        QPointF(spin.mapToGlobal(pos)),
        QPoint(),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(spin, event)
    assert spin.value() == 10
    dialog.close()


def test_conditional_controls_preserve_hidden_values(window):
    assert not window.advanced_tabs.isVisible()
    window.repeat.setChecked(True)
    window.repeat_target.setText("a")
    window.repeat_count.setValue(4)
    window.repeat.setChecked(False)
    assert not window.repeat_count.isEnabled()
    window.repeat.setChecked(True)
    assert window.repeat_count.value() == 4 and window.repeat_target.text() == "a"
    assert not window.meaning_group.isEnabled()
    window.mode.setCurrentIndex(1)
    assert window.meaning_group.isEnabled()
    window.source.setCurrentIndex(1)
    assert not window.mode.isVisible() and window.txt_group.isVisible()
    assert not window.meaning_group.isEnabled()
    window.source.setCurrentIndex(0)
    assert window.mode.currentData() == "meaningful"
    window.daily_stop.setChecked(True)
    original = window.stop_time.time()
    window.daily_stop.setChecked(False)
    assert not window.stop_time.isEnabled() and window.stop_time.time() == original


def test_large_pagination_correct_filters_sort_rapid_navigation_and_provenance(qtbot, tmp_path):
    path = tmp_path / "results.db"
    job = seed(path, 10000)
    ui = MainWindow(path)
    qtbot.addWidget(ui)
    ui.timer.stop()
    ui.load_job(job)
    ui.search_tabs.setCurrentIndex(2)
    ui.show()
    QApplication.processEvents()
    assert ui.results_total == 10000
    assert ui.result_table.item(0, 0).text() == "name000000"
    started = time.perf_counter()
    for _ in range(5):
        ui.page_results(1)
        ui.page_results(-1)
    assert time.perf_counter() - started < 3  # Regression guard for the reproduced ~3s EACH refresh.
    for _ in range(3):
        ui.page_results(1)
    assert ui.offset == 300 and ui.result_table.item(0, 0).text() == "name000300"
    ui.status_filter.setCurrentIndex(ui.status_filter.findData("Available"))
    assert ui.offset == 0 and ui.results_total == 2000
    assert all("محاكاة" in ui.result_table.item(i, 1).text() for i in range(ui.result_table.rowCount()))
    assert "simulated_ambiguous" not in " ".join(ui.result_table.item(0, c).text() for c in range(7))
    ui.result_sort.setCurrentIndex(ui.result_sort.findData("name_desc"))
    assert ui.result_table.item(0, 0).text() == "name009995"
    ui.result_search.setText("0000")
    ui.refresh_results(reset=True)
    assert ui.results_total == 20
    ui.results_mode.setCurrentIndex(ui.results_mode.findData("real"))
    assert ui.results_total == 0 and ui.result_table.rowCount() == 0
    assert not ui.results_next.isEnabled() and not ui.results_previous.isEnabled()
    ui.close()


def test_small_page_has_no_empty_second_page_and_job_isolation(qtbot, tmp_path):
    path = tmp_path / "small.db"
    first = seed(path, 20)
    second = seed(path, 200)
    ui = MainWindow(path)
    qtbot.addWidget(ui)
    ui.load_job(first)
    ui.page_results(1)
    assert ui.offset == 0 and ui.result_table.rowCount() == 20 and ui.results_total == 20
    assert not ui.results_next.isEnabled()
    ui.load_job(second)
    ui.page_results(1)
    assert ui.offset == 100 and ui.result_table.rowCount() == 100 and ui.results_total == 200
    ui.load_job(first)
    assert ui.offset == 0 and ui.result_table.rowCount() == 20
    ui.close()


def test_status_labels_mark_every_simulated_classification():
    for value in ("Available", "Taken", "Reserved", "Invalid", "Unknown"):
        assert "محاكاة" in status_text(value, "simulated")
        assert "محاكاة" not in status_text(value, "real")


def test_query_error_clears_stale_rows_and_job_selection_uses_full_id(qtbot, tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "failure.db"
    first = seed(path, 20)
    second = seed(path, 200)
    ui = MainWindow(path)
    qtbot.addWidget(ui)
    ui.refresh_jobs()
    ui.search_tabs.setCurrentIndex(3)
    ui.show()
    QApplication.processEvents()
    for row in range(ui.job_table.rowCount()):
        if ui.job_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == second:
            qtbot.mouseClick(
                ui.job_table.viewport(),
                Qt.MouseButton.LeftButton,
                pos=ui.job_table.visualItemRect(ui.job_table.item(row, 0)).center(),
            )
            break
    ui.load_selected_job()
    assert ui.current_job == second and ui.results_total == 200
    original = ui.store.result_page

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("test failure")

    monkeypatch.setattr(ui.store, "result_page", fail)
    ui.refresh_results(reset=True)
    assert ui.result_table.rowCount() == 0 and ui._result_rows == []
    assert "تعذر" in ui.result_empty.text() and not ui.results_next.isEnabled()
    monkeypatch.setattr(ui.store, "result_page", original)
    ui.load_job(first)
    assert ui.results_total == 20
    ui.close()
