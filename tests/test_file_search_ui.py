import time
from threading import Event

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication

from username_finder.file_search import WHOLE_VIEW_BYTES
from username_finder.file_search_ui import FileSearchPanel
from username_finder.gui import MainWindow


@pytest.fixture
def panel(qtbot):
    widget = FileSearchPanel()
    widget.resize(900, 650)
    qtbot.addWidget(widget)
    widget.show()
    yield widget
    widget.shutdown()
    qtbot.waitUntil(lambda: not widget.busy, timeout=5000)
    widget.close()


def search(panel, qtbot, path, query, limit="100"):
    panel.set_files([path])
    panel.query.setText(query)
    panel.limit.setCurrentText(limit)
    panel.start.click()
    qtbot.waitUntil(lambda: panel.task is None, timeout=5000)


def view(panel, qtbot, row=0):
    panel.ensureWidgetVisible(panel.table)
    index = panel.model.index(row, 0)
    qtbot.mouseClick(
        panel.table.viewport(), Qt.MouseButton.LeftButton, pos=panel.table.visualRect(index).center()
    )
    qtbot.waitUntil(lambda: panel.page is not None, timeout=5000)


def test_literal_search_limit_order_and_full_line_copy(panel, qtbot, tmp_path):
    path = tmp_path / "records.jsonl"
    line = '{"u":"example_user","id":"123","e":"example@example.com","n":"عربي🙂"}'
    path.write_bytes((line + "\r\n" + "EXAMPLE\n").encode())
    search(panel, qtbot, path, "example", "2")
    assert panel.model.rowCount() == 2 and "اكتمل البحث حسب الحد المحدد — 2 نتيجة" in panel.status.text()
    assert panel.last_summary.status == "limit" and panel.model.rows[0].line == 1
    assert panel.model.rows[0].byte_column < panel.model.rows[1].byte_column
    assert panel.layoutDirection() == Qt.LayoutDirection.RightToLeft
    assert panel.query.layoutDirection() == panel.viewer.layoutDirection() == Qt.LayoutDirection.LeftToRight
    view(panel, qtbot)
    assert panel.viewer.isReadOnly() and panel.viewer.toPlainText() == line
    cursor = panel.viewer.textCursor()
    cursor.setPosition(line.index("example_user"))
    cursor.setPosition(line.index("example_user") + 7, QTextCursor.MoveMode.KeepAnchor)
    panel.viewer.setTextCursor(cursor)
    qtbot.keyClick(panel.viewer, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "example"
    QApplication.clipboard().clear()
    panel.copy_line.click()
    qtbot.waitUntil(lambda: QApplication.clipboard().text() == line)
    panel.copy_location.click()
    assert QApplication.clipboard().text() == f"{path.resolve()}:1"


def test_mouse_selection_and_literal_regex_symbols(panel, qtbot, tmp_path):
    path = tmp_path / "literal.txt"
    path.write_bytes(b"mouse selectable [a.*] line\n")
    search(panel, qtbot, path, "[a.*]")
    view(panel, qtbot)
    panel.ensureWidgetVisible(panel.viewer)
    panel.viewer.horizontalScrollBar().setValue(0)
    start = QTextCursor(panel.viewer.document())
    start.setPosition(0)
    end = QTextCursor(panel.viewer.document())
    end.setPosition(5)
    a, b = panel.viewer.cursorRect(start), panel.viewer.cursorRect(end)
    qtbot.mousePress(panel.viewer.viewport(), Qt.MouseButton.LeftButton, pos=a.center())
    qtbot.mouseMove(panel.viewer.viewport(), pos=b.center(), delay=20)
    qtbot.mouseRelease(panel.viewer.viewport(), Qt.MouseButton.LeftButton, pos=b.center())
    assert panel.viewer.textCursor().hasSelection()
    qtbot.keyClick(panel.viewer, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "mouse"


def test_original_control_separators_are_copied_without_qt_normalization(panel, qtbot, tmp_path):
    path = tmp_path / "separators.txt"
    text = "first\rsecond\u2028third\u2029fourth🙂"
    path.write_bytes(text.encode())
    search(panel, qtbot, path, "second")
    view(panel, qtbot)
    panel.viewer.selectAll()
    qtbot.keyClick(panel.viewer, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == text


@pytest.mark.parametrize("limit", ["0", "-1", "1.5", "text", "100001", "1,,000"])
def test_bad_limits_never_start_a_worker(panel, tmp_path, limit):
    path = tmp_path / "local.txt"
    path.write_bytes(b"content")
    panel.set_files([path])
    panel.query.setText("content")
    panel.limit.setCurrentText(limit)
    panel.start_search()
    assert panel.task is None and "صحيحًا موجبًا" in panel.status.text()


def test_file_controls_empty_query_stale_view_and_errors(panel, qtbot, tmp_path):
    first, second = tmp_path / "one.txt", tmp_path / "two.txt"
    first.write_bytes(b"found old")
    second.write_bytes(b"other")
    panel.set_files([first, second, first])
    assert len(panel.files) == 2 and "مكررة" in panel.status.text()
    panel.file_list.setCurrentRow(1)
    panel.remove_file()
    assert len(panel.files) == 1
    panel.start_search()
    assert panel.task is None and "غير فارغ" in panel.status.text()
    search(panel, qtbot, first, "found")
    first.write_bytes(b"found new and longer")
    panel.table.setCurrentIndex(panel.model.index(0, 0))
    qtbot.waitUntil(lambda: "تغيّر" in panel.line_status.text())
    assert panel.viewer.toPlainText() == "" and not panel.copy_line.isEnabled()
    panel.clear_files()
    assert not panel.files
    first.write_bytes(b"\xff")
    search(panel, qtbot, first, "absent")
    assert panel.last_summary.error_code == "encoding" and "UTF-8" in panel.status.text()


def test_large_line_paging_and_complete_copy(panel, qtbot, tmp_path):
    text = "🙂ع" * (WHOLE_VIEW_BYTES // 3) + "END"
    path = tmp_path / "long.jsonl"
    path.write_text(text + "\n", encoding="utf-8")
    search(panel, qtbot, path, "END", "10")
    view(panel, qtbot)
    assert panel.page_controls.isVisible() and "سطر كبير" in panel.line_status.text()
    assert len(panel.viewer.toPlainText()) < len(text) and "END" in panel.viewer.toPlainText()
    old = panel.page
    panel.previous.click()
    qtbot.waitUntil(lambda: panel.page is not old)
    assert panel.page.requested < old.requested
    QApplication.clipboard().clear()
    panel.copy_line.click()
    qtbot.waitUntil(lambda: QApplication.clipboard().text() == text)


def slow_search(monkeypatch):
    import username_finder.file_search_ui as module

    entered = Event()
    original = module.search_files

    def controlled(*args, **kwargs):
        deliver = kwargs["on_batch"]

        def batch(rows):
            accepted = deliver(rows)
            entered.set()
            kwargs["cancel"].wait(3)
            return accepted

        kwargs["on_batch"] = batch
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "search_files", controlled)
    return entered


def test_responsive_cancel_and_result_model_capacity(panel, qtbot, tmp_path, monkeypatch):
    path = tmp_path / "many.txt"
    path.write_bytes(b"a\n" * 200000)
    entered = slow_search(monkeypatch)
    panel.set_files([path])
    panel.query.setText("a")
    panel.limit.setCurrentText("100000")
    ticks = []
    timer = QTimer(panel)
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    panel.start_search()
    qtbot.waitUntil(entered.is_set)
    qtbot.wait(40)
    assert len(ticks) >= 3 and not panel.start.isEnabled()
    started = time.perf_counter()
    panel.cancel.click()
    qtbot.waitUntil(lambda: panel.task is None, timeout=1500)
    assert panel.last_summary.status == "cancelled" and time.perf_counter() - started < 1.5
    assert panel.model.rowCount() <= 100000 and "جزئية" in panel.status.text()
    timer.stop()
    monkeypatch.undo()
    search(panel, qtbot, path, "a", "1,000")
    assert panel.model.rowCount() == 1000 and panel.last_summary.status == "limit"


def test_independent_file_tab_state_and_shutdown(qtbot, tmp_path, monkeypatch):
    path = tmp_path / "many.txt"
    path.write_bytes(b"a\n" * 200000)
    entered = slow_search(monkeypatch)
    window = MainWindow(tmp_path / "isolated.db")
    qtbot.addWidget(window)
    window.timer.stop()
    window.show()
    panel = window.file_search
    assert window.tabs.count() == 8 and window.tabs.tabText(2) == "البحث داخل الملفات"
    assert window.tabs.tabText(1) == "Instagram"
    original = window.editor_signature()
    window.tabs.setCurrentWidget(panel)
    panel.set_files([path])
    panel.query.setText("a")
    panel.limit.setCurrentText("100000")
    panel.start_search()
    qtbot.waitUntil(entered.is_set)
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
    assert window.editor_signature() == original and not window.store.jobs()
    assert not window.instagram.start_button.isEnabled()
    window.close()
    qtbot.waitUntil(lambda: window.closed, timeout=3000)
    assert not panel.busy and panel.executor_closed


@pytest.mark.parametrize("size", [(900, 650), (1180, 850)])
def test_early_limit_progress_user_column_width_and_draggable_splitter(panel, qtbot, tmp_path, size):
    panel.resize(*size)
    path = tmp_path / "long-but-accessible-filename-records.txt"
    path.write_bytes(b"match\n" + b"x" * 2_000_000)
    assert panel.table.columnWidth(0) > panel.table.columnWidth(4)
    panel.table.setColumnWidth(0, 340)
    search(panel, qtbot, path, "match", "1")
    assert panel.table.columnWidth(0) == 340
    assert "اكتمل البحث حسب الحد المحدد — 1 نتيجة" in panel.status.text()
    assert "قد توجد نتائج إضافية لم تُفحص" in panel.status.text()
    assert "اكتمل حسب الحد" in panel.progress_bar.format()
    assert panel.progress_bar.value() == 0
    assert panel.last_summary.progress.processed == 5
    handle = panel.splitter.handle(1)
    panel.ensureWidgetVisible(handle)
    qtbot.wait(20)
    before = panel.splitter.sizes()
    pos = handle.rect().center()
    qtbot.mousePress(handle, Qt.MouseButton.LeftButton, pos=pos)
    qtbot.mouseMove(handle, pos=pos + QPoint(0, 50), delay=20)
    qtbot.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=pos + QPoint(0, 50))
    assert panel.splitter.sizes() != before
    assert panel.splitter.sizes()[0] >= 120 and panel.viewer.height() >= 90
    assert panel.horizontalScrollBar().maximum() == 0
