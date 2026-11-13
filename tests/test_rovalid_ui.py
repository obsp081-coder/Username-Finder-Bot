"""Offscreen native-process preparation and mocked checking integration."""

import pytest
from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtWidgets import QMessageBox

from username_finder import rovalid, rovalid_review
from username_finder.gui import MainWindow
from username_finder.roblox_activity import RobloxActivity
from username_finder.rovalid_ui import RoValidPanel


@pytest.fixture
def desktop(qtbot, tmp_path, monkeypatch):
    # Allowed-path tests simulate review only in this process; production ships blocked.
    # Managed children inherit no override and remain independently fail-closed.
    # Mock only parent reviews; real children never inherit an authorization.
    original = rovalid_review.journal

    def parent_review(database):
        records = original(database)
        snapshot = rovalid_review.inspect(database)
        return records + [
            dict(
                scope=rovalid_review.scope(mode, connection),
                decision="authorize",
                fingerprint=snapshot["fingerprint"],
            )
            for mode in rovalid_review.MODES
            for connection in rovalid_review.CONNECTIONS
        ]

    monkeypatch.setattr(rovalid_review, "journal", parent_review)
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    window = MainWindow(tmp_path / "test.db")
    window.timer.stop()
    qtbot.addWidget(window)
    window.show()
    yield window
    window.close()
    qtbot.waitUntil(lambda: window.closed, timeout=10000)


def wait_idle(qtbot, panel):
    qtbot.waitUntil(lambda: not panel.busy, timeout=15000)
    qtbot.wait(20)


def test_navigation_and_legacy_pages_preserved(desktop, qtbot):
    assert isinstance(desktop.roblox, RoValidPanel)
    assert not hasattr(desktop.roblox, "bulk") and not hasattr(desktop.roblox, "prefilter")
    assert desktop.tabs.count() == 8
    titles = [desktop.navigation.item(i).text() for i in range(desktop.navigation.count())]
    assert (
        titles.index("اجتماعي · Social")
        < titles.index("سناب")
        < titles.index("ألعاب · Games")
        < titles.index("Roblox")
    )
    pages = [desktop.search_tabs.widget(i) for i in range(4)]
    for row in range(desktop.navigation.count()):
        item = desktop.navigation.item(row)
        target = item.data(Qt.ItemDataRole.UserRole)
        if target is not None:
            desktop.navigation.setCurrentRow(row)
            assert desktop.tabs.currentIndex() == target
    assert pages == [desktop.search_tabs.widget(i) for i in range(4)]
    assert desktop.roblox.layoutDirection() == Qt.LayoutDirection.RightToLeft
    assert [desktop.roblox.workspace.tabText(i) for i in range(4)] == [
        "الإعدادات",
        "التشغيل",
        "النتائج",
        "السجل",
    ]
    assert not desktop.roblox.busy


def test_prepare_child_import_dedupe_persistence_and_no_legacy_jobs(desktop, qtbot, tmp_path):
    panel = desktop.roblox
    source = tmp_path / "source.txt"
    source.write_text("Example\nEXAMPLE\nGoodName\nInvalid!\n", encoding="utf8")
    panel.source.setCurrentIndex(1)
    panel.input_path.setText(str(source))
    panel.checking_mode.setCurrentIndex(1)
    panel.gap.setValue(0.375)
    panel.concurrency.setValue(3)
    before = desktop.store.db.total_changes
    beats = []
    timer = QTimer()
    timer.timeout.connect(lambda: beats.append(1))
    timer.start(5)
    panel.prepare()
    identity = panel.current_run
    panel.prepare()  # A second action cannot replace the in-flight run.
    assert panel.current_run == identity
    wait_idle(qtbot, panel)
    timer.stop()
    assert beats and panel.outcome == "prepared"
    assert (panel.record["valid"], panel.record["invalid"], panel.record["duplicates"]) == (2, 1, 1)
    assert desktop.store.db.total_changes == before
    assert not panel.repo.runs()
    saved = rovalid.read_json(panel.root / "settings.json")
    assert saved["gap"] == 0.375 and saved["mode"] == "validator" and saved["concurrency"] == 3
    # No secret values in the request manifest, saved settings, arguments or history.
    assert "proxies" not in panel.record and "webhook" not in saved
    assert panel.history.rowCount() == 1
    # Recreate only the panel against the same isolated app Store.
    second = RoValidPanel(desktop.store, desktop.make_table, desktop.fill_table)
    qtbot.addWidget(second)
    assert second.options() == panel.options()
    assert second.history.rowCount() == 1 and not second.busy


def test_historical_block_status_and_direct_start_are_read_only(desktop, monkeypatch):
    panel = desktop.roblox
    guard = RobloxActivity(desktop.store.path)
    guard.block.write_bytes(b"http_429")
    before = desktop.store.db.total_changes
    monkeypatch.setattr(panel, "launch", lambda *a: pytest.fail("Blocked launch"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: pytest.fail("No confirmation while blocked"))
    panel.current_run = "a" * 32
    panel.record = dict(state="Prepared", settings=panel.options())
    panel.controls()
    assert "تاريخي" in panel.restriction.text() and "429" in panel.restriction.text()
    desktop.job_manager.refresh()
    assert "مراجعة RoValid مطلوبة" in desktop.job_manager.summary.text()
    assert not panel.start_button.isEnabled() and not panel.resume_button.isEnabled()
    panel.start()
    panel.resume()
    assert guard.block.read_bytes() == b"http_429" and desktop.store.db.total_changes == before


def test_child_independently_rejects_live_start_even_if_parent_allowed_path_is_mocked(
    desktop, qtbot, monkeypatch
):
    panel = desktop.roblox
    panel.count.setValue(2)
    panel.prepare()
    wait_idle(qtbot, panel)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    before = desktop.store.db.total_changes
    panel.start()
    wait_idle(qtbot, panel)
    assert panel.outcome == "error"
    assert panel.record["state"] == "Prepared"
    assert not (panel.root / panel.current_run / "results").exists()
    assert desktop.store.db.total_changes == before


def test_confirmation_required_and_saved_resume_settings(desktop, monkeypatch):
    panel = desktop.roblox
    panel.current_run = "a" * 32
    panel.record = dict(state="Prepared", settings=panel.options())
    calls = []
    monkeypatch.setattr(panel, "launch", lambda *args: calls.append(args))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    panel.start()
    assert not calls
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    panel.start()
    assert len(calls) == 1 and not calls[0][1]["resume"]
    panel.record["state"] = "Stopped"
    panel.concurrency.setValue(17)
    panel.resume()
    assert calls[-1][1]["resume"]  # Child uses the stored settings, never the edited controls.
    assert panel.record["settings"]["concurrency"] == rovalid.DEFAULTS["concurrency"]


def test_durable_results_async_view_and_safe_export(desktop, qtbot, tmp_path, monkeypatch):
    panel = desktop.roblox
    panel.count.setValue(2)
    panel.prepare()
    wait_idle(qtbot, panel)
    directory = rovalid.run_directory(panel.root, panel.current_run)
    results = directory / "results"
    results.mkdir()
    (results / "hits.txt").write_text("Example\n", encoding="utf8")
    (results / "unresolved.txt").write_text("OtherName\n", encoding="utf8")
    panel.refresh_results()
    wait_idle(qtbot, panel)
    assert panel.result_table.rowCount() == 2
    assert panel.result_table.item(0, 1).text() == "متاح أوليًا"
    assert panel.filter.findData("Taken") == -1
    panel.search.setText("missing")
    panel.refresh_results()
    wait_idle(qtbot, panel)
    assert panel.result_table.rowCount() == 0
    panel.search.clear()
    destination = tmp_path / "safe.csv"
    monkeypatch.setattr(
        "username_finder.rovalid_ui.QFileDialog.getSaveFileName", lambda *a: (str(destination), "")
    )
    panel.filter.setCurrentIndex(0)
    panel.export()
    wait_idle(qtbot, panel)
    assert "Example" in destination.read_text(encoding="utf-8-sig")


def test_error_exit_never_claims_success(desktop, qtbot, tmp_path):
    panel = desktop.roblox
    panel.source.setCurrentIndex(1)
    panel.input_path.setText(str(tmp_path / "absent.txt"))
    panel.prepare()
    wait_idle(qtbot, panel)
    assert panel.outcome == "error"
    assert "لا استئناف تلقائي" in panel.activity_status.text()
    assert not panel.start_button.isEnabled()


def test_legacy_manager_cannot_dispatch_or_enable_overnight(desktop, monkeypatch):
    panel = desktop.roblox
    identity = panel.repo.create_run(["OldName"])
    manager = desktop.job_manager
    manager.refresh()
    index = next(i for i, row in enumerate(manager.rows) if row["id"] == identity)
    manager.table.setCurrentCell(index, 0)
    assert not manager.start_button.isEnabled() and not manager.overnight_button.isEnabled()
    before = desktop.store.db.total_changes
    manager.start_selected()
    manager.overnight_selected()
    assert desktop.store.db.total_changes == before
    manager.open_selected()
    assert desktop.tabs.currentWidget() is panel and panel.workspace.currentIndex() == 3
    assert panel.history.rowCount() == 0


def test_progress_separates_attempts_and_resolved_and_dashboard_stop(desktop, monkeypatch):
    panel = desktop.roblox
    panel.current_run = "a" * 32
    panel.show_progress(
        dict(
            input_total=10,
            resolved=4,
            works=1,
            taken=2,
            censored=1,
            invalid=0,
            requests=9,
            rps=3,
            checks_rps=4 / 3,
            elapsed=3,
            ratelimited=2,
            proxy_alive=0,
        )
    )
    assert panel.progress.value() == 400
    assert "3.00" in panel.statistics.text() and "1.33" in panel.statistics.text()
    called = []
    monkeypatch.setattr(panel, "stop", lambda: called.append("stop"))
    desktop.dashboard.operation_cards[0].stop()
    assert called == ["stop"]
    desktop.dashboard.quick_actions[2].click()
    assert panel.checking_mode.currentData() == "two_stage" and panel.workspace.currentIndex() == 0


def test_invalid_saved_statistics_fail_closed_without_rewriting_evidence(desktop):
    panel = desktop.roblox
    panel.record = dict(state="Stopped", summary={"requests": "bad"})
    panel.show_saved_statistics()
    panel.controls()
    assert panel.record["state"] == "ReviewRequired"
    assert not panel.resume_button.isEnabled()


@pytest.mark.parametrize("width,height", [(900, 650), (1180, 850)])
def test_offscreen_layout_accessible(desktop, qtbot, width, height):
    desktop.resize(width, height)
    desktop.tabs.setCurrentWidget(desktop.roblox)
    qtbot.wait(20)
    assert desktop.width() == width
    assert desktop.navigation.verticalScrollBar().maximum() == 0
    panel = desktop.roblox
    for index in range(4):
        panel.workspace.setCurrentIndex(index)
        assert panel.workspace.currentWidget().width() <= panel.width()
    panel.workspace.setCurrentIndex(0)
    assert panel.settings_page.verticalScrollBar().maximum() > 0
    assert panel.webhook.echoMode() == panel.webhook.EchoMode.Password
    assert not panel.proxy_entries.revealed


def test_stop_shutdown_and_failed_start_without_killing_process(desktop, monkeypatch):
    panel = desktop.roblox
    writes = []
    monkeypatch.setattr(panel.process, "state", lambda: QProcess.ProcessState.Running)
    monkeypatch.setattr(panel.process, "write", lambda value: writes.append(value))
    panel.action = "run"
    panel.stop()
    panel.stop()
    assert writes == [b"stop\n"] and panel.stopping
    panel.shutdown()
    assert panel.closing and writes == [b"stop\n"]
    monkeypatch.undo()
    panel.closing = panel.stopping = False
    panel._process_error(QProcess.ProcessError.FailedToStart)
    assert "تعذر بدء" in panel.activity_status.text()
