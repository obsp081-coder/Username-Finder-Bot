import ctypes
import json
import socket
import sys
from types import SimpleNamespace

import pytest
from osintgram_fixture import TOOLS, FakeBackend, MemoryCredentials, MemorySettings
from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox

from username_finder.osintgram_api import error_category, error_message
from username_finder.osintgram_backend import BackendManager
from username_finder.osintgram_catalog import CATALOG, explanation
from username_finder.osintgram_credentials import TARGET, Credential, CredentialError, WindowsCredentials
from username_finder.osintgram_login import authenticate
from username_finder.osintgram_process import OwnedProcess, WindowsJob, child_environment
from username_finder.osintgram_setup import AccountSetup


class FakeProcess(QObject):
    started = Signal()
    ended = Signal(int)
    failed = Signal()
    output = Signal(bytes)

    def __init__(self):
        super().__init__()
        self.active = False
        self.calls = []
        self.stops = 0
        self.writes = []
        self.dependencies = True
        self.start_failure = False

    def start(self, python, args, directory, environment, capture=False):
        self.calls.append((python, args, directory, environment, capture))
        self.active = True
        if self.start_failure:
            self.active = False
            QTimer.singleShot(0, self.failed.emit)
        elif "-c" in args:
            QTimer.singleShot(0, lambda: self.output.emit(json.dumps({"ready": self.dependencies}).encode()))
            QTimer.singleShot(1, lambda: self.finish(0))
        else:
            QTimer.singleShot(0, self.started.emit)

    def finish(self, code):
        self.active = False
        self.ended.emit(code)

    def stop(self):
        self.stops += 1
        if self.active:
            self.active = False
            QTimer.singleShot(0, lambda: self.ended.emit(1))

    def write(self, value):
        self.writes.append(value)


@pytest.fixture
def managed(qtbot, tmp_path):
    (tmp_path / "src/web").mkdir(parents=True)
    (tmp_path / "src/web/app.py").write_text("# synthetic server path", encoding="utf-8")
    (tmp_path / ".venv/Scripts").mkdir(parents=True)
    (tmp_path / ".venv/Scripts/python.exe").write_bytes(b"synthetic interpreter; never executed")
    created = []

    def factory():
        process = FakeProcess()
        created.append(process)
        return process

    manager = BackendManager(
        settings=MemorySettings(directory=str(tmp_path), login_username="fixture_login"),
        credentials=MemoryCredentials(("fixture_login", "fixture-password")),
        process_factory=factory,
    )
    manager.startup_timeout_ms = 1000
    yield manager, created
    manager.shutdown()
    qtbot.waitUntil(lambda: not manager.busy, timeout=3000)
    manager.deleteLater()


def fake_free_then_ready(manager):
    def probe(path="/api/about"):
        if not manager.server:
            manager._validate()
        else:
            manager.deadline.stop()
            manager._launching = False
            manager.set_state("ready", "synthetic ready")
            manager.ready.emit(TOOLS)

    manager._probe = probe


def test_owned_start_default_environment_no_duplicates_and_shutdown(managed, qtbot, monkeypatch):
    manager, created = managed
    monkeypatch.setenv("HIKERAPI_TOKEN", "UNRELATED_SECRET")
    fake_free_then_ready(manager)
    manager.ensure()
    for _ in range(5):
        manager.ensure()
    qtbot.waitUntil(lambda: manager.state == "ready")
    assert manager.owned and manager.backend == "instagrapi" and len(created) == 2
    python, args, cwd, environment, capture = created[-1].calls[0]
    assert python == manager.directory / ".venv/Scripts/python.exe" and cwd == manager.directory
    assert args[:3] == ["-m", "uvicorn", "src.web.app:app"]
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert "--reload" not in args and "fixture-password" not in str(args)
    assert environment.value("INSTAGRAM_PASSWORD") == "fixture-password"
    assert environment.value("INSTAGRAM_BACKEND") == "instagrapi" and not environment.contains(
        "HIKERAPI_TOKEN"
    )
    assert not capture
    manager.shutdown()
    qtbot.waitUntil(lambda: not manager.busy)
    assert created[-1].stops == 1


def test_window_close_waits_for_only_its_owned_backend(managed, qtbot, tmp_path):
    from username_finder.gui import MainWindow

    manager, created = managed
    window = MainWindow(tmp_path / "owned-close.db", osintgram_options={"manager": manager})
    qtbot.addWidget(window)
    window.timer.stop()
    window.show()
    fake_free_then_ready(manager)
    manager.ensure()
    qtbot.waitUntil(lambda: manager.state == "ready")
    window.close()
    qtbot.waitUntil(lambda: window.closed)
    assert created[-1].stops == 1 and not manager.busy


def test_existing_identity_is_reused_and_never_stopped(managed, qtbot):
    manager, created = managed
    backend = FakeBackend()
    manager._url = lambda path: QUrl(backend.url + path)
    try:
        manager.ensure()
        qtbot.waitUntil(lambda: manager.state == "ready")
        assert not created and not manager.owned and manager.backend == "unknown"
        assert [path for method, path, body in backend.calls] == ["/api/about", "/api/tools"]
        manager.shutdown()
        assert not created and backend.thread.is_alive()
    finally:
        backend.close()


@pytest.mark.parametrize("bad", ["foreign", "malformed", "redirect", "timeout"])
def test_occupied_or_unresponsive_port_is_never_taken_over(managed, qtbot, bad):
    manager, created = managed
    backend = FakeBackend()
    manager._url = lambda path: QUrl(backend.url + path)
    if bad == "foreign":
        backend.about = {"repository": "another service"}
    elif bad == "malformed":
        backend.tools_raw = b"bad JSON"
    elif bad == "redirect":
        backend.tools_status = 302
        backend.redirect = backend.url + "/do-not-follow"
    else:
        manager.probe_timeout_ms = 40
        backend.tools_delay = 0.15
    try:
        manager.ensure()
        qtbot.waitUntil(lambda: manager.state == "error")
        assert not created and not manager.owned
        assert len(backend.calls) == 1
    finally:
        backend.close()


def test_configuration_missing_and_no_credentials_are_explained(managed, qtbot):
    manager, created = managed
    manager.credentials.account = None
    manager._validate()
    assert manager.state == "credentials_missing" and not created
    manager.settings.setValue("directory", str(manager.directory / "missing"))
    manager._validate()
    assert manager.state == "error" and not created


@pytest.mark.parametrize("failure", ["dependencies", "spawn", "timeout"])
def test_startup_failures_and_deadline_do_not_leave_owned_workers(managed, qtbot, failure):
    manager, created = managed
    if failure == "dependencies":
        original = manager.factory

        def factory():
            process = original()
            process.dependencies = False
            return process

        manager.factory = factory
    elif failure == "spawn":
        original = manager.factory

        def factory():
            process = original()
            process.start_failure = True
            return process

        manager.factory = factory
    else:
        manager.startup_timeout_ms = 50
    manager._probe = lambda path="/api/about": None
    manager._validate()
    qtbot.waitUntil(lambda: manager.state == "error", timeout=2000)
    qtbot.waitUntil(lambda: not manager.busy)
    assert all(not process.active for process in created)


def test_auth_protocol_explicit_code_no_args_and_session_identity_setting(managed, qtbot):
    manager, created = managed
    manager.login()
    process = created[-1]
    assert process.active
    assert "fixture-password" not in str(process.calls[0][1])
    assert process.calls[0][3].value("INSTAGRAM_PASSWORD") == "fixture-password"
    kinds = []
    manager.auth_event.connect(kinds.append)
    process.output.emit(b'{"type":"two_factor"}\n')
    assert kinds == ["two_factor"]
    manager.submit_code("123456")
    assert json.loads(process.writes[-1]) == {"code": "123456"}
    process.output.emit(b'{"type":"authenticated"}\n')
    manager.restart_owned = lambda *args: None
    process.finish(0)
    assert manager.settings.value("session_account") == "fixture_login"
    assert "session_saved" in kinds
    assert "fixture-password" not in str(manager.settings.values)


@pytest.mark.parametrize("event", ["authentication_failed", "challenge_required"])
def test_auth_failure_preserves_ordinary_configuration(managed, qtbot, event):
    manager, created = managed
    manager.settings.setValue("session_account", "previous_account")
    manager.login()
    emitted = []
    manager.auth_event.connect(emitted.append)
    process = created[-1]
    process.output.emit((json.dumps({"type": event}) + "\n").encode())
    process.finish(1)
    assert manager.settings.value("session_account") == "previous_account"
    assert manager.credentials.account is not None
    assert emitted == [event]


def test_bad_auth_output_and_cancel_stop_only_the_owned_helper(managed, qtbot):
    manager, created = managed
    manager.login()
    events = []
    manager.auth_event.connect(events.append)
    created[-1].output.emit(b"untrusted output with a secret\n")
    qtbot.waitUntil(lambda: manager.auth is None)
    assert "untrusted" not in str(events) and created[-1].stops == 1


class FakeCredentialAPI:
    def __init__(self):
        self.saved = None
        self.freed = 0

    def CredWriteW(self, pointer, flags):
        value = ctypes.cast(pointer, ctypes.POINTER(Credential)).contents
        assert value.Type == 1 and value.Persist == 2 and value.TargetName == TARGET
        self.saved = value.UserName, ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize)
        return True

    def CredReadW(self, target, kind, flags, output):
        assert target == TARGET
        if self.saved is None:
            ctypes.set_last_error(1168)
            return False
        self.blob = (ctypes.c_ubyte * len(self.saved[1])).from_buffer_copy(self.saved[1])
        self.value = Credential(
            UserName=self.saved[0], CredentialBlobSize=len(self.blob), CredentialBlob=self.blob
        )
        ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(Credential)))[0] = ctypes.pointer(self.value)
        return True

    def CredDeleteW(self, target, kind, flags):
        assert target == TARGET
        self.saved = None
        return True

    def CredFree(self, pointer):
        self.freed += 1


def test_windows_credential_api_save_read_update_delete_using_double():
    api = FakeCredentialAPI()
    credentials = WindowsCredentials(api)
    assert credentials.load() is None
    credentials.save("fixture", "synthetic-password")
    assert credentials.load() == ("fixture", "synthetic-password")
    credentials.save("fixture", "updated-password")
    assert credentials.load() == ("fixture", "updated-password")
    credentials.delete()
    assert credentials.load() is None and api.saved is None and api.freed == 2
    api.CredWriteW = lambda *_: False
    with pytest.raises(CredentialError) as error:
        credentials.save("fixture", "DO_NOT_LEAK")
    assert "DO_NOT_LEAK" not in str(error.value)
    for invalid in ("\ud800", "bad\x00password"):
        with pytest.raises(CredentialError):
            credentials.save("fixture", invalid)


def test_account_setup_mask_save_remove_and_distinct_target(managed, qtbot, monkeypatch):
    manager, created = managed
    manager.restart_owned = lambda *args, **kwargs: None
    setup = AccountSetup(manager)
    qtbot.addWidget(setup)
    assert setup.password.echoMode() == QLineEdit.EchoMode.Password
    setup.username.setText("new_login")
    setup.password.setText("synthetic-new-password")
    setup.show_password.setChecked(True)
    assert setup.password.echoMode() == QLineEdit.EchoMode.Normal
    setup.save_account()
    assert setup.password.text() == "" and setup.password.echoMode() == QLineEdit.EchoMode.Password
    assert manager.credentials.account == ("new_login", "synthetic-new-password")
    assert "synthetic-new-password" not in str(manager.settings.values) + setup.status.text()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    setup.remove_account()
    assert manager.credentials.account is None and manager.settings.value("login_username") is None
    assert "لم تُحذف" in setup.status.text()


def test_gui_verification_code_is_only_sent_to_owned_helper(managed, qtbot, monkeypatch):
    manager, created = managed
    setup = AccountSetup(manager)
    qtbot.addWidget(setup)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("123456", True))
    manager.login()
    assert setup.cancel.isEnabled() and not setup.save.isEnabled()
    created[-1].output.emit(b'{"type":"challenge"}\n')
    assert json.loads(created[-1].writes[-1]) == {"code": "123456"}
    assert "123456" not in str(manager.settings.values)


class TwoFactor(Exception):
    pass


class FakeLogin:
    user_id = 123
    challenge_code_handler = None

    def __init__(self, failure=False, factor=False, mismatch=False):
        self.failure = failure
        self.factor = factor
        self.mismatch = mismatch
        self.calls = []

    def load_settings(self, path):
        self.loaded = path

    def login(self, username, password, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise ValueError("synthetic failure")
        if self.factor and "verification_code" not in kwargs:
            raise TwoFactor()
        if kwargs.get("relogin"):
            self.mismatch = False
        return True

    def account_info(self):
        return SimpleNamespace(username="another_account" if self.mismatch else "fixture")

    def get_settings(self):
        return {"synthetic_session": True}


@pytest.mark.parametrize(
    "mode", ["existing", "missing", "invalid", "two_factor", "different_account", "failure"]
)
def test_login_helper_preserves_existing_session_until_success(tmp_path, mode):
    session = tmp_path / "config/instagrapi_session.json"
    session.parent.mkdir()
    if mode != "missing":
        session.write_bytes(b'{"synthetic_previous": true}')
    client = FakeLogin(
        failure=mode == "failure", factor=mode == "two_factor", mismatch=mode == "different_account"
    )
    if mode == "invalid":
        client.load_settings = lambda _: (_ for _ in ()).throw(ValueError("synthetic invalid settings"))
    if mode == "failure":
        with pytest.raises(ValueError):
            authenticate(client, "fixture", "synthetic", session, lambda _: "123456", TwoFactor)
        assert session.read_bytes() == b'{"synthetic_previous": true}'
    else:
        authenticate(client, "fixture", "synthetic", session, lambda _: "123456", TwoFactor)
        assert json.loads(session.read_text()) == {"synthetic_session": True}
        if mode == "two_factor":
            assert client.calls[-1]["verification_code"] == "123456"
        if mode == "different_account":
            assert client.calls[-1]["relogin"] is True


def test_catalog_explains_all_known_commands_and_unknown_fallback():
    assert len(CATALOG) == 29
    for name, entry in CATALOG.items():
        assert entry.title and entry.purpose and entry.result and entry.limitation
        text = explanation({"name": name, "params": [], "needs_target": True}, "instagrapi")
        assert entry.purpose in text and "النتيجة" in text
        if entry.hiker_only:
            assert "غير مدعوم" in text and "HikerAPI" in text
    assert "original description" in explanation(
        {"name": "unknown_tool", "description": "original description"}, "unknown"
    )


def test_error_categories_do_not_assume_502_means_authentication():
    assert error_category("generic failure", 502) == "backend"
    assert error_category("login required", 502) == "authentication"
    assert error_category("challenge required", 502) == "challenge"
    assert error_category("HikerAPI only", 502) == "unsupported"
    assert error_category("private account", 403) == "access"
    assert "مصادقة" not in error_message("generic failure", 502)


def test_child_environment_does_not_mutate_parent_or_inherit_arbitrary_secrets(monkeypatch):
    monkeypatch.setenv("UNRELATED_SECRET", "never-inherit")
    env = child_environment(("fixture", "synthetic"))
    assert not env.contains("UNRELATED_SECRET")
    assert env.value("INSTAGRAM_PASSWORD") == "synthetic"
    import os

    assert os.environ["UNRELATED_SECRET"] == "never-inherit"


class ProbeReply(QObject):
    def __init__(self, error, status=None):
        super().__init__()
        self.network_error = error
        self.status = status

    def error(self):
        return self.network_error

    def attribute(self, attribute):
        assert attribute == QNetworkRequest.Attribute.HttpStatusCodeAttribute
        return self.status


def finish_probe(manager, error, status=None, data=b"", reason=()):
    reply = ProbeReply(error, status)
    manager.reply = reply
    manager._probe_done(reply, "/api/about", data, reason)


@pytest.mark.parametrize(
    ("error", "reason", "expected"),
    [
        (QNetworkReply.ConnectionRefusedError, (), "connection_refused"),
        (QNetworkReply.OperationCanceledError, ("timeout",), "timeout"),
        (QNetworkReply.TimeoutError, (), "timeout"),
        (QNetworkReply.RemoteHostClosedError, (), "connection_closed"),
        (QNetworkReply.UnknownNetworkError, (), "network_error"),
    ],
)
def test_network_failure_is_never_foreign_service(managed, monkeypatch, error, reason, expected):
    manager, created = managed
    monkeypatch.setattr(manager, "_port_free", lambda: False)
    messages = []
    manager.changed.connect(lambda state, message: messages.append(message))
    finish_probe(manager, error, reason=reason)
    assert manager.failure_code == expected and manager.state == "error"
    assert not created and not manager.owned
    assert all("8000" not in message for message in messages)


@pytest.mark.parametrize(
    "error", [QNetworkReply.ConnectionRefusedError, QNetworkReply.OperationCanceledError]
)
def test_free_port_after_refusal_or_probe_abort_starts_validation(managed, monkeypatch, error):
    manager, _ = managed
    validated = []
    monkeypatch.setattr(manager, "_port_free", lambda: True)
    monkeypatch.setattr(manager, "_validate", lambda: validated.append(True))
    finish_probe(manager, error, reason=("timeout",))
    assert validated == [True] and manager.state != "error"


def test_free_loopback_port_launches_without_waiting_for_http_timeout(managed, monkeypatch):
    manager, _ = managed
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    manager._url = lambda path: QUrl(f"http://127.0.0.1:{port}" + path)
    validated = []
    monkeypatch.setattr(manager, "_validate", lambda: validated.append(True))
    manager.ensure()
    assert validated == [True] and manager.reply is None


@pytest.mark.parametrize("status", [302, 401, 404, 500, 502, 503])
def test_http_failure_does_not_claim_foreign_port_or_expose_response(managed, status):
    manager, _ = managed
    messages = []
    manager.changed.connect(lambda state, message: messages.append(message))
    finish_probe(manager, QNetworkReply.NoError, status, b"password=SECRET; sessionid=SECRET")
    assert manager.failure_code == "http_error"
    assert str(status) in messages[-1] and "SECRET" not in str(messages)
    assert "SECRET" not in manager.probe_detail


@pytest.mark.parametrize("body", [b"not JSON", b"{}", b"[]"])
def test_missing_identity_is_contract_error_not_confirmed_foreign_service(managed, body):
    manager, _ = managed
    finish_probe(manager, QNetworkReply.NoError, 200, body)
    assert manager.failure_code == "api_contract"


@pytest.mark.parametrize("mode", ["timeout", "reset", "http503", "temporary"])
def test_transient_owned_readiness_failure_retries_within_deadline(managed, qtbot, mode):
    manager, _ = managed
    backend = FakeBackend()
    manager._url = lambda path: QUrl(backend.url + path)
    manager._launching = manager.owned = True
    manager.state = "starting"
    manager.deadline.start(1500)
    try:
        if mode == "http503":
            finish_probe(manager, QNetworkReply.ServiceUnavailableError, 503)
        elif mode == "reset":
            finish_probe(manager, QNetworkReply.RemoteHostClosedError, 200)
        elif mode == "temporary":
            finish_probe(manager, QNetworkReply.TemporaryNetworkFailureError)
        else:
            finish_probe(manager, QNetworkReply.OperationCanceledError, reason=("timeout",))
        assert manager.poll.isActive() and manager.deadline.isActive()
        assert manager.failure_code == "" and manager.state == "starting"
        qtbot.waitUntil(lambda: manager.state == "ready", timeout=2000)
        assert not manager.deadline.isActive()
    finally:
        backend.close()


def test_owned_exit_keeps_actual_category_and_retry_starts_new_owned_process(managed, qtbot):
    manager, created = managed
    manager._probe = lambda path="/api/about": None
    manager._validate()
    qtbot.waitUntil(lambda: manager.server is not None)
    process = manager.server
    process.diagnostic_code = "application_import"
    process.finish(1)
    assert manager.failure_code == "application_import" and manager.state == "error"
    manager._fail("cleanup must not replace the cause", "other")
    assert manager.failure_code == "application_import"
    fake_free_then_ready(manager)
    manager._port_free = lambda: True
    manager.ensure()
    qtbot.waitUntil(lambda: manager.state == "ready")
    assert len(created) == 4 and manager.failure_code == ""


def test_retry_after_foreign_service_failure_rediscovers_without_ownership(managed, qtbot):
    manager, created = managed
    backend = FakeBackend()
    manager._url = lambda path: QUrl(backend.url + path)
    backend.about = {"repository": "https://example.invalid/another-service"}
    try:
        manager.ensure()
        qtbot.waitUntil(lambda: manager.state == "error")
        assert manager.failure_code == "foreign_service"
        backend.about = {"repository": "https://github.com/Datalux/Osintgram"}
        manager.ensure()
        qtbot.waitUntil(lambda: manager.state == "ready")
        assert not manager.failure_code and not manager.owned and not created
    finally:
        backend.close()


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("ModuleNotFoundError: SECRET", "python_dependency"),
        ("Error loading ASGI app. SECRET", "application_import"),
        ("Application startup failed. SECRET", "application_startup"),
        ("[WinError 10048] SECRET", "port_in_use"),
        ("PermissionError: SECRET", "permission"),
        ("password=SECRET; cookies=SECRET", ""),
    ],
)
def test_owned_process_stderr_is_classified_without_forwarding_secrets(qtbot, tmp_path, stderr, expected):
    process = OwnedProcess()
    output, ended = [], []
    process.output.connect(output.append)
    process.ended.connect(ended.append)
    try:
        process.start(
            sys.executable,
            ["-c", f"import sys; sys.stderr.write({stderr!r}); sys.exit(7)"],
            tmp_path,
            child_environment(),
        )
        qtbot.waitUntil(lambda: bool(ended))
        assert (ended, process.diagnostic_code) == ([7], expected)
        assert not output and process._stderr_tail == b"" and process.job is None
    finally:
        process.stop()


def test_windows_job_failure_kills_only_owned_process_and_clears_environment(qtbot, tmp_path, monkeypatch):
    import username_finder.osintgram_process as module

    if module.os.name != "nt":
        pytest.skip("Windows Job Object lifecycle")

    def fail_job(pid):
        raise OSError("SECRET must not be exposed")

    monkeypatch.setattr(module, "WindowsJob", fail_job)
    process = OwnedProcess()
    failed, started, ended = [], [], []
    process.failed.connect(lambda: failed.append(True))
    process.started.connect(lambda: started.append(True))
    process.ended.connect(ended.append)
    try:
        process.start(
            sys.executable,
            ["-c", "import time; time.sleep(30)"],
            tmp_path,
            child_environment(("fixture", "SECRET")),
        )
        qtbot.waitUntil(lambda: bool(ended))
        assert failed == [True] and not started and not process.active
        assert process.diagnostic_code == "windows_job"
        assert not process.process.processEnvironment().contains("INSTAGRAM_PASSWORD")
    finally:
        process.stop()


@pytest.mark.parametrize("code", [0, 9])
def test_rapid_owned_interpreter_exit_preserves_exit_code(qtbot, tmp_path, code):
    process = OwnedProcess()
    ended, failed = [], []
    process.ended.connect(ended.append)
    process.failed.connect(lambda: failed.append(True))
    try:
        process.start(sys.executable, ["-c", f"raise SystemExit({code})"], tmp_path, child_environment())
        qtbot.waitUntil(lambda: bool(ended))
        assert ended == [code] and not failed
        assert not process.active and process.job is None
    finally:
        process.stop()


def test_invalid_working_directory_is_safely_classified(qtbot, tmp_path):
    process = OwnedProcess()
    failed = []
    process.failed.connect(lambda: failed.append(True))
    process.start(sys.executable, ["-c", "pass"], tmp_path / "missing", child_environment())
    qtbot.waitUntil(lambda: bool(failed))
    assert process.diagnostic_code == "working_directory" and not process.active


def test_exit_during_job_assignment_is_not_reported_as_containment_failure(qtbot, tmp_path, monkeypatch):
    import username_finder.osintgram_process as module

    if module.os.name != "nt":
        pytest.skip("Windows Job Object startup race")
    process = OwnedProcess()
    ended, failed = [], []
    process.ended.connect(ended.append)
    process.failed.connect(lambda: failed.append(True))

    def exited_before_assignment(pid):
        # Force the signal-ordering race deterministically in this test only.
        assert process.process.waitForFinished(2000)
        raise ProcessLookupError("owned interpreter has already exited")

    monkeypatch.setattr(module, "WindowsJob", exited_before_assignment)
    process.start(
        sys.executable,
        ["-c", "raise SystemExit(12)"],
        tmp_path,
        child_environment(("fixture", "SECRET")),
    )
    qtbot.waitUntil(lambda: bool(ended))
    assert ended == [12] and not failed and not process.diagnostic_code
    assert not process.active and not process.process.processEnvironment().contains("INSTAGRAM_PASSWORD")


@pytest.mark.parametrize("failure", ["readiness", "exit"])
def test_shutdown_and_late_process_signals_preserve_first_failure(managed, qtbot, failure):
    manager, created = managed
    manager._probe = lambda path="/api/about": None
    manager._validate()
    qtbot.waitUntil(lambda: manager.server is not None)
    server = manager.server
    if failure == "readiness":
        manager._startup_timeout()
        expected = "startup_timeout"
    else:
        server.finish(13)
        expected = "process_exit"
    manager._server_started()
    assert not manager.poll.isActive() and manager.failure_code == expected
    qtbot.waitUntil(lambda: not manager.busy)
    manager.shutdown()
    assert manager.failure_code == expected and all(not p.active for p in created)


def test_unexpected_network_error_during_owned_start_never_launches_another_server(managed, qtbot):
    manager, created = managed
    manager._launching = manager.owned = True
    manager.state = "starting"
    manager._port_free = lambda: True
    finish_probe(manager, QNetworkReply.UnknownNetworkError)
    assert manager.failure_code == "network_error" and manager.state == "error"
    assert not created and not manager.worker and not manager.poll.isActive()


@pytest.mark.parametrize("stage", ["vanished", "exited", "assign_race", "denied"])
def test_job_snapshot_child_exit_is_not_a_containment_failure(monkeypatch, stage):
    closed, assignments = [], []
    job = WindowsJob.__new__(WindowsJob)
    job.handle = 42
    checks = []

    def get_exit_code(handle, pointer):
        checks.append(True)
        pointer._obj.value = 259 if stage == "assign_race" and len(checks) == 1 else 0
        return True

    def assign(handle, process):
        assignments.append(process)
        return False

    def contained(process, handle, pointer):
        pointer._obj.value = False
        return True

    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5 if stage == "denied" else 87)
    job.kernel = SimpleNamespace(
        OpenProcess=lambda *args: 0 if stage in ("vanished", "denied") else 123,
        GetExitCodeProcess=get_exit_code,
        IsProcessInJob=contained,
        AssignProcessToJobObject=assign,
        CloseHandle=closed.append,
    )
    if stage == "denied":
        with pytest.raises(OSError):
            job._assign(99, allow_exited=True)
    else:
        job._assign(99, allow_exited=True)
        assert assignments == ([123] if stage == "assign_race" else [])
        assert closed == ([123] if stage != "vanished" else [])
