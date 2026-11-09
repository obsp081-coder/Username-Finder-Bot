import time
from urllib.parse import parse_qs, urlsplit

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox
from test_roblox import csv_file, response

from username_finder import roblox_protocol, roblox_ui
from username_finder.gui import MainWindow
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store


def test_restriction_status_and_cancelled_confirmation_never_dispatch(window, monkeypatch):
    panel = window.roblox
    before = panel.repo.store.db.total_changes
    panel.restriction_button.click()
    assert panel.restriction_button.text() == "تحقق من القيد"
    assert panel.diagnostic_button.isEnabled() and "غير معروفة" in panel.restriction_label.text()
    assert panel.repo.store.db.total_changes == before
    monkeypatch.setattr(roblox_ui.QInputDialog, "getText", lambda *args: ("DiagnosticName", True))
    monkeypatch.setattr(panel, "_confirm", lambda _: False)
    panel.start_diagnostic()
    assert not panel.test_calls and not panel.repo.runs()


def test_historical_restriction_is_read_only_and_cannot_diagnose(window, monkeypatch):
    from username_finder.roblox_activity import RobloxActivity

    panel = window.roblox
    marker = RobloxActivity(panel.repo.store.path).block
    marker.write_bytes(b"http_429")
    monkeypatch.setattr(roblox_ui.QInputDialog, "getText", lambda *args: pytest.fail("Blocked"))
    panel.check_restriction()
    assert not panel.diagnostic_button.isEnabled()
    assert "تاريخي" in panel.restriction_label.text() and "HTTP 429" in panel.restriction_label.text()
    panel.start_diagnostic()
    assert marker.read_bytes() == b"http_429" and not panel.test_calls and not panel.repo.runs()


def test_restriction_controls_remain_accessible_in_all_roblox_modes(window):
    panel = window.roblox
    for mode in range(3):
        panel.checking_mode.setCurrentIndex(mode)
        assert panel.restriction_button.isVisibleTo(panel)
        panel.check_restriction()
        assert panel.restriction_label.isVisibleTo(panel)
    assert not panel.test_calls


def test_confirmed_single_diagnostic_preserves_inputs_and_selected_operation(window, qtbot, monkeypatch):
    panel = window.roblox
    panel.manual.setPlainText("PreservedInput")
    selected = panel.current_run
    speed = panel.speed.currentData()
    monkeypatch.setattr(roblox_ui.QInputDialog, "getText", lambda *args: ("DiagnosticName", True))
    confirmations = []
    monkeypatch.setattr(panel, "_confirm", lambda text: (confirmations.append(text), True)[1])
    panel.start_diagnostic()
    wait_done(qtbot, panel)
    assert panel.test_calls == ["DiagnosticName"] and len(confirmations) == 1
    assert "HTTP 200" in panel.restriction_label.text() and "لا يثبت" in panel.restriction_label.text()
    assert panel.manual.toPlainText() == "PreservedInput" and panel.current_run == selected
    assert panel.speed.currentData() == speed
    assert not panel.diagnostic_button.isEnabled()
    assert len(panel.repo.history("DiagnosticName")) == 1


def test_diagnostic_overlap_and_stop_do_not_touch_another_operation(window, qtbot, monkeypatch):
    import threading

    panel = window.roblox
    entered, release = threading.Event(), threading.Event()
    previous = panel.repo.create_run(["PreviousName"])
    panel.current_run = previous
    panel.checking_mode.setCurrentIndex(2)

    def fetch(url):
        panel.test_calls.append(url)
        entered.set()
        assert release.wait(5)
        return response()

    panel.fetcher = fetch
    monkeypatch.setattr(roblox_ui.QInputDialog, "getText", lambda *args: ("DiagnosticName", True))
    monkeypatch.setattr(panel, "_confirm", lambda _: True)
    panel.start_diagnostic()
    try:
        qtbot.waitUntil(entered.is_set, timeout=3000)
        assert panel.busy and panel.stop_button.isEnabled()
        assert panel.diagnostic_cancel_button.isVisibleTo(panel)
        panel.start_diagnostic()
        panel.check_restriction()
        assert not panel.diagnostic_button.isEnabled()
        panel.diagnostic_cancel_button.click()
    finally:
        release.set()
    wait_done(qtbot, panel)
    assert len(panel.test_calls) == 1 and panel.current_run == previous
    assert panel.repo.progress(previous)["state"] == "Ready"


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(parse_qs(urlsplit(url).query)["Username"][0])
        return response()

    actual_runner = roblox_ui.RobloxRunner
    monkeypatch.setattr(roblox_protocol, "fetch", fetch)
    monkeypatch.setattr(
        roblox_ui, "RobloxRunner", lambda *a, **kw: actual_runner(*a, wait=lambda _: False, **kw)
    )
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    ui = MainWindow(tmp_path / "gui.db")
    ui.roblox.fetcher = fetch
    ui.roblox.test_calls = calls
    qtbot.addWidget(ui)
    ui.show()
    yield ui
    ui.close()
    qtbot.waitUntil(lambda: ui.closed, timeout=3000)


def wait_done(qtbot, panel):
    qtbot.waitUntil(lambda: not panel.busy, timeout=5000)


def test_navigation_rtl_and_supported_sizes_without_automatic_requests(window, qtbot):
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        "سناب",
        "Instagram",
        "البحث داخل الملفات",
        "Roblox",
        "صانع ملفات اليوزرات",
        "X — تجريبي",
        "إدارة العمليات",
        "لوحة التحكم",
    ]
    original = [window.search_tabs.widget(i) for i in range(4)]
    window.tabs.setCurrentIndex(3)
    panel = window.roblox
    assert window.tabs.currentWidget() is panel
    assert panel.layoutDirection() == Qt.LayoutDirection.RightToLeft
    assert panel.manual.layoutDirection() == Qt.LayoutDirection.LeftToRight
    assert panel.skip_previous.isChecked() and not panel.test_calls
    assert not panel.stop_button.isEnabled()
    for width, height in [(900, 650), (1180, 850)]:
        window.resize(width, height)
        qtbot.wait(20)
        assert panel.horizontalScrollBar().maximum() == 0
    for i in range(4):
        window.tabs.setCurrentIndex(i)
    assert original == [window.search_tabs.widget(i) for i in range(4)]
    assert not panel.test_calls
    assert window.store.jobs() == []


def test_manual_preview_local_validation_start_skip_and_dedup(window, qtbot, tmp_path):
    panel = window.roblox
    path = csv_file(tmp_path / "previous.csv", [("UsedName", 1, "2026-11-07T05:00:00+00:00")])
    panel.repo.import_csv(path)
    panel.modes.setCurrentIndex(1)
    panel.manual.setPlainText("UsedName\nFreshName\nfreshname\ninvalid-name")
    panel.preview()
    wait_done(qtbot, panel)
    assert "مكرر: 1" in panel.preview_label.text()
    assert "غير صالح: 1" in panel.preview_label.text()
    assert not panel.test_calls and not panel.repo.runs()
    panel.start()
    wait_done(qtbot, panel)
    assert panel.test_calls == ["FreshName"]
    assert panel.repo.progress(panel.current_run)["skipped"] == 1
    assert window.store.jobs() == []


def test_generation_works_and_results_filter(window, qtbot):
    panel = window.roblox
    panel.minimum_length.setValue(5)
    panel.maximum_length.setValue(5)
    panel.quantity.setValue(4)
    panel.start()
    wait_done(qtbot, panel)
    assert len(panel.test_calls) == len(set(panel.test_calls)) == 4
    assert all(len(n) == 5 for n in panel.test_calls)
    assert panel.result_table.rowCount() == 4
    panel.class_filter.setCurrentIndex(panel.class_filter.findData("Taken"))
    assert panel.result_table.rowCount() == 0
    panel.class_filter.setCurrentIndex(panel.class_filter.findData("Available"))
    panel.search.setText(panel.test_calls[0])
    qtbot.waitUntil(lambda: panel.result_table.rowCount() == 1)


def test_running_hidden_results_are_deferred_and_refreshed_when_shown(window, qtbot, monkeypatch):
    panel = window.roblox
    window.tabs.setCurrentWidget(panel)
    calls = []
    original = panel.repo.results

    def results(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(panel.repo, "results", results)
    panel.busy = True
    try:
        panel.workspace.setCurrentIndex(0)
        panel.refresh()
        assert not calls
        panel.workspace.setCurrentIndex(1)
        qtbot.waitUntil(lambda: bool(calls))
        calls.clear()
        window.tabs.setCurrentWidget(window.x_panel)
        panel.refresh()
        assert not calls
        window.tabs.setCurrentWidget(panel)
        qtbot.waitUntil(lambda: bool(calls))
    finally:
        panel.busy = False


def test_range_preview_shows_lengths_and_checks_total(window, qtbot):
    panel = window.roblox
    panel.minimum_length.setValue(3)
    panel.maximum_length.setValue(20)
    panel.quantity.setValue(18)
    panel.preview()
    wait_done(qtbot, panel)
    assert not panel.test_calls
    for length in range(3, 21):
        assert f"(الطول {length})" in panel.preview_label.text()
    window.tabs.setCurrentWidget(panel)
    for size in ((900, 650), (1180, 850)):
        window.resize(*size)
        qtbot.wait(20)
        assert panel.horizontalScrollBar().maximum() == 0
    panel.start()
    wait_done(qtbot, panel)
    assert sorted(map(len, panel.test_calls)) == list(range(3, 21))
    assert panel.repo.results()[1] == 18


def test_invalid_length_range_does_not_create_job(window, qtbot):
    panel = window.roblox
    panel.minimum_length.setValue(10)
    panel.maximum_length.setValue(3)
    panel.start()
    wait_done(qtbot, panel)
    assert not panel.repo.runs() and not panel.test_calls
    assert "نطاق" in panel.status.text()


def test_recheck_all_requires_confirmation_and_preserves_history(window, qtbot, tmp_path, monkeypatch):
    panel = window.roblox
    panel.repo.import_csv(csv_file(tmp_path / "previous.csv", [("UsedName", 1, "2026-11-07T05:00:00+00:00")]))
    defaults = []

    def decline(*args):
        defaults.append(args[-1])
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", decline)
    panel.recheck_all()
    assert defaults == [QMessageBox.StandardButton.No]
    assert not panel.test_calls and not panel.repo.runs()
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    panel.recheck_all()
    wait_done(qtbot, panel)
    assert panel.test_calls == ["UsedName"]
    assert panel.skip_previous.isChecked()
    assert panel.rows[0]["completed_checks"] == 2
    assert panel.rows[0]["changed"]
    assert "Taken" in panel.result_table.item(0, 5).text()
    assert len(panel.repo.history("UsedName")) == 2


def test_txt_csv_selection_and_one_time_history_import_no_requests(window, qtbot, tmp_path, monkeypatch):
    panel = window.roblox
    txt = tmp_path / "names.txt"
    txt.write_text("FreshName\nOtherName", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a: ([str(txt)], ""))
    panel.add_files()
    panel.modes.setCurrentIndex(1)
    panel.preview()
    wait_done(qtbot, panel)
    assert "صالح نحويًا: 2" in panel.preview_label.text() and not panel.test_calls
    source = csv_file(tmp_path / "results.csv", [("StoredName", 1, "2026-11-07T05:00:00+00:00")])
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(source), ""))
    panel.import_history()
    wait_done(qtbot, panel)
    panel.import_history()
    wait_done(qtbot, panel)
    assert not panel.test_calls and panel.repo.results()[1] == 1
    assert len(panel.repo.history("StoredName")) == 1
    assert "لم تُرسل" in panel.status.text()


def test_paused_operation_survives_window_restart(window, qtbot):
    panel = window.roblox
    panel.speed.setCurrentIndex(panel.speed.findData(0.5))
    calls = []

    def fetch(url):
        calls.append(url)
        panel.runner.cancel.set()
        return response()

    panel.fetcher = fetch
    panel.modes.setCurrentIndex(1)
    panel.manual.setPlainText("FirstName\nNextName")
    panel.start()
    wait_done(qtbot, panel)
    identity = panel.current_run
    assert panel.repo.progress(identity)["state"] == "Paused" and len(calls) == 1
    panel.speed.setCurrentIndex(panel.speed.findData(0))
    path = window.store.path
    window.close()
    qtbot.waitUntil(lambda: window.closed)
    reopened = MainWindow(path)
    qtbot.addWidget(reopened)
    other = reopened.roblox
    other.fetcher = lambda url: (calls.append(url), response())[1]
    other.operations.setCurrentIndex(other.operations.findData(identity))
    assert other.speed.currentData() == 0
    assert "0.5 ثانية" in other.effective_speed.text()
    other.resume()
    wait_done(qtbot, other)
    assert len(calls) == len(set(calls)) == 2
    assert other.repo.progress(identity)["state"] == "Completed"
    assert other.repo.progress(identity)["interval_seconds"] == 0.5
    assert other.repo.results()[1] == 2
    reopened.close()
    qtbot.waitUntil(lambda: reopened.closed)


def test_close_during_request_waits_for_durable_cleanup(window, qtbot):
    panel = window.roblox
    entered = []

    def fetch(_):
        entered.append(True)
        time.sleep(0.1)
        return response()

    panel.fetcher = fetch
    panel.quantity.setValue(2)
    panel.start()
    qtbot.waitUntil(lambda: bool(entered))
    path = window.store.path
    window.close()
    qtbot.waitUntil(lambda: window.closed, timeout=3000)
    store = Store(path)
    try:
        assert RobloxStore(store).results()[1] == 1
    finally:
        store.close()


def test_speed_options_default_warning_and_saved_preference(window, qtbot):
    panel = window.roblox
    assert [panel.speed.itemData(i) for i in range(panel.speed.count())] == [5, 2, 1, 0.5, 0]
    assert panel.speed.currentData() == 5
    assert panel.speed_warning.isHidden()
    panel.speed.setCurrentIndex(panel.speed.findData(0))
    assert not panel.speed_warning.isHidden()
    assert "Experimental speed. Short tests succeeded" in panel.speed_warning.text()
    assert not panel.test_calls
    path = window.store.path
    window.close()
    qtbot.waitUntil(lambda: window.closed)
    reopened = MainWindow(path)
    qtbot.addWidget(reopened)
    try:
        assert reopened.roblox.speed.currentData() == 0
        assert not reopened.roblox.speed_warning.isHidden()
        assert not reopened.roblox.repo.runs()
    finally:
        reopened.close()
        qtbot.waitUntil(lambda: reopened.closed)


@pytest.mark.parametrize("mode", ["generate", "manual", "txt", "csv", "recheck_all", "recheck_selected"])
def test_all_input_paths_snapshot_selected_speed(window, qtbot, tmp_path, monkeypatch, mode):
    panel = window.roblox
    panel.speed.setCurrentIndex(panel.speed.findData(0.5))
    if mode.startswith("recheck"):
        panel.repo.import_csv(
            csv_file(tmp_path / "prior.csv", [("StoredName", 1, "2026-11-07T05:00:00+00:00")])
        )
        panel.refresh()
        panel.result_table.setCurrentCell(0, 0)
        monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
        getattr(panel, mode)()
    else:
        panel.quantity.setValue(2)
        if mode != "generate":
            panel.modes.setCurrentIndex(1)
            if mode == "manual":
                panel.manual.setPlainText("FirstName\nNextName")
            else:
                path = tmp_path / f"names.{mode}"
                path.write_text(
                    "FirstName\nNextName" if mode == "txt" else "username\nFirstName\nNextName",
                    encoding="utf-8",
                )
                panel.files.addItem(str(path))
        panel.start()
    wait_done(qtbot, panel)
    saved = panel.repo.progress(panel.current_run)
    assert saved["interval_seconds"] == 0.5
    assert saved["state"] == "Completed"
    assert len(panel.test_calls) == (1 if mode.startswith("recheck") else 2)
    assert window.store.jobs() == []


def test_resume_displays_original_speed_and_disables_changes_while_active(window, qtbot):
    panel = window.roblox
    panel.speed.setCurrentIndex(panel.speed.findData(2))
    identity = panel.repo.create_run(["FirstName", "NextName"])
    panel.speed.setCurrentIndex(panel.speed.findData(0))
    panel.refresh()
    panel.operations.setCurrentIndex(panel.operations.findData(identity))
    assert "2 ثانية" in panel.effective_speed.text()
    observed = []

    def fetch(_):
        observed.append(panel.repo.progress(identity)["interval_seconds"])
        assert not panel.speed.isEnabled()
        panel.runner.cancel.set()
        return response()

    panel.fetcher = fetch
    panel.resume()
    wait_done(qtbot, panel)
    assert observed == [2]
    assert panel.speed.isEnabled()
    assert panel.speed.currentData() == 0
    assert "2 ثانية" in panel.effective_speed.text()
    panel.fetcher = lambda _: response()
    panel.resume()
    wait_done(qtbot, panel)
    assert panel.repo.progress(identity)["interval_seconds"] == 2
    assert panel.repo.progress(identity)["state"] == "Completed"
