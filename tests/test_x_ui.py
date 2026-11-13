import csv
import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox
from test_x import Clock, response

from username_finder import x, x_ui
from username_finder.gui import MainWindow
from username_finder.x_runner import XRunner
from username_finder.x_storage import XStore


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    ui = MainWindow(tmp_path / "gui.db")
    clock = Clock()
    monkeypatch.setattr(
        x_ui,
        "XRunner",
        lambda repo, **kwargs: XRunner(
            repo, clock=clock.now, monotonic=lambda: clock.seconds, wait=clock.wait, **kwargs
        ),
    )
    ui.x_panel.fetcher = clock.fetch
    ui.x_panel.test_clock = clock
    qtbot.addWidget(ui)
    ui.show()
    yield ui
    ui.close()
    qtbot.waitUntil(lambda: ui.closed, timeout=5000)


def wait_done(qtbot, panel):
    qtbot.waitUntil(lambda: not panel.busy, timeout=5000)


def test_reproduce_saved_manual_empty_job_beside_current_generation_controls(window, qtbot):
    panel = window.x_panel
    identity = panel.repo.prepare(x.input_spec("egge"))
    panel.current_run = None
    panel.minimum_length.setValue(8)
    panel.maximum_length.setValue(8)
    panel.quantity.setText("10")
    panel.refresh()
    run = panel.repo.progress(panel.current_run)
    assert panel.current_run == identity
    assert panel.modes.currentIndex() == 0
    assert run["state"] == "Ready" and run["cursor"] == 1 and run["invalid"] == 1
    assert run["total"] == run["skipped"] == run["duplicates"] == 0
    assert json.loads(run["spec"])["manual"] == "egge"
    assert not panel.start_button.isEnabled()
    assert "إدخال يدوي" in panel.preparation_summary.text()
    assert "عناصر الإدخال المقروءة: 1" in panel.preparation_summary.text()
    assert "المطلوب: 10" not in panel.preparation_summary.text()
    assert "لا أسماء مؤهلة" in panel.status.text()
    panel.preview()
    wait_done(qtbot, panel)
    generated = panel.repo.progress(panel.current_run)
    assert panel.current_run != identity
    assert generated["cursor"] == generated["total"] == 10 and generated["invalid"] == 0
    assert "المطلوب: 10" in panel.preparation_summary.text()
    assert "المُولَّد المقروء: 10" in panel.preparation_summary.text()
    assert "الطول 8–8" in panel.preparation_summary.text()
    assert all(len(name) == 8 for name in panel.repo.preview(panel.current_run))
    assert not panel.test_clock.calls


@pytest.mark.parametrize("headers", [{}, {"retry-after": "120"}])
def test_local_restriction_review_displays_evidence_without_releasing_or_requesting(
    window, monkeypatch, headers
):
    panel = window.x_panel
    identity = panel.repo.prepare(x.input_spec("valid_name\nfreshname"))
    clock = panel.test_clock
    XRunner(
        panel.repo,
        fetcher=lambda *args: x.protocol.Response(429, b"", headers),
        clock=clock.now,
        wait=clock.wait,
    ).run(identity)
    original = panel.repo.progress(identity)
    history = panel.repo.history("valid_name")
    control = panel.repo.control()
    panel.current_run = identity
    panel.refresh()
    assert not hasattr(panel, "restriction") and not hasattr(panel, "review_restriction_button")
    assert "unexpected_http_status:429" in panel.status.text()
    assert panel.repo.progress(identity) == original
    assert panel.repo.history("valid_name") == history
    assert panel.repo.control() == control
    assert not panel.start_button.isEnabled() and not panel.test_clock.calls


def test_all_previously_checked_generation_is_explicitly_empty_without_duplicate_job(window, qtbot):
    panel = window.x_panel
    config = x.generation_range_spec(8, 8, 10)
    identity = panel.repo.prepare({"generation": config})
    clock = panel.test_clock
    XRunner(panel.repo, fetcher=clock.fetch, clock=clock.now, wait=clock.wait).run(identity)
    empty = panel.repo.prepare({"generation": config})
    panel.current_run = empty
    panel.refresh()
    assert "لا أسماء مؤهلة" in panel.status.text() and "لا أسماء مؤهلة" in panel.operations.currentText()
    assert "المطلوب: 10" in panel.preparation_summary.text()
    assert "تجاوز سابق: 10" in panel.preparation_summary.text()
    assert "إجمالي المؤهل: 0" in panel.preparation_summary.text()
    assert not panel.start_button.isEnabled() and not panel.resume_button.isEnabled()
    panel.start()
    assert not panel.busy and len(panel.repo.runs()) == 2 and len(clock.calls) == 10


def test_blocked_preparation_selection_and_restart_keep_distinct_sources(window, qtbot):
    panel = window.x_panel
    with window.store.transaction() as db:
        panel.repo._control(db, blocked="unexpected_http_status:429")
    empty = panel.repo.prepare(x.input_spec("egge"))
    panel.quantity.setText("10")
    panel.minimum_length.setValue(8)
    panel.maximum_length.setValue(8)
    panel.preview()
    wait_done(qtbot, panel)
    generated = panel.current_run
    assert panel.repo.progress(generated)["total"] == 10
    assert "جاهزة — الفحص ممنوع" in panel.status.text()
    assert not panel.start_button.isEnabled()
    panel.operations.setCurrentIndex(panel.operations.findData(empty))
    assert "إدخال يدوي" in panel.preparation_summary.text()
    assert "غير صالح: 1" in panel.preparation_summary.text()
    panel.operations.setCurrentIndex(panel.operations.findData(generated))
    assert "المطلوب: 10" in panel.preparation_summary.text()
    assert len(panel.repo.runs()) == 2 and not panel.test_clock.calls
    recreated = x_ui.XPanel(window.store, window.make_table, window.fill_table, fetcher=panel.fetcher)
    qtbot.addWidget(recreated)
    try:
        assert recreated.current_run == generated
        assert "المطلوب: 10" in recreated.preparation_summary.text()
        assert not recreated.start_button.isEnabled()
        assert recreated.repo.progress(empty)["invalid"] == 1
    finally:
        recreated.shutdown()


def test_reproduce_ready_20_disabled_by_previous_global_429(window, qtbot):
    panel = window.x_panel
    old = panel.repo.prepare({"generation": x.generation_spec(8, 100)})
    count = 0

    def fetch(*args):
        nonlocal count
        count += 1
        return x.protocol.Response(429, b"{}", {"retry-after": "120"}) if count == 81 else response()

    clock = panel.test_clock
    XRunner(panel.repo, fetcher=fetch, clock=clock.now, wait=clock.wait).run(old)
    assert panel.repo.progress(old)["completed"] == 81
    assert panel.repo.progress(old)["state"] == "Restricted"
    blocked = panel.repo.control()["blocked"]
    panel.speed.setCurrentIndex(panel.speed.findData(0))
    panel.quantity.setText("20")
    panel.preview()
    wait_done(qtbot, panel)
    new = panel.current_run
    saved = panel.repo.progress(new)
    assert saved["state"] == "Ready" and saved["total"] == 20 and saved["completed"] == 0
    assert len(panel.repo.preview(new)) == 20
    panel.speed.setCurrentIndex(panel.speed.findData(2))
    assert panel.repo.preferred_interval() == 2
    assert panel.repo.progress(new)["interval_seconds"] == 0
    assert panel.repo.progress(new)["spec"] == saved["spec"]
    assert panel.repo.control()["blocked"] == blocked
    assert not panel.start_button.isEnabled() and not panel.resume_button.isEnabled()
    assert not hasattr(panel, "restriction") and blocked in panel.status.text()
    assert "0 ثانية" in panel.job_settings.text() and "2 ثانية" in panel.job_settings.text()
    panel.start()
    assert len(panel.repo.runs()) == 2
    assert count == 81 and not clock.calls
    assert panel.repo.results(history=True)[1] == 81


def test_navigation_rtl_warnings_and_sizes_without_requests(window, qtbot):
    panel = window.x_panel
    assert window.tabs.tabText(window.tabs.indexOf(panel)) == "X — تجريبي"
    assert window.tabs.count() == 8
    assert panel.layoutDirection() == Qt.LayoutDirection.RightToLeft
    assert panel.manual.layoutDirection() == Qt.LayoutDirection.LeftToRight
    assert panel.skip_previous.isChecked()
    assert panel.speed.currentData() == 5
    assert [panel.speed.itemData(i) for i in range(panel.speed.count())] == [
        30,
        15,
        10,
        5,
        4,
        3,
        2,
        1.5,
        1,
        0.5,
        0,
    ]
    assert "hzwu9" in panel.warning.text() and "غير رسمية" in panel.warning.text()
    original = [window.search_tabs.widget(i) for i in range(4)]
    window.tabs.setCurrentWidget(panel)
    for size in ((900, 650), (1180, 850)):
        window.resize(*size)
        qtbot.wait(20)
        assert panel.horizontalScrollBar().maximum() == 0
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
    assert [window.search_tabs.widget(i) for i in range(4)] == original
    assert not panel.test_clock.calls
    assert not window.roblox.repo.known_count()
    assert window.store.jobs() == []


@pytest.mark.parametrize("proxy", [False, True], ids=["direct-job", "proxy-job"])
def test_start_uses_ready_job_once_with_frozen_speed_and_route(window, qtbot, proxy):
    panel = window.x_panel
    direct = {"mode": "direct", "proxy_url": ""}
    proxied = {"mode": "proxy", "proxy_url": "http://127.0.0.1:8080"}
    if proxy:
        panel.connection_mode.setCurrentIndex(panel.connection_mode.findData("proxy"))
        panel.proxy_url.setText(proxied["proxy_url"])
        panel.save_connection()
    panel.speed.setCurrentIndex(panel.speed.findData(0))
    panel.quantity.setText("20")
    panel.preview()
    wait_done(qtbot, panel)
    identity = panel.current_run
    spec = panel.repo.progress(identity)["spec"]
    names = panel.repo.preview(identity)
    assert panel.start_button.isEnabled() and panel.resume_button.isEnabled()
    panel.speed.setCurrentIndex(panel.speed.findData(2))
    panel.repo.set_connection(direct if proxy else proxied)
    panel.start_button.click()
    wait_done(qtbot, panel)
    assert panel.current_run == identity and len(panel.repo.runs()) == 1
    assert panel.repo.progress(identity)["spec"] == spec
    assert panel.repo.progress(identity)["interval_seconds"] == 0
    assert panel.repo.connection_for_run(identity) == (proxied if proxy else direct)
    assert panel.repo.progress(identity)["state"] == "Completed"
    assert [name for name, _ in panel.test_clock.calls] == names
    assert not panel.test_clock.waits


@pytest.mark.parametrize("state", ["Paused", "PreparationPaused", "PreparationError"])
def test_start_continues_selected_recoverable_job_without_duplicate_preparation(window, qtbot, state):
    panel = window.x_panel
    identity = panel.repo.prepare(x.input_spec("valid_name\nfreshname"))
    panel.repo.end(identity, state)
    panel.current_run = identity
    panel.refresh()
    # The current input controls deliberately differ from the selected job.
    panel.quantity.setText("20")
    panel.start_button.click()
    wait_done(qtbot, panel)
    assert len(panel.repo.runs()) == 1 and panel.current_run == identity
    if state.startswith("Preparation"):
        assert not panel.test_clock.calls
        assert panel.repo.progress(identity)["state"] == "Ready"
        panel.start_button.click()  # Checking requires a separate explicit action.
        wait_done(qtbot, panel)
    assert [name for name, _ in panel.test_clock.calls] == ["valid_name", "freshname"]
    assert panel.repo.progress(identity)["state"] == "Completed"


@pytest.mark.parametrize(
    "state,start,resume",
    [
        (None, True, False),
        ("Ready", True, True),
        ("Running", False, False),
        ("Completed", True, False),
        ("Paused", True, True),
        ("PreparationPaused", True, True),
        ("PreparationError", True, True),
        ("Restricted", False, False),
        ("Error", False, False),
    ],
)
def test_start_lifecycle_states(window, state, start, resume):
    panel = window.x_panel
    if state:
        identity = panel.repo.prepare(x.input_spec("valid_name"))
        panel.repo.end(identity, state)
        panel.current_run = identity
    panel.refresh()
    assert panel.start_button.isEnabled() is start
    assert panel.resume_button.isEnabled() is resume
    assert not panel.test_clock.calls


def test_empty_ready_job_has_no_startable_requests(window):
    panel = window.x_panel
    panel.current_run = panel.repo.prepare(x.input_spec("bad-name"))
    panel.refresh()
    assert not panel.start_button.isEnabled() and not panel.resume_button.isEnabled()
    assert "لا أسماء مؤهلة" in panel.status.text()


def test_length_range_preview_then_start_reuses_mixed_candidates(window, qtbot):
    panel = window.x_panel
    panel.minimum_length.setValue(5)
    panel.maximum_length.setValue(15)
    panel.quantity.setText("11")
    panel.preview()
    wait_done(qtbot, panel)
    names = panel.repo.preview(panel.current_run)
    assert [len(name) for name in names] == list(range(5, 16))
    for name in names:
        assert f"{name} (الطول {len(name)})" in panel.preview_label.text()
    window.tabs.setCurrentWidget(panel)
    for size in ((900, 650), (1180, 850)):
        window.resize(*size)
        qtbot.wait(20)
        assert panel.horizontalScrollBar().maximum() == 0
    panel.start()
    wait_done(qtbot, panel)
    assert [name for name, _ in panel.test_clock.calls] == names
    assert len(panel.repo.runs()) == 1


def test_invalid_length_range_does_not_prepare_job(window):
    panel = window.x_panel
    panel.minimum_length.setValue(10)
    panel.maximum_length.setValue(5)
    panel.preview()
    assert not panel.repo.runs() and not panel.test_clock.calls
    assert "نطاق" in panel.status.text()


def test_all_pacing_choices_labels_warnings_and_settings_without_requests(window):
    panel = window.x_panel
    assert panel.speed.currentData() == 5
    assert panel.speed_warning.isHidden()
    for seconds in (30, 15, 10, 5, 4, 3, 2, 1.5, 1, 0.5, 0):
        panel.speed.setCurrentIndex(panel.speed.findData(seconds))
        assert panel.repo.preferred_interval() == seconds
        assert panel.speed_warning.isHidden() is (seconds >= 5)
        assert f"{seconds:g}" in panel.speed.currentText()
    assert "دون تأخير إضافي" in panel.speed.currentText()
    assert not panel.test_clock.calls


def test_resumable_fast_job_keeps_warning_when_next_job_preference_is_slower(window, qtbot):
    panel = window.x_panel
    panel.speed.setCurrentIndex(panel.speed.findData(0))
    panel.quantity.setText("2")
    panel.preview()
    wait_done(qtbot, panel)
    panel.speed.setCurrentIndex(panel.speed.findData(30))
    assert panel.repo.preferred_interval() == 30
    assert panel.repo.progress(panel.current_run)["interval_seconds"] == 0
    assert not panel.speed_warning.isHidden()
    assert not panel.test_clock.calls


@pytest.mark.parametrize("seconds", [1.5, 0.5, 0])
def test_saved_fast_speed_is_restored_when_panel_is_recreated(window, qtbot, seconds):
    panel = window.x_panel
    panel.speed.setCurrentIndex(panel.speed.findData(seconds))
    recreated = x_ui.XPanel(
        window.store,
        window.make_table,
        window.fill_table,
        fetcher=lambda *args: pytest.fail("No automatic requests"),
    )
    qtbot.addWidget(recreated)
    try:
        assert recreated.speed.currentData() == seconds
        assert not recreated.speed_warning.isHidden()
    finally:
        recreated.shutdown()


def test_manual_preview_start_skip_filter_and_export(window, qtbot, monkeypatch, tmp_path):
    panel = window.x_panel
    panel.modes.setCurrentIndex(1)
    panel.manual.setPlainText("hzwu9\nHZWU9\n_name\nbad-name")
    panel.preview()
    wait_done(qtbot, panel)
    progress = panel.repo.progress(panel.current_run)
    assert progress["total"] == 2 and progress["invalid"] == 1 and progress["duplicates"] == 1
    assert not panel.test_clock.calls
    panel.resume()
    wait_done(qtbot, panel)
    assert panel.repo.counts() == {"Taken": 2}
    assert "Taken" in panel.table.item(0, 1).text()
    panel.classification.setCurrentIndex(panel.classification.findData("Taken"))
    panel.search.setText("hzwu9")
    qtbot.waitUntil(lambda: len(panel.rows) == 1 and panel.rows[0]["username"] == "hzwu9")
    path = tmp_path / "x.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(path), ""))
    panel.export()
    wait_done(qtbot, panel)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        records = list(csv.DictReader(stream))
    assert len(records) == 1 and records[0]["preliminary"] == "True"
    assert "hzwu9" in records[0]["warning"]
    panel.start()
    wait_done(qtbot, panel)
    assert panel.repo.progress(panel.current_run)["skipped"] == 2
    assert len(panel.test_clock.calls) == 2


def test_selected_recheck_confirmation_changes_and_roblox_separation(window, qtbot, monkeypatch):
    panel = window.x_panel
    window.tabs.setCurrentWidget(panel)
    panel.modes.setCurrentIndex(1)
    panel.manual.setPlainText("valid_name")
    panel.start()
    wait_done(qtbot, panel)
    panel.workspace.setCurrentIndex(1)
    panel.ensureWidgetVisible(panel.table)
    qtbot.mouseClick(
        panel.table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=panel.table.visualItemRect(panel.table.item(0, 0)).center(),
    )
    assert panel.selected()
    monkeypatch.setattr(panel, "_confirm", lambda _: False)
    panel.recheck_selected()
    assert len(panel.test_clock.calls) == 1
    monkeypatch.setattr(panel, "_confirm", lambda _: True)
    panel.fetcher = lambda *a: response(False, "is_banned_word")
    panel.recheck_selected()
    wait_done(qtbot, panel)
    assert panel.rows[0]["classification"] == "Reserved" and panel.rows[0]["changed"], panel.status.text()
    assert len(panel.repo.history("valid_name")) == 2
    assert window.roblox.repo.known_count() == 0
    panel.recheck_all()
    wait_done(qtbot, panel)
    assert len(panel.repo.history("valid_name")) == 3


def test_generation_file_import_and_speed_persistence(window, qtbot, monkeypatch, tmp_path):
    panel = window.x_panel
    panel.minimum_length.setValue(15)
    panel.maximum_length.setValue(15)
    panel.quantity.setText("3")
    panel.speed.setCurrentIndex(panel.speed.findData(10))
    panel.preview()
    wait_done(qtbot, panel)
    assert panel.repo.progress(panel.current_run)["total"] == 3
    assert panel.repo.progress(panel.current_run)["interval_seconds"] == 10
    panel.speed.setCurrentIndex(panel.speed.findData(30))
    panel.resume()
    wait_done(qtbot, panel)
    assert panel.test_clock.waits == [10, 10]
    assert all(len(name) == 15 for name, _ in panel.test_clock.calls)
    assert XStore(window.store).preferred_interval() == 30
    path = tmp_path / "names.csv"
    path.write_text("username,note\nnew_name,fixture\n", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *args, **kwargs: ([str(path)], ""))
    panel.modes.setCurrentIndex(1)
    panel.add_files()
    panel.add_files()
    assert panel.files.count() == 1
    panel.start()
    wait_done(qtbot, panel)
    assert panel.repo.history("new_name")


def test_unknown_disables_new_checks_preserves_pending_and_evidence(window, qtbot):
    panel = window.x_panel
    panel.modes.setCurrentIndex(1)
    panel.manual.setPlainText("valid_name\nfreshname")
    panel.fetcher = lambda *args: response(False, "custom_message")
    panel.start()
    wait_done(qtbot, panel)
    assert panel.repo.progress(panel.current_run)["completed"] == 1
    assert panel.repo.progress(panel.current_run)["pending"] == 1
    assert not panel.start_button.isEnabled() and not panel.resume_button.isEnabled()
    assert "custom_message" in json.loads(panel.repo.history("valid_name")[0]["evidence"])["response_data"]


def test_saved_paused_operation_selected_without_automatic_resume(window, qtbot):
    panel = window.x_panel
    identity = panel.repo.prepare(x.input_spec("valid_name"))
    panel.repo.start(identity)
    panel.repo.recover()
    panel.current_run = None
    panel.refresh()
    assert panel.current_run == identity and panel.resume_button.isEnabled()
    assert not panel.test_clock.calls


def test_stop_during_real_cancellable_delay_and_safe_close(window, qtbot, monkeypatch):
    panel = window.x_panel
    calls = []

    def fetch(*args):
        calls.append(args)
        return response()

    monkeypatch.setattr(x_ui, "XRunner", XRunner)
    panel.fetcher = fetch
    panel.modes.setCurrentIndex(1)
    panel.manual.setPlainText("valid_name\nfreshname")
    panel.start()
    qtbot.waitUntil(lambda: panel.runner is not None and len(calls) == 1)
    panel.stop()
    wait_done(qtbot, panel)
    assert panel.repo.progress(panel.current_run)["state"] == "Paused"
    assert panel.repo.progress(panel.current_run)["pending"] == 1
    assert len(calls) == 1


def test_restore_refuses_to_replace_database_while_x_busy(window, monkeypatch):
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *a, **kw: pytest.fail("Restore must be blocked")
    )
    window.x_panel.busy = True
    try:
        window.restore_database()
    finally:
        window.x_panel.busy = False


def test_cancelled_historical_preparation_never_starts_checker(window, qtbot, monkeypatch):
    panel = window.x_panel
    panel.modes.setCurrentIndex(1)
    panel.manual.setPlainText("valid_name")
    panel.start()
    wait_done(qtbot, panel)
    original = panel.repo.prepare

    def cancel_preparation(*args, **kwargs):
        panel.cancel.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(panel.repo, "prepare", cancel_preparation)
    monkeypatch.setattr(panel, "_confirm", lambda _: True)
    panel.recheck_all()
    wait_done(qtbot, panel)
    assert panel.repo.progress(panel.current_run)["state"] == "PreparationPaused"
    assert len(panel.test_clock.calls) == 1
