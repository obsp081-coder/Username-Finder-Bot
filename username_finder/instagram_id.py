"""Single-account ID resolution through the existing managed Osintgram service."""

from __future__ import annotations

import re

from PySide6.QtCore import QObject, QTimer, Signal

from .instagram_ui import normalize_username, syntax_error
from .osintgram_api import ContractError, LocalOsintgramClient, build_payload, error_message


def account_identifier(result, username):
    """Accept only an exact username and an exact positive decimal ID, never floats."""
    if not isinstance(result, dict):
        raise ValueError("استجابة الحساب غير متوافقة.")
    name = result.get("username")
    if not isinstance(name, str) or name.lower() != username:
        raise ValueError("اسم الحساب في الاستجابة لا يطابق الاسم المطلوب؛ لم يُستخدم المعرّف.")
    value = result.get("id")
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,127}", value):
        raise ValueError("لم تُعد الخدمة معرّف حساب عشريًا موجبًا صالحًا؛ لا تُقبل الأرقام التقريبية.")
    return value


class InstagramIdResolver(QObject):
    """Own only an async transport; reuse the sole backend manager and its session.

    Reserve the existing analysis UI while a conversion drains, without altering
    its target, commands, results, authentication or managed process ownership.
    """

    resolved = Signal(str, str)
    failed = Signal(str)
    status = Signal(str)
    idle = Signal()

    def __init__(self, analysis, parent=None):
        super().__init__(parent)
        self.analysis = analysis
        self.manager = analysis.manager
        self.client = LocalOsintgramClient(self)
        self.client.run_timeout_ms = 20000
        self.client.finished.connect(self._finished)
        self.client.event_received.connect(self._event)
        self.client.idle.connect(self._idle)
        self.manager.ready.connect(self._ready)
        self.manager.changed.connect(self._changed)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.setInterval(45000)
        self.deadline.timeout.connect(self._timeout)
        self.active = self.waiting = False
        self.username = ""
        self.identifier = self.problem = ""

    @property
    def busy(self):
        return self.active or self.client.busy

    def start(self, text):
        if self.busy or self.client.closing:
            return False
        username = normalize_username(text)
        problem = syntax_error(username)
        if problem:
            self.failed.emit(problem)
            return False
        if self.analysis.running or self.analysis.client.busy or self.manager.busy:
            self.failed.emit("الخدمة مشغولة بالتحليل أو إعداد الحساب؛ انتظر انتهاء العملية ثم أعد المحاولة.")
            return False
        self.active = self.waiting = True
        self.username = username
        self.identifier = self.problem = ""
        self.analysis.id_resolution_busy = True
        self.analysis._refresh_actions()
        self.deadline.start()
        self.status.emit("جارٍ تحويل اسم المستخدم إلى معرّف Instagram…")
        if self.manager.state == "ready":
            self._ready(self.analysis.tools)
        else:
            self.manager.ensure()
        return True

    def _ready(self, tools):
        if not self.active or not self.waiting:
            return
        if not self.analysis._session_usable():
            self._fail("تحقق من جلسة حساب الدخول في صفحة Instagram «الحساب والاتصال» أولًا.")
            return
        try:
            payload = build_payload(tools, {"get_user_info": {}}, self.username, 1)
            self.client.connected = True
            self.waiting = False
            self.client.run(payload)
        except (ContractError, ValueError):
            self._fail("أمر get_user_info غير متاح أو غير متوافق في الخدمة الحالية.")

    def _changed(self, state, _message):
        if self.active and state in ("error", "credentials_missing", "unconfigured", "stopping"):
            self._fail("تعذر تجهيز خدمة Osintgram؛ راجع إعداد الخدمة والجلسة في صفحة Instagram.")

    def _event(self, event):
        if not self.active:
            return
        if event["type"] == "error":
            self.problem = error_message(event.get("detail", ""))
        elif event["type"] == "tool_result":
            result = event["result"]
            if event["is_error"] or (isinstance(result, dict) and "error" in result):
                self.problem = error_message(result.get("error", "") if isinstance(result, dict) else "")
            else:
                try:
                    self.identifier = account_identifier(result, self.username)
                except ValueError as exc:
                    self.problem = str(exc)

    def _finished(self, outcome, message):
        if not self.active:
            return
        if outcome != "complete" or self.problem or not self.identifier:
            self._fail(self.problem or message or "لم تصل استجابة مكتملة تتضمن معرّف الحساب.")
            return
        username, identifier = self.username, self.identifier
        self.active = self.waiting = False
        self.deadline.stop()
        self.resolved.emit(username, identifier)
        self._idle()

    def _fail(self, message):
        self.cancel()
        self.failed.emit(message + "\nتعذر الحصول على معرّف الحساب. لم يبدأ البحث داخل الملفات.")

    def _timeout(self):
        self._fail("انتهت مهلة التحويل؛ لم تُعد المحاولة تلقائيًا.")

    def cancel(self):
        self.active = self.waiting = False
        self.deadline.stop()
        if self.client.run_id:
            self.client.cancel()
        for reply, context in list(self.client.requests.items()):
            if context["kind"] != "cancel":
                self.client._abort(reply, "أُلغي تحويل اسم المستخدم.")
        self._idle()

    def _idle(self):
        if not self.busy:
            self.analysis.id_resolution_busy = False
            self.analysis._refresh_actions()
            self.idle.emit()

    def shutdown(self):
        self.cancel()
        self.client.shutdown()
