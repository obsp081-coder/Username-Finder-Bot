"""Local identity probe, validated external startup and explicit session setup."""

from __future__ import annotations

import json
import socket
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkReply, QNetworkRequest

from .osintgram_api import BASE_URL, validate_tools
from .osintgram_credentials import CredentialError, WindowsCredentials
from .osintgram_process import OwnedProcess, child_environment

DEFAULT_DIRECTORY = Path.home() / "Desktop" / "Osintgram-Lab"
CHECK_DEPENDENCIES = (
    "import importlib.util,json; "
    "print(json.dumps({'ready':all(importlib.util.find_spec(n) is not None "
    "for n in ('uvicorn','fastapi','httpx','hikerapi','geopy','instagrapi','pydantic'))}))"
)


class BackendManager(QObject):
    changed = Signal(str, str)
    ready = Signal(object)
    idle = Signal()
    auth_event = Signal(str)

    def __init__(self, parent=None, settings=None, credentials=None, process_factory=None):
        super().__init__(parent)
        self.settings = settings if settings is not None else QSettings("UsernameFinderBot", "Osintgram")
        self.credentials = credentials or WindowsCredentials()
        self.factory = process_factory or (lambda: OwnedProcess(self))
        self.network = QNetworkAccessManager(self)
        self.network.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self.reply = None
        self.worker = None
        self.server = None
        self.auth = None
        self.owned = False
        self.closing = False
        self.state = "unconfigured"
        self.backend = "unknown"
        self._buffer = bytearray()
        self._restart = False
        self._probe_path = ""
        self._launching = False
        self.failure_code = ""
        self.probe_detail = ""
        self._identified = False
        self.startup_timeout_ms = 20000
        self.probe_timeout_ms = 4000
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(self._startup_timeout)
        self.poll = QTimer(self)
        self.poll.setSingleShot(True)
        self.poll.timeout.connect(self._probe)
        self.auth_deadline = QTimer(self)
        self.auth_deadline.setSingleShot(True)
        self.auth_deadline.timeout.connect(self.cancel_login)

    @property
    def directory(self):
        return Path(self.settings.value("directory", str(DEFAULT_DIRECTORY)))

    @property
    def session_exists(self):
        return (self.directory / "config/instagrapi_session.json").is_file()

    @property
    def busy(self):
        return bool(
            self.reply
            or self._launching
            or any(p and p.active for p in (self.worker, self.auth))
            or ((self.closing or self.state in ("error", "stopping")) and self.server and self.server.active)
        )

    def set_state(self, state, message):
        self.state = state
        self.changed.emit(state, message)

    def ensure(self):
        if self.closing or self.busy or self.state == "ready":
            return
        self.failure_code = ""
        self.probe_detail = ""
        self._identified = False
        self._restart = False
        self.set_state("checking", "نتحقق من برنامج التحليل المحلي…")
        if self._port_free():
            self._validate()
        else:
            self._probe()

    def _port_free(self):
        """Test the configured loopback bind without taking over a listening socket."""
        url = self._url("/api/about")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind((url.host(), url.port(80)))
            return True
        except OSError:
            return False

    def _url(self, path):
        return QUrl(BASE_URL + path)

    def _probe(self, path="/api/about"):
        if self.closing or self.reply or self.state == "error":
            return
        if self._launching and self._port_free():
            self.poll.start(300)
            return
        request = QNetworkRequest(self._url(path))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        request.setAttribute(
            QNetworkRequest.Attribute.CookieLoadControlAttribute, QNetworkRequest.LoadControl.Manual
        )
        request.setAttribute(
            QNetworkRequest.Attribute.CookieSaveControlAttribute, QNetworkRequest.LoadControl.Manual
        )
        self._probe_path = path
        reply = self.network.get(request)
        self.reply = reply
        reply.setReadBufferSize(262145)
        data = bytearray()
        abort_reason = []

        def read():
            data.extend(bytes(reply.readAll()))
            if len(data) > 262144:
                abort_reason.append("oversized")
                reply.abort()

        timer = QTimer(reply)
        timer.setSingleShot(True)

        def timeout():
            abort_reason.append("timeout")
            reply.abort()

        timer.timeout.connect(timeout)
        timer.start(self.probe_timeout_ms)
        reply.readyRead.connect(read)
        reply.finished.connect(lambda: self._probe_done(reply, path, bytes(data), abort_reason))

    def _probe_done(self, reply, path, data, abort_reason=()):
        if reply is not self.reply:
            reply.deleteLater()
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error = reply.error()
        self.reply = None
        reply.deleteLater()
        if self.closing or self.state == "error":
            self.idle.emit()
            return
        self.probe_detail = f"{path}: {error.name}; HTTP {status or '-'}"
        if "oversized" in abort_reason:
            self._fail("استجابة خدمة التحليل أكبر من الحد المسموح؛ تحقق من توافق API.", "response_size")
            return
        connection_errors = (
            QNetworkReply.NetworkError.ConnectionRefusedError,
            QNetworkReply.NetworkError.RemoteHostClosedError,
            QNetworkReply.NetworkError.TimeoutError,
            QNetworkReply.NetworkError.OperationCanceledError,
            QNetworkReply.NetworkError.TemporaryNetworkFailureError,
        )
        if error != QNetworkReply.NetworkError.NoError and (status is None or error in connection_errors):
            kind = (
                "timeout"
                if "timeout" in abort_reason or error == QNetworkReply.NetworkError.TimeoutError
                else "connection_refused"
                if error == QNetworkReply.NetworkError.ConnectionRefusedError
                else "connection_closed"
                if error == QNetworkReply.NetworkError.RemoteHostClosedError
                else "network_error"
            )
            if self._launching and (
                kind in ("timeout", "connection_refused", "connection_closed")
                or error == QNetworkReply.NetworkError.TemporaryNetworkFailureError
            ):
                self.poll.start(300)
            elif not self._launching and not self._identified and self._port_free():
                self._validate()
            else:
                messages = {
                    "timeout": "انتهت مهلة الاتصال بخدمة التحليل؛ لم نتأكد من جاهزيتها. أعد المحاولة.",
                    "connection_refused": "رفضت خدمة التحليل الاتصال. تحقق من تشغيلها ثم أعد المحاولة.",
                    "connection_closed": "أغلقت خدمة التحليل الاتصال قبل اكتمال الاستجابة. أعد المحاولة.",
                }
                self._fail(
                    messages.get(kind, "تعذر الاتصال بمحرك Osintgram بسبب خطأ في الشبكة المحلية."), kind
                )
            return
        if status != 200 or error != QNetworkReply.NetworkError.NoError:
            if self._launching and status in (500, 502, 503, 504):
                self.poll.start(300)
            else:
                self._fail(
                    f"أعادت خدمة التحليل استجابة HTTP غير متوقعة ({status or 'غير معروف'}). "
                    "تحقق من توافق API.",
                    "http_error",
                )
            return
        try:
            value = json.loads(data)
            if path == "/api/about":
                if not isinstance(value, dict) or not isinstance(value.get("repository"), str):
                    raise ValueError
                if value["repository"] != "https://github.com/Datalux/Osintgram":
                    if self.owned:
                        self._fail(
                            "استجابت خدمة بهوية غير متوقعة أثناء التشغيل؛ تحقق من المنفذ 8000.", "identity"
                        )
                    else:
                        self._fail("المنفذ 8000 مستخدم بواسطة خدمة بهوية أخرى؛ لن نوقفها.", "foreign_service")
                    return
                self._identified = True
                self._probe("/api/tools")
                return
            tools = validate_tools(value)
        except (ValueError, TypeError, RecursionError, UnicodeError):
            self._fail(
                "استجابة خدمة التحليل لا تطابق عقد Osintgram المتوقع. تحقق من إصدار API.", "api_contract"
            )
            return
        self.deadline.stop()
        self._launching = False
        if not self.owned:
            self.backend = "unknown"
        self.set_state("ready", "المحرك متصل؛ صلاحية جلسة Instagram لم تُختبر بعد.")
        self.ready.emit(tools)

    def _validate(self):
        directory = self.directory
        python = directory / ".venv/Scripts/python.exe"
        if not directory.is_dir() or not (directory / "src/web/app.py").is_file():
            self._fail("اختر مجلد Osintgram الصحيح؛ ملف خادم التحليل غير موجود.")
            return
        if not python.is_file():
            self._fail("بيئة Python الخاصة بـOsintgram غير موجودة؛ لن نثبّت حزمًا تلقائيًا.")
            return
        try:
            account = self.credentials.load()
        except CredentialError:
            self._fail(str(CredentialError()))
            return
        if not account:
            self.set_state("credentials_missing", "احفظ حساب الدخول أولًا لبدء المحرك تلقائيًا.")
            self.idle.emit()
            return
        self.set_state("validating", "نتحقق من بيئة Python والحزم المطلوبة…")
        self.worker = self.factory()
        self._buffer.clear()
        self.worker.output.connect(self._collect)
        self.worker.failed.connect(lambda: self._process_failed(self.worker))
        self.worker.ended.connect(self._validated)
        self.deadline.start(self.startup_timeout_ms)
        self.worker.start(
            python, ["-I", "-c", CHECK_DEPENDENCIES], directory, child_environment(), capture=True
        )

    def _collect(self, chunk):
        self._buffer.extend(chunk[:4096])
        if len(self._buffer) > 4096:
            if self.worker:
                self.worker.stop()
            self._fail("استجابة غير متوافقة من بيئة Python.")

    def _validated(self, code):
        diagnostic = getattr(self.worker, "diagnostic_code", "")
        self.worker = None
        if self.closing or self.state == "error":
            self.idle.emit()
            return
        try:
            if code != 0 or json.loads(self._buffer).get("ready") is not True:
                raise ValueError
        except (ValueError, AttributeError):
            self._fail(
                "بعض حزم Osintgram مفقودة أو غير متوافقة. أصلح بيئته الموجودة؛ لم تُثبّت أي حزمة.",
                diagnostic or "python_dependency",
            )
            return
        try:
            account = self.credentials.load()
            if not account:
                raise CredentialError()
        except CredentialError:
            self._fail(str(CredentialError()))
            return
        self.server = self.factory()
        self.owned = True
        self.backend = "instagrapi"
        self._launching = True
        self.set_state("starting", "جارٍ بدء Osintgram؛ انتظر قليلًا…")
        self.server.started.connect(self._server_started)
        self.server.failed.connect(lambda: self._process_failed(self.server))
        self.server.ended.connect(self._server_ended)
        self.server.start(
            self.directory / ".venv/Scripts/python.exe",
            [
                "-m",
                "uvicorn",
                "src.web.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--no-access-log",
                "--log-level",
                "error",
            ],
            self.directory,
            child_environment(account),
        )

    def _server_started(self):
        if self._launching and not self.closing and self.state == "starting":
            self.poll.start(0)

    def _process_failed(self, process):
        category = getattr(process, "diagnostic_code", "") or "process_start"
        messages = {
            "windows_job": (
                "تعذر احتواء عملية Osintgram داخل Windows Job Object؛ أُوقف التشغيل لحماية ملكية العمليات."
            ),
            "working_directory": "مجلد تشغيل Osintgram غير متاح. أعد اختيار مجلد التثبيت.",
            "port_in_use": "تعذر ربط Osintgram بالمنفذ 8000 لأنه مستخدم؛ لن نوقف العملية الأخرى.",
            "permission": "رفض Windows صلاحية التشغيل أو الربط بالمنفذ. تحقق من أذونات البيئة المحلية.",
            "python_dependency": "تعذر بدء المحرك بسبب مشكلة في Python أو المكتبات المطلوبة.",
            "application_import": "تعذر استيراد تطبيق Osintgram. تحقق من مجلده وتوافق مكتباته.",
            "application_startup": "فشلت تهيئة تطبيق Osintgram أثناء بدء التشغيل. تحقق من بيئته الخارجية.",
        }
        self._fail(
            messages.get(category, "تعذر تشغيل محرك Osintgram؛ تحقق من Python وأذونات المجلد."), category
        )

    def _server_ended(self, code):
        process = self.server
        self.server = None
        self.owned = False
        if self.closing:
            self.idle.emit()
        elif self._restart:
            self._restart = False
            self.state = "unconfigured"
            QTimer.singleShot(0, self.ensure)
        elif self.state == "stopping":
            self.set_state("unconfigured", "أُوقف المحرك الذي شغّله التطبيق؛ الجلسة الخارجية محفوظة.")
            self.idle.emit()
        elif self.state != "error":
            if getattr(process, "diagnostic_code", ""):
                self._process_failed(process)
            else:
                self._fail(
                    f"توقفت خدمة Osintgram بصورة غير متوقعة (رمز الخروج {code}). "
                    "تحقق من بيئة التشغيل ثم أعد المحاولة.",
                    "process_exit",
                )
        else:
            self.idle.emit()

    def _startup_timeout(self):
        self._fail(
            "لم يصبح محرك التحليل جاهزًا خلال المهلة المحددة. أعد المحاولة بعد التحقق من بيئته.",
            "startup_timeout",
        )

    def _fail(self, message, code="configuration"):
        if self.closing or self.state == "error":
            return
        self.deadline.stop()
        self.poll.stop()
        self._launching = False
        self.failure_code = code
        self.set_state("error", message)
        if self.reply:
            self.reply.abort()
        if self.server:
            self.server.stop()
        if self.worker:
            self.worker.stop()
        self.idle.emit()

    def restart_owned(self, restart=True):
        if self.server:
            self._restart = restart
            self.set_state("stopping", "نوقف المحرك المملوك للتطبيق لتطبيق إعداد الحساب…")
            self.server.stop()
        elif restart:
            self.state = "unconfigured"
            self.ensure()

    def login(self):
        if self.busy or self.closing:
            return
        try:
            account = self.credentials.load()
            if not account:
                raise CredentialError()
        except CredentialError:
            self.auth_event.emit("credentials_missing")
            return
        python = self.directory / ".venv/Scripts/python.exe"
        if not python.is_file():
            self.auth_event.emit("configuration_missing")
            return
        self.auth = self.factory()
        self._auth_buffer = bytearray()
        self._auth_success = False
        self._auth_terminal = False
        self.auth.output.connect(self._auth_read)
        self.auth.failed.connect(self._auth_failed)
        self.auth.ended.connect(self._auth_done)
        self.auth_deadline.start(180000)
        self.auth.start(
            python,
            ["-I", str(Path(__file__).with_name("osintgram_login.py"))],
            self.directory,
            child_environment(account),
            capture=True,
        )
        self.auth_event.emit("authenticating")

    def _auth_read(self, chunk):
        self._auth_buffer.extend(chunk)
        if len(self._auth_buffer) > 4096:
            self.cancel_login()
            return
        while b"\n" in self._auth_buffer:
            line, _, rest = self._auth_buffer.partition(b"\n")
            self._auth_buffer = bytearray(rest)
            try:
                kind = json.loads(line)["type"]
                if kind not in (
                    "authenticated",
                    "challenge",
                    "two_factor",
                    "challenge_required",
                    "authentication_failed",
                ):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                self.cancel_login()
                return
            self._auth_success |= kind == "authenticated"
            self._auth_terminal |= kind in ("challenge_required", "authentication_failed")
            self.auth_event.emit(kind)

    def _auth_failed(self):
        self._auth_terminal = True
        self.auth_event.emit("authentication_failed")
        if self.auth and not self.auth.active:
            self.auth_deadline.stop()
            self.auth = None
            self.idle.emit()

    def submit_code(self, code):
        if self.auth and self.auth.active:
            self.auth.write((json.dumps({"code": code}) + "\n").encode())

    def cancel_login(self):
        self._auth_terminal = True
        if self.auth:
            self.auth.stop()
        self.auth_event.emit("authentication_cancelled")

    def _auth_done(self, code):
        self.auth_deadline.stop()
        self.auth = None
        if self.closing:
            self.idle.emit()
            return
        if code == 0 and self._auth_success:
            self.settings.setValue("session_account", self.settings.value("login_username", ""))
            self.settings.sync()
            self.auth_event.emit("session_saved")
            self.restart_owned()
        elif code != 0 and not self._auth_terminal:
            self.auth_event.emit("authentication_failed")
        self.idle.emit()

    def shutdown(self):
        self.closing = True
        self.poll.stop()
        self.deadline.stop()
        self.auth_deadline.stop()
        self._launching = False
        if self.reply:
            self.reply.abort()
        for process in (self.auth, self.worker, self.server):
            if process:
                process.stop()
