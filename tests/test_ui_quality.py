import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QPalette, QWheelEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from username_finder.domain import Filters, SearchConfig
from username_finder.gui import MainWindow
from username_finder.sources import preview_txt


@pytest.fixture
def ui(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path / "quality.db")
    qtbot.addWidget(window)
    window.timer.stop()
    window.tabs.setCurrentWidget(window.snapchat_tabs)
    window.resize(1180, 850)
    monkeypatch.setattr(window, "unsaved_choice", lambda: QMessageBox.StandardButton.Discard)
    window.show()
    yield window
    window.close()


def wheel(control, delta=-120, pixel=None):
    position = control.rect().center()
    event = QWheelEvent(
        QPointF(position),
        QPointF(control.mapToGlobal(position)),
        pixel or QPoint(),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(control, event)


@pytest.mark.parametrize("name", ["tabs", "search_tabs", "advanced_tabs"])
def test_tab_bar_wheel_preserves_selection_but_click_and_keyboard_work(ui, qtbot, name):
    ui.advanced_toggle.setChecked(True)
    tabs = getattr(ui, name)
    bar = tabs.tabBar()
    qtbot.wait(20)
    for index in range(tabs.count()):
        tabs.setCurrentIndex(index)
        for delta in (-120, 120):
            wheel(bar, delta)
            assert tabs.currentIndex() == index
        wheel(bar, 0, QPoint(0, -12))
        assert tabs.currentIndex() == index
    tabs.setCurrentIndex(0)
    qtbot.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(2).center())
    assert tabs.currentIndex() == 2
    bar.setFocus()
    qtbot.keyClick(bar, Qt.Key.Key_Right)
    assert tabs.currentIndex() == 1  # Native RTL keyboard direction remains unchanged.


def test_snapchat_child_tab_wheel_and_click_keyboard(ui, qtbot):
    tabs = ui.snapchat_tabs
    bar = tabs.tabBar()
    wheel(bar)
    assert tabs.currentIndex() == 0
    qtbot.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(1).center())
    wheel(bar, 120)
    assert tabs.currentIndex() == 1
    bar.setFocus()
    qtbot.keyClick(bar, Qt.Key.Key_Right)
    assert tabs.currentIndex() == 0


def test_advanced_tab_bar_wheel_scrolls_outer_page_once_without_switching(ui, qtbot):
    ui.advanced_toggle.setChecked(True)
    ui.advanced_tabs.setCurrentIndex(2)
    qtbot.wait(20)
    tabs = ui.advanced_tabs
    bar = tabs.tabBar()
    outer = ui.search_tabs.widget(0).verticalScrollBar()
    inner = tabs.currentWidget().verticalScrollBar()
    assert outer.maximum() > 24
    outer.setValue(0)
    inner_before = inner.value()
    wheel(bar, 0, QPoint(0, -12))
    assert outer.value() == 12
    wheel(bar, 0, QPoint(0, 12))
    assert outer.value() == 0
    outer.setValue(outer.maximum())
    wheel(bar)
    assert outer.value() == outer.maximum()
    assert tabs.currentIndex() == 2 and ui.tabs.currentIndex() == 0
    assert inner.value() == inner_before


@pytest.mark.parametrize("name", ["workers", "ttl", "retries"])
def test_wheel_skips_non_scrollable_inner_page_without_changing_values(ui, qtbot, name):
    ui.advanced_toggle.setChecked(True)
    ui.advanced_tabs.setCurrentIndex(2)
    ui.advanced_tabs.setMinimumHeight(600)
    qtbot.wait(20)
    outer = ui.search_tabs.widget(0)
    inner = ui.advanced_tabs.currentWidget()
    # Use real layout ranges, not the artificial ranges that hid the original regression.
    assert inner.verticalScrollBar().maximum() == 0
    assert outer.verticalScrollBar().maximum() > 0
    outer.verticalScrollBar().setValue(0)
    before = ui.editor_signature()
    wheel(getattr(ui, name))
    assert outer.verticalScrollBar().value() > 0
    assert ui.editor_signature() == before


def test_inner_boundary_chains_outward_and_reverse_scrolling_works(ui, qtbot):
    ui.advanced_toggle.setChecked(True)
    ui.advanced_tabs.setCurrentIndex(2)
    inner = ui.advanced_tabs.currentWidget()
    content = inner.widget()
    content.setMinimumHeight(900)
    qtbot.wait(20)
    outer = ui.search_tabs.widget(0)
    inner_bar, outer_bar = inner.verticalScrollBar(), outer.verticalScrollBar()
    assert inner_bar.maximum() > 0 and outer_bar.maximum() > 0
    inner_bar.setValue(0)
    outer_bar.setValue(0)
    wheel(ui.workers)
    assert inner_bar.value() > 0 and outer_bar.value() == 0
    inner_bar.setValue(inner_bar.maximum())
    wheel(ui.workers)
    assert outer_bar.value() > 0
    inner_bar.setValue(0)
    previous = outer_bar.value()
    wheel(ui.workers, 120)
    assert outer_bar.value() < previous


def test_pixel_scroll_over_spin_child_and_page_viewport_has_no_recursion(ui, qtbot):
    ui.advanced_toggle.setChecked(True)
    ui.advanced_tabs.setCurrentIndex(2)
    ui.advanced_tabs.setMinimumHeight(600)
    qtbot.wait(20)
    outer = ui.search_tabs.widget(0)
    assert ui.advanced_tabs.currentWidget().verticalScrollBar().maximum() == 0
    outer.verticalScrollBar().setValue(0)
    before = ui.workers.value()
    wheel(ui.workers.lineEdit(), 0, QPoint(0, -12))
    assert ui.workers.value() == before and outer.verticalScrollBar().value() == 12
    previous = outer.verticalScrollBar().value()
    wheel(ui.advanced_tabs.currentWidget().viewport())
    assert outer.verticalScrollBar().value() > previous


def luminance(color):
    channels = [color.redF(), color.greenF(), color.blueF()]
    linear = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in channels]
    return sum(x * weight for x, weight in zip(linear, [0.2126, 0.7152, 0.0722], strict=True))


def contrast(a, b):
    bright, dark = sorted([luminance(a), luminance(b)], reverse=True)
    return (bright + 0.05) / (dark + 0.05)


@pytest.mark.parametrize("dark_system", [False, True])
def test_shared_combo_popups_keep_readable_state_colors(qtbot, tmp_path, monkeypatch, dark_system):
    app = QApplication.instance()
    original = app.palette()
    palette = QPalette(original)
    palette.setColor(QPalette.ColorRole.Base, QColor("#000000" if dark_system else "#ffffff"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#ffffff" if dark_system else "#000000"))
    app.setPalette(palette)
    window = MainWindow(tmp_path / "palette.db")
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "unsaved_choice", lambda: QMessageBox.StandardButton.Discard)
    try:
        window.show()
        for control in (
            window.source,
            window.mode,
            window.preset_selector,
            window.repeat_kind,
            window.result_sort,
        ):
            control.ensurePolished()
            control.showPopup()
            qtbot.wait(2)
            view = control.view()
            assert view.layoutDirection() == Qt.LayoutDirection.RightToLeft
            for group in (
                QPalette.ColorGroup.Active,
                QPalette.ColorGroup.Inactive,
                QPalette.ColorGroup.Disabled,
            ):
                p = view.palette()
                assert (
                    contrast(p.color(group, QPalette.ColorRole.Base), p.color(group, QPalette.ColorRole.Text))
                    >= 3
                )
                assert (
                    contrast(
                        p.color(group, QPalette.ColorRole.Highlight),
                        p.color(group, QPalette.ColorRole.HighlightedText),
                    )
                    >= 3
                )
            control.hidePopup()
        css = window.styleSheet()
        for state in (":hover", ":selected", ":focus", ":disabled"):
            assert "QAbstractItemView::item" + state in css
    finally:
        window.close()
        app.setPalette(original)


def txt_config(path, **kwargs):
    return SearchConfig(
        source="txt", files=(str(path),), filters=kwargs.pop("filters", Filters(3, 3)), **kwargs
    )


def test_three_name_txt_preview_counts_and_dedup_are_local_only(tmp_path):
    path = tmp_path / "names.txt"
    path.write_bytes(b"\xef\xbb\xbfaaa\r\naab\r\naac\r\nAAA\r\n")
    result = preview_txt(txt_config(path))
    assert result["diagnostics"]["counts"]["eligible"] == 3
    assert result["diagnostics"]["counts"]["duplicate"] == 1
    assert result["diagnostics"]["complete"]
    assert result["examples"] == ["aaa", "aab", "aac"]
    assert result["level"] == "success"


@pytest.mark.parametrize(
    "contents,expected",
    [
        (b"", "فارغة"),
        (b"\r\n \r\n", "فارغة"),
        (b"abc\nab1\n", "لا توجد أسماء مطابقة"),
        (b"\xff\xfea\x00\n", "UTF-8"),
    ],
)
def test_txt_preview_edge_cases_have_specific_messages(tmp_path, contents, expected):
    path = tmp_path / "input.txt"
    path.write_bytes(contents)
    result = preview_txt(txt_config(path, filters=Filters(3, 3, prefix="z")))
    assert expected in result["message"]
    assert "تعذر إتمام العملية" not in result["message"]


def test_missing_changed_and_large_txt_never_claim_unscanned_zero(tmp_path):
    import hashlib

    path = tmp_path / "missing.txt"
    result = preview_txt(txt_config(path))
    assert "غير موجود" in result["message"] and str(tmp_path) not in result["message"]
    path.write_bytes(b"aaa\naab\n")
    expected = {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()}
    path.write_bytes(b"aaa\naac\n")
    assert "تغير محتوى" in preview_txt(txt_config(path), expected)["message"]
    path.write_bytes(b"abc\n" * 10000 + b"zzz\n")
    result = preview_txt(txt_config(path, filters=Filters(3, 3, prefix="z")))
    assert not result["diagnostics"]["complete"]
    assert "غير معلوم" in result["message"] and "لا يمكن الجزم" in result["message"]
    assert result["diagnostics"]["counts"]["lines"] <= 200
    assert result["diagnostics"]["bytes_read"] <= 65537


def test_preview_byte_limit_does_not_decode_partial_utf8_or_report_false_change(tmp_path):
    path = tmp_path / "long.txt"
    path.write_bytes(("أ" * 40000).encode("utf-8"))
    result = preview_txt(txt_config(path), {str(path.resolve()): "0" * 64})
    assert result["diagnostics"]["bytes_read"] <= 65537
    assert result["diagnostics"]["codes"] == []
    assert not result["diagnostics"]["complete"]


def test_exact_txt_gui_preview_clears_stale_error_and_handles_conflicts(ui, qtbot, tmp_path, monkeypatch):
    path = tmp_path / "known.txt"
    path.write_text("aaa\naab\naac\n", encoding="utf-8")
    ui.source.setCurrentIndex(1)
    ui.files.addItem(str(path))
    ui.min_length.setValue(3)
    ui.max_length.setValue(3)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: None)
    ui.show_error("ValueError")
    ui.preview()
    qtbot.waitUntil(lambda: ui.last_preview_diagnostics.get("counts", {}).get("eligible") == 3)
    assert "اكتملت المعاينة" in ui.statusBar().currentMessage()
    assert "3" in ui.preview_label.text() and "غير معلوم" not in ui.preview_label.text()
    assert not ui.store.jobs() and not ui.is_running()
    ui.min_length.setValue(5)
    ui.preview()
    assert "الطول" in ui.preview_label.text()
    assert "تعذر إتمام العملية" not in ui.statusBar().currentMessage()
    ui.min_length.setValue(3)
    ui.preview()
    qtbot.waitUntil(lambda: "اكتملت المعاينة" in ui.statusBar().currentMessage())
    ui.limit.setText("invalid")
    ui.preview()
    assert "عددًا صحيحًا" in ui.preview_label.text()
    assert "invalid literal" not in ui.preview_label.text()


def test_stale_preview_does_not_replace_current_settings(ui, qtbot, tmp_path, monkeypatch):
    import threading

    import username_finder.gui as gui

    path = tmp_path / "names.txt"
    path.write_text("aaa\naab\naac\n", encoding="utf-8")
    ui.source.setCurrentIndex(1)
    ui.files.addItem(str(path))
    gate = threading.Event()
    original = gui.preview_txt

    def delayed(*args):
        assert gate.wait(2)
        return original(*args)

    monkeypatch.setattr(gui, "preview_txt", delayed)
    ui.preview()
    ui.prefix.setText("z")
    gate.set()
    qtbot.waitUntil(lambda: "تغيرت الإعدادات" in ui.preview_label.text())


def test_close_after_unsaved_discard_finishes_but_cancel_keeps_editor(ui, qtbot, monkeypatch):
    ui.prefix.setText("a")
    monkeypatch.setattr(ui, "unsaved_choice", lambda: QMessageBox.StandardButton.Cancel)
    ui.close()
    assert ui.isVisible() and not ui.closed and ui.prefix.text() == "a"
    monkeypatch.setattr(ui, "unsaved_choice", lambda: QMessageBox.StandardButton.Discard)
    ui.close()
    qtbot.waitUntil(lambda: ui.closed, timeout=500)
    assert not ui.isVisible()
