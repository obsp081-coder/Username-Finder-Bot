import json

import pytest
from osintgram_fixture import PROFILE, TOOLS, FakeBackend, MemoryCredentials, MemorySettings, param
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtWidgets import QFileDialog, QMessageBox

from username_finder.gui import MainWindow
from username_finder.osintgram_backend import BackendManager


@pytest.fixture
def ui(qtbot, tmp_path, monkeypatch):
    backend = FakeBackend()
    manager = BackendManager(settings=MemorySettings(), credentials=MemoryCredentials())
    window = MainWindow(tmp_path / "osintgram-ui.db", osintgram_options={"manager": manager})
    qtbot.addWidget(window)
    window.timer.stop()
    monkeypatch.setattr(window, "unsaved_choice", lambda: QMessageBox.StandardButton.Discard)
    analysis = window.instagram.analysis
    analysis.client._url = lambda path: QUrl(backend.url + path)
    analysis.manager._url = lambda path: QUrl(backend.url + path)
    analysis.activate = lambda: None
    window.instagram.modes.setCurrentIndex(1)
    window.tabs.setCurrentWidget(window.instagram)
    window.show()
    yield window, analysis, backend
    window.close()
    qtbot.waitUntil(lambda: window.closed, timeout=6500)
    backend.close()


def discover(panel, qtbot):
    panel.connect_backend()
    assert panel.state == "connecting"
    qtbot.waitUntil(lambda: panel.state == "connected", timeout=2000)


def test_connection_diagnostics_are_safe_and_cleared_on_retry(ui, qtbot):
    window, panel, backend = ui
    backend.tools_status = 503
    backend.tools_raw = b"password=SECRET; sessionid=SECRET"
    panel.connect_backend()
    qtbot.waitUntil(lambda: panel.manager.state == "error")
    tooltip = panel.connection_status.toolTip()
    assert "http_error" in tooltip and "HTTP 503" in tooltip
    assert "SECRET" not in tooltip + panel.connection_status.text()
    backend.tools_status = 200
    backend.tools_raw = None
    discover(panel, qtbot)
    assert panel.connection_status.toolTip() == ""


def choose(panel, name, checked=True):
    for index in range(panel.commands.topLevelItemCount()):
        group = panel.commands.topLevelItem(index)
        for row in range(group.childCount()):
            item = group.child(row)
            if item.data(0, Qt.ItemDataRole.UserRole) == name:
                panel.commands.setCurrentItem(item)
                item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                return item
    raise AssertionError(name)


def run(panel, qtbot):
    panel.start_analysis()
    qtbot.waitUntil(lambda: not panel.running, timeout=3000)
    qtbot.waitUntil(lambda: not panel.client.busy, timeout=2000)


def test_dynamic_discovery_params_target_and_local_preview_are_isolated(ui, qtbot, monkeypatch):
    window, panel, backend = ui
    assert not backend.calls and not panel.start.isEnabled()
    baseline = window.editor_signature()
    dirty = window.preset_dirty

    def forbidden(*args, **kwargs):
        raise AssertionError("Analysis must not create availability jobs or invoke its engine")

    monkeypatch.setattr(window.store, "create_job", forbidden)
    monkeypatch.setattr(window, "start_engine", forbidden)
    discover(panel, qtbot)
    assert len(panel.tools) == len(TOOLS)
    assert panel.limit.value() == 10
    assert panel.layoutDirection() == Qt.LayoutDirection.RightToLeft
    assert panel.target.layoutDirection() == Qt.LayoutDirection.LeftToRight
    for name in ("get_followers_email", "future_complex_tool", "get_post_comments"):
        choose(panel, name, False)
    choose(panel, "get_followers_email")
    assert panel.editors["match_limit"].text() == "20"
    assert panel.editors["max_checks"].text() == "1000"
    panel.start_analysis()
    assert "أدخل اسم" in panel.execution_status.text() and not panel.running
    assert all(path != "/api/run" for method, path, body in backend.calls)
    choose(panel, "get_followers_email", False)
    choose(panel, "search_hashtag")
    panel.start_analysis()
    assert "hashtag" in panel.execution_status.text() and not panel.running
    panel.editors["hashtag"].setText("fixture")
    panel.editors["sort"].setCurrentIndex(panel.editors["sort"].findData("recent"))
    panel.limit.setValue(25)
    run(panel, qtbot)
    request = next(body for method, path, body in backend.calls if path == "/api/run")
    assert request["target"] is None and request["max_items"] == 25
    assert request["calls"] == [
        {"name": "search_hashtag", "args": {"hashtag": "fixture", "sort": "recent", "limit": 200}}
    ]
    assert panel.outcome == "complete" and "لا توجد نتائج" in panel.result_status.text()
    assert window.editor_signature() == baseline and window.preset_dirty == dirty
    assert not window.store.jobs() and window.instagram.result_table.rowCount() == 0
    assert not window.instagram.start_button.isEnabled()
    assert all(card.text().endswith("\n0") for card in window.instagram.counters.values())
    window.instagram.manual.setText("@local_preview")
    window.instagram.preview()
    assert window.instagram.last_preview["counts"]["valid"] == 1


def test_profile_lists_complex_json_and_explicit_exports(ui, qtbot, tmp_path, monkeypatch):
    window, panel, backend = ui
    discover(panel, qtbot)
    panel.target.setText("@fixture_user")
    for name in ("get_user_info", "get_followers", "future_complex_tool"):
        choose(panel, name)
    run(panel, qtbot)
    assert panel.outcome == "complete" and len(panel.result_items) == 3
    panel.result_picker.setCurrentIndex(0)
    text = "\n".join(panel.table.item(row, 1).text() for row in range(panel.table.rowCount()))
    assert PROFILE["username"] in text and str(PROFILE["id"]) in text and "حساب اختباري" in text
    assert "غير متوفر (null)" in text and "نعم" in text and "لا" in text
    assert "<img" in text  # Displayed as literal text, never fetched/rendered as HTML.
    assert not panel.csv_export.isEnabled()  # Nested bio_links require JSON or their own dataset.
    panel.dataset_picker.setCurrentIndex(1)
    assert panel.csv_export.isEnabled()
    panel.json_toggle.setChecked(True)
    assert not panel.json_view.isHidden() and "biography" in panel.json_view.toPlainText()
    panel.result_picker.setCurrentIndex(2)
    assert panel.dataset_picker.count() == 3
    panel.dataset_picker.setCurrentIndex(1)
    assert panel.table.rowCount() == 2 and not panel.csv_export.isEnabled()
    values = [panel.table.item(r, c).text() for r in range(2) for c in range(2)]
    assert "غير موجود في الاستجابة" in values and "غير متوفر (null)" in values
    panel.result_picker.setCurrentIndex(1)
    assert panel.csv_export.isEnabled()
    path = tmp_path / "analysis.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), "JSON (*.json)"))
    panel.save_json()
    exported = json.loads(path.read_text(encoding="utf-8"))
    assert exported["status"] == "complete" and exported["results"][0]["result"]["id"] == PROFILE["id"]
    csv_path = tmp_path / "analysis.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(csv_path), "CSV (*.csv)"))
    panel.save_csv()
    assert "fixture_follower" in csv_path.read_text(encoding="utf-8-sig")
    assert not window.store.jobs()


def test_partial_and_stream_errors_are_not_success_or_availability(ui, qtbot):
    window, panel, backend = ui
    discover(panel, qtbot)
    panel.target.setText("fixture")
    choose(panel, "get_user_info")
    choose(panel, "get_followers")
    backend.results["get_followers"] = {"error": "login required session_token=DO_NOT_DISPLAY"}
    run(panel, qtbot)
    assert panel.outcome == "partial" and panel.state == "error"
    assert "جلسة" in panel.result_status.text()
    assert "DO_NOT_DISPLAY" not in json.dumps(panel.document)
    assert not window.instagram.result_table.rowCount()
    backend.mode = "stream_error"
    discover(panel, qtbot)
    run(panel, qtbot)
    assert panel.outcome == "failed" and "DO_NOT_DISPLAY" not in json.dumps(panel.document)
    assert "جلسة" in panel.execution_status.text()


def test_responsive_gui_no_duplicate_execution_and_real_cancel_payload(ui, qtbot):
    window, panel, backend = ui
    discover(panel, qtbot)
    panel.target.setText("fixture")
    choose(panel, "get_user_info")
    backend.delay = 2
    ticks = []
    timer = QTimer(window)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)
    panel.start_analysis()
    assert panel.state == "running" and not panel.start.isEnabled()
    panel.start_analysis()
    qtbot.waitUntil(lambda: backend.active is not None, timeout=2000)
    qtbot.wait(80)
    assert len(ticks) >= 4
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
    window.tabs.setCurrentWidget(window.instagram)
    assert window.tabs.currentWidget() is window.instagram
    with qtbot.waitSignal(panel.client.finished, timeout=2000):
        panel.cancel_analysis()
    qtbot.waitUntil(lambda: not panel.client.busy)
    assert panel.outcome == "cancelled" and panel.cancel_acknowledged
    requests = [(path, body) for method, path, body in backend.calls if method == "POST"]
    assert len([path for path, body in requests if path == "/api/run"]) == 1
    assert requests[-1] == ("/api/cancel", {"run_id": panel.document["request"]["run_id"]})
    timer.stop()


def test_cancel_before_registration_is_reissued_once_at_start(ui, qtbot):
    window, panel, backend = ui
    discover(panel, qtbot)
    backend.build_delay = 0.15
    backend.delay = 2
    panel.target.setText("fixture")
    choose(panel, "get_user_info")
    panel.start_analysis()
    panel.cancel_analysis()
    qtbot.waitUntil(lambda: not panel.running, timeout=2500)
    qtbot.waitUntil(lambda: not panel.client.busy)
    assert panel.cancel_acknowledged and panel.outcome == "cancelled"
    assert len([path for method, path, body in backend.calls if path == "/api/cancel"]) == 2


def test_close_running_analysis_requests_cancel_and_closes_without_threads(ui, qtbot):
    window, panel, backend = ui
    discover(panel, qtbot)
    backend.delay = 2
    panel.target.setText("fixture")
    choose(panel, "get_user_info")
    panel.start_analysis()
    qtbot.waitUntil(lambda: backend.active is not None, timeout=2000)
    window.close()
    qtbot.waitUntil(lambda: window.closed, timeout=3000)
    assert not panel.client.busy and not window.isVisible()
    assert any(path == "/api/cancel" for method, path, body in backend.calls)


@pytest.mark.parametrize("size", [(900, 650), (1180, 850)])
def test_analysis_layout_and_local_modes_preserve_state(ui, qtbot, size):
    window, panel, backend = ui
    window.resize(*size)
    discover(panel, qtbot)
    window.instagram.manual.setText("local_name")
    window.instagram.preview()
    panel.target.setText("fixture")
    choose(panel, "get_user_info")
    qtbot.wait(30)
    assert window.instagram.horizontalScrollBar().maximum() == 0
    for widget in (panel.target, panel.commands, panel.start, panel.table):
        panel.pages.setCurrentIndex(2 if widget is panel.table else 1)
        center = widget.mapTo(window.instagram.widget(), widget.rect().center())
        window.instagram.ensureVisible(center.x(), center.y(), 0, 20)
        qtbot.wait(5)
        assert (
            window.instagram.viewport()
            .rect()
            .contains(widget.mapTo(window.instagram.viewport(), widget.rect().center()))
        )
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
    assert panel.target.text() == "fixture" and panel.selected() == ["get_user_info"]
    assert window.instagram.last_preview["counts"]["valid"] == 1
    assert len(backend.calls) == 2


def test_dynamic_typed_parameters_and_new_command_are_not_dropped(ui, qtbot):
    window, panel, backend = ui
    backend.tools = [
        {
            "name": "new_tool",
            "needs_target": False,
            "params": [
                param("enabled", "boolean", False),
                param("options", "object", {"x": 1}),
                param("items", "array", []),
            ],
        }
    ]
    discover(panel, qtbot)
    choose(panel, "new_tool")
    panel.editors["options"].setText("invalid JSON")
    panel.start_analysis()
    assert "options" in panel.execution_status.text() and not panel.running
    panel.editors["options"].setText('{"x": 2}')
    panel.editors["items"].setText('["fixture"]')
    run(panel, qtbot)
    call = panel.document["request"]["calls"][0]
    assert call == {"name": "new_tool", "args": {"enabled": False, "options": {"x": 2}, "items": ["fixture"]}}


def test_reconnect_preserves_parameters_and_selection(ui, qtbot):
    window, panel, backend = ui
    discover(panel, qtbot)
    choose(panel, "search_hashtag")
    panel.editors["hashtag"].setText("fixture")
    panel.editors["sort"].setCurrentIndex(panel.editors["sort"].findData("recent"))
    backend.tools_status = 502
    panel.connect_backend()
    qtbot.waitUntil(lambda: panel.state == "error")
    assert not panel.start.isEnabled()
    backend.tools_status = 200
    discover(panel, qtbot)
    assert panel.selected() == ["search_hashtag"]
    assert panel.editors["hashtag"].text() == "fixture"
    assert panel.editors["sort"].currentData() == "recent"


def test_large_result_projection_is_bounded_but_export_retains_rows(ui, qtbot, tmp_path):
    from username_finder.osintgram_results import export_json

    window, panel, backend = ui
    discover(panel, qtbot)
    choose(panel, "future_complex_tool")
    backend.results["future_complex_tool"] = [
        {f"field_{column}": row for column in range(40)} for row in range(500)
    ]
    run(panel, qtbot)
    assert panel.table.rowCount() == 300 and panel.table.columnCount() == 32
    assert len(panel.document["results"][0]["result"]) == 500
    path = tmp_path / "large.json"
    export_json(path, panel.document)
    assert len(json.loads(path.read_text(encoding="utf-8"))["results"][0]["result"]) == 500


def test_failed_cancellation_is_not_reported_as_confirmed_stop(ui, qtbot):
    window, panel, backend = ui
    discover(panel, qtbot)
    panel.target.setText("fixture")
    choose(panel, "get_user_info")
    backend.delay = 0.3
    backend.cancel_raw = b'{"unexpected": true}'
    panel.start_analysis()
    qtbot.waitUntil(lambda: backend.active is not None)
    panel.cancel_analysis()
    qtbot.waitUntil(lambda: panel.cancel.isEnabled())
    assert not panel.cancel_acknowledged and "لم يُؤكد" in panel.execution_status.text()
    qtbot.waitUntil(lambda: not panel.running)
    assert panel.outcome == "cancelled" and "لم تؤكد" in panel.execution_status.text()


def test_all_commands_failing_does_not_report_partial_success(ui, qtbot):
    window, panel, backend = ui
    discover(panel, qtbot)
    panel.target.setText("fixture")
    choose(panel, "get_user_info")
    backend.results["get_user_info"] = {"error": "Session expired DO_NOT_DISPLAY"}
    run(panel, qtbot)
    assert panel.outcome == "failed" and "فشلت جميع" in panel.execution_status.text()
    assert "جلسة" in panel.result_status.text()
    assert "DO_NOT_DISPLAY" not in json.dumps(panel.document)


def test_automatic_activation_is_scoped_to_analysis_and_reuses_identity(ui, qtbot):
    window, panel, backend = ui
    window.instagram.modes.setCurrentIndex(0)
    panel.activate = panel.manager.ensure
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
    assert not backend.calls
    window.instagram.modes.setCurrentIndex(1)
    qtbot.waitUntil(lambda: panel.state == "connected")
    assert [path for method, path, body in backend.calls] == ["/api/about", "/api/tools"]
    window.instagram.modes.setCurrentIndex(0)
    window.instagram.modes.setCurrentIndex(1)
    qtbot.wait(20)
    assert len(backend.calls) == 2 and not window.store.jobs()


def test_arabic_details_search_selection_and_known_backend_restrictions(ui, qtbot):
    from PySide6.QtCore import Qt

    window, panel, backend = ui
    panel.manager.backend = "instagrapi"
    tools = [
        {"name": name, "params": [], "needs_target": True}
        for name in ("get_user_info", "get_account_about", "get_suggested_profiles", "unfamiliar_tool")
    ]
    panel._discovered(tools)
    item = choose(panel, "get_account_about", False)
    assert not item.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert "HikerAPI" in panel.parameter_title.text() and "غير مدعوم" in panel.parameter_title.text()
    assert "get_account_about" not in panel.parameter_title.text()
    choose(panel, "get_user_info")
    assert "يعرض الاسم والنبذة" in panel.parameter_title.text() and panel.parameter_title.wordWrap()
    panel.search.setText("معلومات الحساب")
    assert "1" in panel.selection_count.text()
    assert panel.selected() == ["get_user_info"]
    panel.clear_selection()
    assert not panel.selected()
    choose(panel, "unfamiliar_tool", False)
    assert "unfamiliar_tool" in panel.parameter_title.text()


def test_new_internal_tab_bars_and_limit_keep_wheel_protection(ui, qtbot):
    from test_ui_quality import wheel

    window, panel, backend = ui
    discover(panel, qtbot)
    before = panel.limit.value()
    for bar in (window.instagram.modes, panel.pages.tabBar()):
        index = bar.currentIndex()
        wheel(bar)
        assert bar.currentIndex() == index
    wheel(panel.limit)
    assert panel.limit.value() == before
    qtbot.keyClick(panel.limit, Qt.Key.Key_Up)
    assert panel.limit.value() == before + 1


def test_reported_external_backend_updates_restrictions_without_requiring_our_credentials(ui, qtbot):
    window, panel, backend = ui
    backend.tools = backend.tools + [{"name": "get_account_about", "params": [], "needs_target": True}]
    backend.identity_backend = "instagrapi"
    discover(panel, qtbot)
    panel.target.setText("fixture")
    choose(panel, "get_user_info")
    run(panel, qtbot)
    assert panel.manager.backend == "instagrapi" and not panel.manager.owned
    assert panel._session_usable() and panel.start.isEnabled()
    item = choose(panel, "get_account_about", False)
    assert not item.flags() & Qt.ItemFlag.ItemIsUserCheckable
