import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox

from username_finder.gui import MainWindow
from username_finder.instagram_ui import local_preview, normalize_username, syntax_error


@pytest.fixture
def ui(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path / "instagram-ui.db")
    qtbot.addWidget(window)
    window.timer.stop()
    monkeypatch.setattr(window, "unsaved_choice", lambda: QMessageBox.StandardButton.Discard)
    window.show()
    yield window
    window.close()


@pytest.mark.parametrize("name", ["a", "123", "_example_", "a_b_c", "a.b.c", "a" * 30])
def test_instagram_basic_format_is_independent_of_snapchat_rules(name):
    assert syntax_error(name) is None
    assert normalize_username(" @ExAmple_1 ") == "example_1"


@pytest.mark.parametrize("name", ["", "a" * 31, "a-b", "a b", "أحمد", ".name", "name.", "a..b"])
def test_instagram_invalid_format_has_local_explanation(name):
    assert syntax_error(name)


def test_independent_instagram_tab_rtl_manual_preview_and_disabled_verification(ui, qtbot):
    assert [ui.tabs.tabText(i) for i in range(ui.tabs.count())] == [
        "سناب",
        "Instagram",
        "البحث داخل الملفات",
        "Roblox",
        "صانع ملفات اليوزرات",
        "X — تجريبي",
        "إدارة العمليات",
        "لوحة البداية",
    ]
    ui.tabs.setCurrentIndex(1)
    panel = ui.instagram
    assert ui.tabs.currentWidget() is panel
    assert panel.layoutDirection() == Qt.LayoutDirection.RightToLeft
    assert panel.manual.layoutDirection() == Qt.LayoutDirection.LeftToRight
    assert panel.result_table.layoutDirection() == Qt.LayoutDirection.RightToLeft
    assert panel.indicator.text() == "التحقق الحقيقي غير مفعّل"
    assert not panel.start_button.isEnabled() and panel.start_button.toolTip()
    qtbot.keyClicks(panel.manual, "@1.Example_user")
    assert "مقبولة محليًا" in panel.feedback.text()
    qtbot.keyClick(panel.manual, Qt.Key.Key_Return)
    assert panel.last_preview["counts"]["valid"] == 1
    assert "1.example_user" in panel.preview_text.toPlainText()
    assert "لم تُفحص الإتاحة" in panel.preview_summary.text()
    assert panel.result_table.rowCount() == 0
    assert all(card.text().endswith("\n0") for card in panel.counters.values())
    assert not ui.store.jobs() and not ui.current_job and not ui.is_running()
    panel.manual.setText("bad-name")
    assert "فقط" in panel.feedback.text() and panel.last_preview is None
    assert not panel.preview_text.toPlainText()


def test_file_picker_preview_and_removal_do_not_touch_snapchat(ui, qtbot, tmp_path, monkeypatch):
    panel = ui.instagram
    ui.prefix.setText("snap")
    ui.limit.setText("25")
    baseline = ui.editor_signature()
    dirty = ui.preset_dirty
    path = tmp_path / "أسماء.txt"
    path.write_bytes(b"\xef\xbb\xbf123\na_b_c\n123\nbad-name\n\n")
    calls = []

    def choose(*args):
        calls.append(args)
        return [str(path), str(path)], "Text files (*.txt)"

    def forbidden(*args, **kwargs):
        raise AssertionError("Instagram local preparation must not create jobs or start an engine")

    monkeypatch.setattr(QFileDialog, "getOpenFileNames", choose)
    monkeypatch.setattr(ui.store, "create_job", forbidden)
    monkeypatch.setattr(ui, "start_engine", forbidden)
    ui.tabs.setCurrentWidget(ui.instagram)
    qtbot.mouseClick(panel.add_button, Qt.MouseButton.LeftButton)
    assert calls[0][0] is panel and calls[0][-1] == "Text files (*.txt)"
    assert panel.files.count() == 1 and ui.files.count() == 0
    qtbot.mouseClick(panel.preview_button, Qt.MouseButton.LeftButton)
    counts = panel.last_preview["counts"]
    assert counts["valid"] == 2 and counts["invalid"] == 1
    assert counts["duplicate"] == 1 and counts["blank"] == 1
    preview = panel.preview_text.toPlainText()
    for index in range(ui.tabs.count()):
        ui.tabs.setCurrentIndex(index)
    assert panel.preview_text.toPlainText() == preview and panel.files.count() == 1
    assert ui.editor_signature() == baseline and ui.preset_dirty == dirty
    assert not ui.store.jobs() and ui.result_table.rowCount() == 0
    assert panel.result_table.rowCount() == 0
    assert all(card.text().endswith("\n0") for card in panel.counters.values())
    panel.files.setCurrentRow(0)
    qtbot.mouseClick(panel.remove_button, Qt.MouseButton.LeftButton)
    assert panel.files.count() == 0 and panel.last_preview is None
    panel.preview()
    assert "أولًا" in panel.preview_summary.text()


def test_txt_preview_errors_empty_input_and_bounded_read(tmp_path):
    path = tmp_path / "names.txt"
    assert local_preview("", (str(path),))["warnings"]
    path.write_bytes(b"")
    assert local_preview("", (str(path),))["counts"]["valid"] == 0
    path.write_bytes(b"\xff\xfe\n")
    result = local_preview("", (str(path),))
    assert result["counts"]["malformed"] == 1 and "UTF-8" in result["warnings"][0]
    path.write_bytes(b"abc\n" * 10000)
    result = local_preview("", (str(path),))
    assert result["limited"] and result["counts"]["lines"] == 200
    path.write_bytes(b"a" * 1000000)
    result = local_preview("", (str(path),))
    assert result["limited"] and result["counts"]["lines"] == 0


@pytest.mark.parametrize("size", [(900, 650), (1180, 850)])
def test_instagram_layout_scrolls_to_all_sections_at_supported_sizes(ui, qtbot, size):
    ui.resize(*size)
    ui.tabs.setCurrentWidget(ui.instagram)
    qtbot.wait(20)
    panel = ui.instagram
    assert panel.horizontalScrollBar().maximum() == 0
    assert not ui.tabs.tabBar().usesScrollButtons() or ui.tabs.tabBar().tabRect(1).width() > 0
    for control in (panel.manual, panel.preview_button, panel.start_button, panel.result_table):
        panel.ensureWidgetVisible(control)
        qtbot.wait(5)
        position = control.mapTo(panel.viewport(), control.rect().center())
        assert panel.viewport().rect().contains(position)
    assert not panel.start_button.isEnabled()


def test_responsive_groups_and_compact_empty_states_preserve_input(ui, qtbot):
    panel = ui.instagram
    ui.tabs.setCurrentWidget(ui.instagram)
    assert panel.preview_text.isHidden() and panel.files.isHidden()
    assert not panel.remove_button.isEnabled()
    for size, columns in [((1180, 850), 2), ((900, 650), 1), ((1180, 850), 2)]:
        ui.resize(*size)
        qtbot.wait(25)
        assert panel._input_columns == columns
        manual, files = panel.manual_group.geometry(), panel.txt_group.geometry()
        assert not manual.intersects(files)
        if columns == 2:
            assert manual.top() == files.top()
        else:
            assert files.top() > manual.bottom()
        assert panel.start_button.width() <= 180
        assert panel.body.width() <= 1240
        assert panel.horizontalScrollBar().maximum() == 0
        assert all(card.height() <= 60 for card in panel.counters.values())
    panel.manual.setText("123_example")
    panel.preview()
    assert panel.preview_text.isVisible() and panel.preview_text.height() <= 90
    original = panel.preview_text.toPlainText()
    ui.resize(900, 650)
    qtbot.wait(25)
    assert panel.preview_text.toPlainText() == original
    assert panel.manual.text() == "123_example" and panel.result_table.rowCount() == 0


def test_file_display_stays_bounded_and_returns_to_empty_state(ui, tmp_path, monkeypatch):
    panel = ui.instagram
    paths = []
    for index in range(4):
        path = tmp_path / f"names-{index}.txt"
        path.write_text("example_1\n", encoding="utf-8")
        paths.append(str(path))
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *args: (paths, "Text files (*.txt)"))
    panel.add_files()
    assert panel.files.count() == 4 and panel.files.height() <= 110
    assert panel.files.viewport().height() >= panel.files.sizeHintForRow(0)
    assert panel.files_empty.isHidden() and "4" in panel.file_count.text()
    while panel.files.count():
        panel.files.setCurrentRow(0)
        panel.remove_file()
    assert panel.files.isHidden() and not panel.files_empty.isHidden()
    assert not panel.remove_button.isEnabled()


def test_local_actions_have_no_remote_or_job_side_effects(ui, qtbot, monkeypatch):
    import socket
    import urllib.request

    def forbidden(*args, **kwargs):
        raise AssertionError("Local Instagram actions must not invoke a remote service or job")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(ui.store, "create_job", forbidden)
    monkeypatch.setattr(ui, "submit", forbidden)
    panel = ui.instagram
    ui.tabs.setCurrentWidget(ui.instagram)
    baseline = ui.editor_signature()
    panel.manual.setText("example_1")
    qtbot.keyClick(panel.manual, Qt.Key.Key_Return)
    qtbot.mouseClick(panel.start_button, Qt.MouseButton.LeftButton)
    assert ui.editor_signature() == baseline
    assert not ui.store.jobs() and not ui.is_running()
    assert panel.result_table.rowCount() == 0 and not panel.start_button.isEnabled()
    assert list(panel.counters) == ["متاح", "مستخدم", "محجوز", "غير صالح", "غير محسوم"]
    assert all(card.text().endswith("\n0") for card in panel.counters.values())
