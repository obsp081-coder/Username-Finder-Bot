"""Cohesive page layouts using temporary stores and exclusively offline evidence."""

import os

import pytest
from osintgram_fixture import PROFILE, TOOLS, MemoryCredentials, MemorySettings
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QMessageBox, QStyle, QStyleOptionSpinBox
from test_osintgram_ui import choose
from test_x import response

from username_finder import x
from username_finder.gui import MainWindow
from username_finder.osintgram_backend import BackendManager
from username_finder.ui_cards import Card, PageHeading
from username_finder.x_runner import XRunner


@pytest.fixture
def desktop(qtbot, tmp_path, monkeypatch):
    settings = MemorySettings(directory=str(tmp_path / "offline-analysis"))
    manager = BackendManager(settings=settings, credentials=MemoryCredentials())
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    window = MainWindow(tmp_path / "milestone-two.db", osintgram_options={"manager": manager})
    monkeypatch.setattr(window.instagram.analysis, "activate", lambda: None)
    window.timer.stop()
    qtbot.addWidget(window)
    window.show()
    yield window
    window.close()
    qtbot.waitUntil(lambda: window.closed, timeout=5000)


def test_remaining_pages_reuse_cards_and_preserve_drafts(desktop, qtbot, monkeypatch):
    window = desktop
    snapshot = [window.search_tabs.widget(i) for i in range(4)]
    window.instagram.manual.setText("@local_preview")
    window.x_panel.manual.setPlainText("LocalDraft")
    window.file_search.query.setText("literal.*")
    config = window.file_generator.config()
    before = window.store.db.total_changes

    def forbidden(*args, **kwargs):
        pytest.fail("Presentation must not start network or checking work")

    window.roblox.fetcher = window.x_panel.fetcher = forbidden
    monkeypatch.setattr(window, "start_engine", forbidden)
    for panel in (window.instagram, window.x_panel, window.file_generator, window.file_search):
        window.tabs.setCurrentWidget(panel)
        assert panel.findChildren(PageHeading)
        assert panel.findChildren(Card)
    window.instagram.preview_button.click()
    assert window.instagram.last_preview["counts"]["valid"] == 1
    assert not window.instagram.start_button.isEnabled()
    assert all(card.text().endswith("\n0") for card in window.instagram.counters.values())
    assert window.instagram.result_table.rowCount() == 0
    window.x_panel.results_options.toggle.click()
    assert not window.x_panel.results_options.content.isHidden()
    window.file_generator.advanced_toggle.click()
    assert not window.file_generator.advanced.isHidden()
    assert window.file_generator.config() == config
    assert window.x_panel.manual.toPlainText() == "LocalDraft"
    assert window.file_search.query.text() == "literal.*"
    assert snapshot == [window.search_tabs.widget(i) for i in range(4)]
    assert window.store.db.total_changes == before


def test_numeric_controls_keep_keyboard_click_and_wheel_behavior(desktop, qtbot):
    from test_ui_quality import wheel

    panel = desktop.x_panel
    desktop.tabs.setCurrentWidget(panel)
    spin = panel.minimum_length
    qtbot.wait(20)
    spin.setFocus()
    old = spin.value()
    qtbot.keyClick(spin, Qt.Key.Key_Up)
    assert spin.value() == old + 1
    option = QStyleOptionSpinBox()
    spin.initStyleOption(option)
    point = (
        spin.style()
        .subControlRect(QStyle.ComplexControl.CC_SpinBox, option, QStyle.SubControl.SC_SpinBoxDown, spin)
        .center()
    )
    qtbot.mouseClick(spin, Qt.MouseButton.LeftButton, pos=point)
    assert spin.value() == old
    wheel(spin, -120)
    assert spin.value() == old
    assert spin.height() >= 30


@pytest.mark.parametrize("width,height", [(900, 650), (1180, 850), (1500, 1000)])
def test_native_remaining_page_states(desktop, qtbot, tmp_path, width, height):
    window = desktop
    window.resize(width, height)
    window.statusBar().showMessage("معاينة محلية ببيانات اختبار فقط — لا طلبات حية أو إجراءات مكتبية")

    def capture(name, page, area=None, focus=None):
        window.tabs.setCurrentWidget(page)
        qtbot.wait(30)
        if area:
            area.verticalScrollBar().setValue(0)
            if focus:
                area.ensureWidgetVisible(focus)
        qtbot.wait(45)
        assert window.width() == width
        assert window.sidebar.mapTo(window, QPoint()).x() > window.tabs.mapTo(window, QPoint()).x()
        if hasattr(page, "horizontalScrollBar"):
            assert page.horizontalScrollBar().maximum() == 0
        if os.environ.get("UFB_SCREENSHOTS"):
            path = tmp_path / f"{name}-{width}.png"
            assert window.grab().save(str(path))
            print("MILESTONE_TWO_SCREENSHOT=" + str(path))

    instagram = window.instagram
    capture("instagram-empty", instagram, instagram)
    instagram.manual.setText("@local_preview")
    instagram.preview()
    assert instagram.last_preview["counts"]["valid"] == 1
    capture("instagram-preview", instagram, instagram, instagram.preview_button)
    assert not instagram.start_button.isEnabled()

    analysis = instagram.analysis
    instagram.modes.setCurrentIndex(1)
    analysis._discovered(TOOLS)
    analysis.target.setText("fixture_user")
    choose(analysis, "get_user_info")
    capture("osintgram-commands", instagram, instagram)
    capture("osintgram-command-options", instagram, instagram, analysis.commands)
    analysis.result_items = [{"name": "get_user_info", "is_error": False, "result": PROFILE}]
    analysis.result_picker.addItem("بيانات اختبار محلية")
    analysis.show_result(0)
    analysis.pages.setCurrentIndex(2)
    capture("osintgram-results", instagram, instagram)
    assert analysis.table.rowCount() > 0
    analysis.pages.setCurrentIndex(0)
    capture("osintgram-setup", instagram, instagram)
    assert not analysis.running and not analysis.manager.busy

    panel = window.x_panel
    assert panel.speed.currentData() == 5
    panel.modes.setCurrentIndex(1)
    panel.manual.setPlainText("LocalExampleOne\nLocalExampleTwo")
    capture("x-input", panel)
    panel.repo.set_interval(0)
    identity = panel.repo.prepare(x.input_spec("LocalExampleOne\nLocalExampleTwo"))
    XRunner(panel.repo, fetcher=lambda *a: response(), wait=lambda _: False).run(identity)
    panel.current_run = identity
    panel.workspace.setCurrentIndex(1)
    panel.refresh()
    qtbot.waitUntil(lambda: panel.table.rowCount() == 2)
    capture("x-results-simple", panel)
    panel.results_presentation.mode.setCurrentIndex(1)
    capture("x-results-advanced", panel)
    restricted = panel.repo.prepare(x.input_spec("RestrictedOne"))
    XRunner(panel.repo, fetcher=lambda *a: x.protocol.Response(429), wait=lambda _: False).run(restricted)
    panel.current_run = restricted
    panel.workspace.setCurrentIndex(2)
    panel.refresh()
    assert panel.repo.control()["blocked"]
    assert not panel.start_button.isEnabled()
    capture("x-restricted", panel, panel.workspace.currentWidget(), panel.restriction)
    capture("x-recovery-actions", panel, panel.workspace.currentWidget(), panel.recovery_check_button)
    panel.workspace.setCurrentIndex(3)
    capture("x-connection", panel)

    window.snapchat_tabs.setCurrentIndex(0)
    window.search_tabs.setCurrentIndex(0)
    capture("snapchat-settings", window.snapchat_tabs)
    window.advanced_toggle.setChecked(True)
    area = window.search_tabs.currentWidget()
    capture("snapchat-advanced", window.snapchat_tabs, area, window.advanced_tabs)
    window.search_tabs.setCurrentIndex(1)
    capture("snapchat-run", window.snapchat_tabs)
    window.search_tabs.setCurrentIndex(2)
    window.fill_table(
        window.result_table,
        [["mock_name", "Taken · محاكاة", "محاكاة", "اختبار محلي", "mock", "دليل اختبار", "محاكاة"]],
    )
    window.result_empty.hide()
    capture("snapchat-results", window.snapchat_tabs)
    window.search_tabs.setCurrentIndex(3)
    capture("snapchat-history", window.snapchat_tabs)
    capture(
        "snapchat-history-actions",
        window.snapchat_tabs,
        window.search_tabs.currentWidget(),
        window.history_table,
    )
    window.snapchat_tabs.setCurrentIndex(1)
    snapscore = window.snapscore
    capture("snapscore-calibration", window.snapchat_tabs, snapscore.form_area)
    for control in (snapscore.preview, snapscore.stop):
        assert (
            snapscore.viewport().rect().contains(control.mapTo(snapscore.viewport(), control.rect().center()))
        )
    assert not snapscore.session.active and not snapscore.calibrated

    generator = window.file_generator
    capture("generator-platform", generator, generator.form_area)
    generator.advanced_toggle.setChecked(True)
    capture("generator-advanced", generator, generator.form_area, generator.primary.advanced)
    for control in (generator.start, generator.stop):
        assert (
            generator.viewport().rect().contains(control.mapTo(generator.viewport(), control.rect().center()))
        )
    assert not generator.busy

    search = window.file_search
    capture("file-search-empty", search, search)
    search.workspace.setCurrentIndex(1)
    capture("file-search-empty-results", search)
    path = tmp_path / "local-records.txt"
    path.write_text("local_example first\nlocal_example second\nother", encoding="utf8")
    search.set_files([path])
    search.query.setText("local_example")
    search.start.click()
    qtbot.waitUntil(lambda: search.task is None)
    assert search.model.rowCount() == 2
    search.table.setCurrentIndex(search.model.index(0, 0))
    qtbot.waitUntil(lambda: search.page is not None)
    capture("file-search-results", search, search, search.splitter)
    assert "local_example" in search.viewer.toPlainText()
    search.mode.setCurrentIndex(1)
    search.workspace.setCurrentIndex(0)
    capture("file-search-instagram-id", search, search)
    assert search.username.layoutDirection() == Qt.LayoutDirection.LeftToRight
    assert not search.busy
