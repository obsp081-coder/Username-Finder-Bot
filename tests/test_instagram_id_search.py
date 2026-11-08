import json
from threading import Event

import pytest
from osintgram_fixture import PROFILE, FakeBackend, MemoryCredentials, MemorySettings
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtWidgets import QApplication

from username_finder.gui import MainWindow
from username_finder.instagram_id import account_identifier
from username_finder.osintgram_backend import BackendManager


@pytest.fixture
def ui(qtbot, tmp_path):
    backend = FakeBackend()
    manager = BackendManager(
        settings=MemorySettings(directory=str(tmp_path / "unused")), credentials=MemoryCredentials()
    )
    window = MainWindow(tmp_path / "isolated.db", osintgram_options={"manager": manager})
    qtbot.addWidget(window)
    window.timer.stop()
    analysis, panel = window.instagram.analysis, window.file_search
    manager._url = analysis.client._url = panel.resolver.client._url = lambda path: QUrl(backend.url + path)
    window.tabs.setCurrentWidget(panel)
    window.show()
    path = tmp_path / "private-local-records.jsonl"
    identifier = str(PROFILE["id"])
    path.write_bytes(
        (
            json.dumps({"id": identifier, "name": "LOCAL_CONTENT_ONLY"})
            + "\n"
            + json.dumps({"id": int(identifier)})
            + "\n"
            + json.dumps({"phone": identifier})
            + "\n"
        ).encode()
    )
    panel.set_files([path])
    panel.mode.setCurrentIndex(1)
    panel.username.setText(" @Fixture_User ")
    yield window, panel, backend, path
    window.close()
    qtbot.waitUntil(lambda: window.closed, timeout=6500)
    backend.close()


def wait_done(panel, qtbot):
    qtbot.waitUntil(lambda: not panel.busy, timeout=4000)


def test_resolves_normalized_account_once_and_scans_only_exact_id(ui, qtbot):
    window, panel, backend, path = ui
    analysis = window.instagram.analysis
    original = (analysis.target.text(), analysis.selected(), analysis.document, window.editor_signature())
    panel.start.click()
    panel.start_search()
    wait_done(panel, qtbot)
    assert panel.resolved_id == str(PROFILE["id"])
    assert panel.model.query == panel.resolved_id and panel.model.rowCount() == 2
    assert panel.last_summary.status == "complete"
    assert "@fixture_user" in panel.resolution.text() and panel.resolved_id in panel.resolution.text()
    calls = [c[2] for c in backend.calls if c[1] == "/api/run"]
    assert len(calls) == 1 and calls[0]["target"] == "fixture_user"
    assert calls[0]["calls"] == [{"name": "get_user_info", "args": {}}]
    assert calls[0]["max_items"] == 1
    outbound = json.dumps(backend.calls)
    assert str(path) not in outbound and "LOCAL_CONTENT_ONLY" not in outbound
    assert panel.resolver.manager is analysis.manager
    assert original == (
        analysis.target.text(),
        analysis.selected(),
        analysis.document,
        window.editor_signature(),
    )
    assert not window.store.jobs() and not window.instagram.start_button.isEnabled()
    assert panel.username.layoutDirection() == Qt.LayoutDirection.LeftToRight
    panel.table.setCurrentIndex(panel.model.index(0, 0))
    qtbot.waitUntil(lambda: panel.page is not None)
    panel.copy_line.click()
    qtbot.waitUntil(lambda: QApplication.clipboard().text() == path.read_text().splitlines()[0])


@pytest.mark.parametrize(
    "value", [None, True, False, 0, -1, 123.0, "", "0", "00123", "1e3", "12 3", "١٢٣", {}, []]
)
def test_invalid_ids_are_not_guessed(value):
    with pytest.raises(ValueError):
        account_identifier({"username": "fixture_user", "id": value}, "fixture_user")


@pytest.mark.parametrize("value", [9876543210123456789, "9876543210123456789"])
def test_large_identifiers_preserve_all_decimal_digits(value):
    assert account_identifier({"username": "Fixture_User", "id": value}, "fixture_user") == str(value)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "mismatch",
        "malformed",
        "incomplete",
        "authentication",
        "rate",
        "stream_error",
        "timeout",
        "connection",
    ],
)
def test_resolution_failures_never_start_local_scan_or_expose_secrets(ui, qtbot, case):
    _, panel, backend, _ = ui
    if case == "missing":
        backend.results["get_user_info"] = {"username": "fixture_user"}
    elif case == "mismatch":
        backend.results["get_user_info"] = {"username": "another_user", "id": PROFILE["id"]}
    elif case in ("malformed", "incomplete", "stream_error"):
        backend.mode = case
    elif case in ("authentication", "rate"):
        backend.run_status = 401 if case == "authentication" else 429
        backend.detail = "session_secret=DO_NOT_DISPLAY" if case == "authentication" else "too many requests"
    elif case == "connection":
        backend.tools_status = 503
    elif case == "timeout":
        backend.delay = 3
        panel.resolver.client.run_timeout_ms = 40
    panel.start.click()
    wait_done(panel, qtbot)
    assert panel.task is None and panel.last_summary is None and panel.model.rowCount() == 0
    assert not panel.resolved_id and "لم يبدأ البحث" in panel.resolution.text()
    assert "DO_NOT_DISPLAY" not in panel.resolution.text()
    if case == "authentication":
        assert "مصادقة" in panel.resolution.text()
    elif case == "rate":
        assert "حد الطلبات" in panel.resolution.text()
    elif case == "timeout":
        assert "مهلة" in panel.resolution.text()


def test_invalid_username_and_busy_analysis_do_not_send_a_request(ui, qtbot):
    window, panel, backend, _ = ui
    panel.username.setText("invalid/name")
    panel.start.click()
    assert not backend.calls and not panel.busy
    panel.username.setText("fixture_user")
    window.instagram.analysis.running = True
    panel.start.click()
    assert "مشغولة" in panel.resolution.text() and not backend.calls
    window.instagram.analysis.running = False


@pytest.mark.parametrize("change", ["username", "mode", "cancel", "close"])
def test_stale_callbacks_cancellation_shutdown_and_gui_heartbeat(ui, qtbot, change):
    window, panel, backend, _ = ui
    backend.delay = 3
    ticks = []
    timer = QTimer(panel)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start(5)
    panel.start.click()
    qtbot.waitUntil(lambda: any(c[1] == "/api/run" for c in backend.calls))
    qtbot.wait(40)
    assert len(ticks) >= 3 and not panel.start.isEnabled()
    assert not window.instagram.analysis.start.isEnabled()
    if change == "username":
        panel.username.setText("new_user")
    elif change == "mode":
        panel.mode.setCurrentIndex(0)
    elif change == "cancel":
        panel.cancel.click()
    else:
        window.close()
    panel.resolver.resolved.emit("fixture_user", str(PROFILE["id"]))
    wait_done(panel, qtbot)
    assert not panel.resolved_id and panel.last_summary is None and panel.task is None
    if change == "close":
        qtbot.waitUntil(lambda: window.closed)
        assert panel.executor_closed and panel.resolver.client.closing
    timer.stop()


def test_startup_timeout_and_resolved_id_invalidation(ui, qtbot, monkeypatch):
    _, panel, backend, _ = ui
    monkeypatch.setattr(panel.resolver.manager, "ensure", lambda: None)
    panel.resolver.deadline.setInterval(40)
    panel.start.click()
    wait_done(panel, qtbot)
    assert "مهلة" in panel.resolution.text() and not backend.calls
    monkeypatch.undo()
    panel.start.click()
    wait_done(panel, qtbot)
    assert panel.resolved_id
    panel.username.setText("other_user")
    assert not panel.resolved_id
    assert len([c for c in backend.calls if c[1] == "/api/run"]) == 1


@pytest.mark.parametrize("close", [False, True])
def test_structured_scan_cancellation_and_shutdown(ui, qtbot, monkeypatch, close):
    import username_finder.file_search_ui as module

    window, panel, _, path = ui
    identifier = str(PROFILE["id"])
    path.write_bytes(('{"id":"' + identifier + '"}\n').encode() * 5000)
    entered = Event()
    original = module.search_account_ids

    def controlled(*args, **kwargs):
        receive = kwargs["on_batch"]

        def deliver(rows):
            accepted = receive(rows)
            entered.set()
            kwargs["cancel"].wait(3)
            return accepted

        kwargs["on_batch"] = deliver
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "search_account_ids", controlled)
    panel.limit.setCurrentText("1,000")
    panel.start.click()
    qtbot.waitUntil(entered.is_set)
    if close:
        window.close()
        qtbot.waitUntil(lambda: window.closed, timeout=2000)
        assert panel.executor_closed
    else:
        panel.cancel.click()
        wait_done(panel, qtbot)
        assert panel.last_summary.status == "cancelled"
