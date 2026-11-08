"""Local Osintgram Base API contract and bounded, asynchronous Qt transport.

No credentials, availability provider, persistent storage or process management.
"""

from __future__ import annotations

import json
import math
import re
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkReply, QNetworkRequest

BASE_URL = "http://127.0.0.1:8000"
MAX_RESPONSE = 8 * 1024 * 1024
MAX_EVENT = 2 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,79}\Z")


class ContractError(ValueError):
    pass


def _invalid_constant(value):
    raise ContractError("تحتوي الاستجابة على رقم JSON غير صالح.")


def _decode(value):
    def finite_float(text):
        number = float(text)
        if not math.isfinite(number):
            _invalid_constant(text)
        return number

    return json.loads(value, parse_constant=_invalid_constant, parse_float=finite_float)


def error_category(detail="", status=None):
    text = str(detail).lower()
    if any(word in text for word in ("captcha", "challenge", "checkpoint")):
        return "challenge"
    if status == 401 or any(word in text for word in ("login", "session", "auth", "credential")):
        return "authentication"
    if "hikerapi only" in text or "unsupported" in text or "not available on" in text:
        return "unsupported"
    if status == 403 or any(word in text for word in ("private", "permission", "access denied")):
        return "access"
    if status == 429 or any(word in text for word in ("rate limit", "too many", "please wait")):
        return "rate_limit"
    return "backend"


def error_message(detail="", status=None):
    """Classify diagnostics without echoing backend exceptions or session secrets."""
    category = error_category(detail, status)
    if category == "challenge":
        message = "يطلب Instagram تأكيد تسجيل الدخول؛ أكمل التحقق دون تجاوز قيوده."
    elif category == "authentication":
        message = "مشكلة مصادقة أو جلسة؛ عالجها في Osintgram ثم أعد الاتصال."
    elif category == "unsupported":
        message = "هذا الأمر غير مدعوم في محرك البيانات الحالي؛ راجع متطلبات HikerAPI."
    elif category == "access":
        message = "الوصول إلى البيانات مقيّد؛ تحقق من صلاحيات حساب الدخول وخصوصية الحساب."
    elif category == "rate_limit":
        message = "وصلت الخدمة إلى حد الطلبات؛ انتظر دون إعادة محاولة تلقائية."
    else:
        message = {
            400: "رفضت الخدمة الإعدادات؛ تحقق من الهدف والأوامر والمعاملات.",
            404: "لم تعثر الخدمة على الهدف أو المسار؛ لا يدل ذلك على إتاحة الاسم.",
            422: "معاملات غير متوافقة مع واجهة الخدمة المحلية.",
            502: "فشل محرك Osintgram؛ راجع حالته محليًا. لا توجد نتيجة إتاحة.",
        }.get(status, "تعذر إكمال العملية في الخدمة المحلية؛ راجع Osintgram.")
    return f"HTTP {status} — {message}" if status else message


def validate_tools(value):
    if not isinstance(value, list) or not value or len(value) > 256:
        raise ContractError("استجابة اكتشاف الأوامر فارغة أو غير متوافقة.")
    names = set()
    for tool in value:
        if (
            not isinstance(tool, dict)
            or not isinstance(tool.get("name"), str)
            or not IDENTIFIER.fullmatch(tool["name"])
            or tool["name"] in names
            or not isinstance(tool.get("needs_target"), bool)
            or not isinstance(tool.get("params"), list)
            or len(tool["params"]) > 64
        ):
            raise ContractError("مواصفات أحد الأوامر غير متوافقة؛ حدّث الخدمة أو واجهة الدمج.")
        names.add(tool["name"])
        params = set()
        for param in tool["params"]:
            if (
                not isinstance(param, dict)
                or not isinstance(param.get("name"), str)
                or not IDENTIFIER.fullmatch(param["name"])
                or param["name"] in params
                or param.get("type") not in ("integer", "number", "string", "boolean", "object", "array")
            ):
                raise ContractError("مواصفات معامل غير مدعومة؛ لم يُحذف أي أمر بصمت.")
            choices = param.get("choices")
            if choices is not None and (
                not isinstance(choices, list)
                or not choices
                or len(choices) > 100
                or any(isinstance(item, (dict, list)) for item in choices)
            ):
                raise ContractError("قائمة خيارات المعامل غير متوافقة.")
            params.add(param["name"])
    return value


def parameter_value(spec, text):
    """Validate an editor value; blank optional values use the advertised default."""
    name = spec["name"]
    if not text.strip():
        default = spec.get("default")
        if spec.get("required") or (spec["type"] == "string" and default == ""):
            raise ContractError(f"المعامل {name} مطلوب.")
        if default is None:
            return None
        text = default if isinstance(default, str) else json.dumps(default)
    try:
        kind = spec["type"]
        if kind == "string":
            value = text.strip()
        elif kind == "integer":
            if not re.fullmatch(r"[0-9]+", text.strip()):
                raise ValueError
            value = int(text)
            if not 1 <= value <= 100000:
                raise ValueError
        else:
            value = _decode(text)
            expected = {"number": (int, float), "boolean": bool, "object": dict, "array": list}[kind]
            if not isinstance(value, expected) or (kind == "number" and isinstance(value, bool)):
                raise ValueError
        if "choices" in spec and value not in spec["choices"]:
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ContractError(f"قيمة المعامل {name} غير صالحة ({spec['type']}).") from exc


def build_payload(tools, selected, target, limit):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ContractError("اختر حد طلبات من 1 إلى 1000؛ التنفيذ غير المحدود غير متاح.")
    if not selected:
        raise ContractError("اختر أمرًا واحدًا على الأقل.")
    specs = {tool["name"]: tool for tool in tools}
    calls = []
    needs_target = False
    for name, values in selected.items():
        if name not in specs:
            raise ContractError("الأمر غير موجود في الاكتشاف الحالي؛ أعد الاتصال.")
        spec = specs[name]
        needs_target |= spec["needs_target"]
        if set(values) - {p["name"] for p in spec["params"]}:
            raise ContractError("توجد معاملات غير معلنة للأمر.")
        args = {}
        for param in spec["params"]:
            value = parameter_value(param, values.get(param["name"], ""))
            if value is not None:
                args[param["name"]] = value
        calls.append({"name": name, "args": args})
    target = target.strip().removeprefix("@").lower()
    if (needs_target and not target) or (
        target and (len(target) > 30 or not re.fullmatch(r"[a-z0-9._]+", target))
    ):
        raise ContractError("أدخل اسم حساب Instagram صالحًا للأوامر التي تتطلب هدفًا.")
    return {
        "target": target or None,
        "calls": calls,
        "max_items": limit,
        "run_id": uuid4().hex,
        "verbose": True,
    }


class LocalOsintgramClient(QObject):
    """Qt performs socket I/O asynchronously; no blocking calls or worker shutdown waits.

    SSE is consumed incrementally with byte/time bounds. Only three literal API
    paths are supported, redirects and proxies are disabled, and nothing is retried.
    """

    tools_ready = Signal(object)
    connection_error = Signal(str)
    event_received = Signal(object)
    finished = Signal(str, str)
    cancellation = Signal(bool, str)
    idle = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.manager = QNetworkAccessManager(self)
        self.manager.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self.requests = {}
        self.run_id = None
        self.cancel_requested = False
        self.closing = False
        self.connected = False
        self.last_error_kind = None
        self.discovery_timeout_ms = 5000
        self.run_timeout_ms = 180000
        self.cancel_timeout_ms = 5000
        self._cancel_on_start = False

    @property
    def busy(self):
        return bool(self.requests)

    def _url(self, path):
        # Kept as a method solely for local fixture injection in tests.
        return QUrl(BASE_URL + path)

    def _request(self, kind, payload=None):
        path = {"tools": "/api/tools", "run": "/api/run", "cancel": "/api/cancel"}[kind]
        request = QNetworkRequest(self._url(path))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        request.setRawHeader(b"Cache-Control", b"no-store")
        for attribute in (
            QNetworkRequest.Attribute.CookieLoadControlAttribute,
            QNetworkRequest.Attribute.CookieSaveControlAttribute,
        ):
            request.setAttribute(attribute, QNetworkRequest.LoadControl.Manual)
        request.setTransferTimeout(30000)
        request.setRawHeader(b"Accept", b"text/event-stream" if kind == "run" else b"application/json")
        if payload is None:
            reply = self.manager.get(request)
        else:
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
            reply = self.manager.post(request, json.dumps(payload, allow_nan=False).encode("utf-8"))
        reply.setReadBufferSize(MAX_EVENT + 1)
        timer = QTimer(reply)
        timer.setSingleShot(True)
        context = {"kind": kind, "buffer": bytearray(), "bytes": 0, "problem": "", "start": False}
        if kind == "run":
            context.update(names=[call["name"] for call in payload["calls"]], calls=0, results=0)
        self.requests[reply] = context
        timer.timeout.connect(lambda: self._abort(reply, "انتهت مهلة الخدمة المحلية؛ لم تُعد المحاولة."))
        timer.start(
            {
                "tools": self.discovery_timeout_ms,
                "run": self.run_timeout_ms,
                "cancel": self.cancel_timeout_ms,
            }[kind]
        )
        reply.readyRead.connect(lambda: self._read(reply))
        reply.finished.connect(lambda: self._finish(reply))
        return reply

    def discover(self):
        if self.busy or self.closing:
            return
        self.connected = False
        self._request("tools")

    def run(self, payload):
        if not self.connected or self.busy or self.closing:
            raise ContractError("اتصل بالخدمة أولًا وانتظر انتهاء العملية الحالية.")
        self.run_id = payload["run_id"]
        self.last_error_kind = None
        self.cancel_requested = False
        self._cancel_on_start = False
        self._request("run", payload)

    def cancel(self):
        if not self.run_id or any(c["kind"] == "cancel" for c in self.requests.values()):
            return
        self.cancel_requested = True
        self._request("cancel", {"run_id": self.run_id})

    def _abort(self, reply, message):
        if reply not in self.requests:
            return
        context = self.requests[reply]
        context["problem"] = message
        if context["kind"] == "run":
            self.cancel()
        reply.abort()

    def _read(self, reply):
        context = self.requests.get(reply)
        if context is None or context["problem"]:
            return
        data = bytes(reply.readAll())
        context["bytes"] += len(data)
        if context["bytes"] > MAX_RESPONSE:
            self._abort(reply, "تجاوزت الاستجابة حد 8 MiB؛ أُوقف العرض وطُلب الإلغاء.")
            return
        context["buffer"].extend(data)
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if context["kind"] != "run" or status != 200:
            return
        content_type = str(reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader) or "").lower()
        if not content_type.startswith("text/event-stream"):
            self._abort(reply, "استجابة التشغيل ليست SSE المتوقعة من الخدمة المثبتة.")
            return
        try:
            while b"\n\n" in context["buffer"]:
                raw, _, rest = context["buffer"].partition(b"\n\n")
                context["buffer"] = bytearray(rest)
                if len(raw) > MAX_EVENT:
                    raise ContractError("حدث كبير يتجاوز حد العرض 2 MiB؛ طُلب الإلغاء.")
                lines = [line[5:].strip() for line in raw.splitlines() if line.startswith(b"data:")]
                if not lines:
                    continue
                event = _decode(b"\n".join(lines))
                self._validate_event(event, context)
                if event["type"] == "start" and self._cancel_on_start:
                    self._cancel_on_start = False
                    self.cancel()
                self.event_received.emit(event)
            if len(context["buffer"]) > MAX_EVENT:
                raise ContractError("تجاوز الحدث حد العرض الآمن؛ طُلب الإلغاء.")
        except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
            message = (
                str(exc) if isinstance(exc, ContractError) else "وصلت بيانات SSE غير صالحة؛ طُلب الإلغاء."
            )
            self._abort(reply, message)

    @staticmethod
    def _validate_event(event, context):
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ContractError("حدث غير متوافق من الخدمة.")
        kind = event["type"]
        if kind == "start":
            if context["start"] or type(event.get("api_calls_total")) is not int:
                raise ContractError("حدث بداية غير متوافق.")
            context["start"] = True
        elif not context["start"]:
            raise ContractError("وصل حدث قبل بداية التشغيل.")
        if kind in ("tool_call", "tool_result"):
            if not isinstance(event.get("name"), str):
                raise ContractError("حدث أمر غير متوافق.")
            if kind == "tool_call" and not isinstance(event.get("input"), dict):
                raise ContractError("معاملات حدث الأمر غير صالحة.")
            if kind == "tool_result" and ("result" not in event or type(event.get("is_error")) is not bool):
                raise ContractError("نتيجة أمر غير متوافقة.")
            index = context["calls"] if kind == "tool_call" else context["results"]
            if index >= len(context["names"]) or event["name"] != context["names"][index]:
                raise ContractError("نتيجة أمر لا تطابق الطلب؛ أُوقف التحليل.")
            if kind == "tool_result" and context["results"] >= context["calls"]:
                raise ContractError("وصلت نتيجة دون حدث تنفيذ مطابق.")
            context["calls" if kind == "tool_call" else "results"] += 1

    def _finish(self, reply):
        context = self.requests.get(reply)
        if context is None or context.get("finishing"):
            return
        context["finishing"] = True
        self._read(reply)
        context = self.requests.pop(reply, None)
        if context is None:
            return
        kind = context["kind"]
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        problem = context["problem"]
        body = bytes(context["buffer"])
        if not problem and status != 200:
            if status is None:
                self.connected = False
            detail = ""
            try:
                detail = _decode(body).get("detail", "")
            except (ValueError, AttributeError, RecursionError):
                pass
            self.last_error_kind = error_category(detail, status)
            problem = (
                error_message(detail, status)
                if status
                else "الخدمة غير متاحة؛ شغّل Osintgram محليًا ثم أعد الاتصال."
            )
        elif not problem and reply.error() != QNetworkReply.NetworkError.NoError:
            self.connected = False
            problem = (
                "انتهت مهلة استجابة الخدمة؛ طُلب الإلغاء دون إعادة المحاولة."
                if reply.error() == QNetworkReply.NetworkError.TimeoutError
                else "انقطع الاتصال بالخدمة؛ قد توجد نتائج جزئية فقط."
            )
        if kind == "run":
            if not problem and (body.strip() or not context["start"]):
                problem = "انتهت الاستجابة دون أحداث مكتملة؛ لا تُعد العملية ناجحة."
            if not problem and not self.cancel_requested and context["results"] != len(context["names"]):
                problem = "وصلت نتائج جزئية فقط؛ لم تكتمل الأوامر المطلوبة."
            if problem:
                self.cancel()
            self.run_id = None
            self.finished.emit(
                "failed" if problem else "cancelled" if self.cancel_requested else "complete", problem
            )
        else:
            try:
                if problem:
                    raise ContractError(problem)
                value = _decode(body)
                if kind == "tools":
                    self.connected = True
                    self.tools_ready.emit(validate_tools(value))
                else:
                    if not isinstance(value, dict) or type(value.get("cancelled")) is not bool:
                        raise ContractError("رد الإلغاء غير متوافق؛ لم يُؤكد توقف المحرك.")
                    acknowledged = value["cancelled"]
                    self._cancel_on_start = not acknowledged and self.run_id is not None
                    self.cancellation.emit(
                        acknowledged, "" if acknowledged else "لم تسجل الخدمة تشغيلًا قابلًا للإلغاء بعد."
                    )
            except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
                message = (
                    str(exc) if isinstance(exc, ContractError) else "رد JSON فارغ أو غير متوافق من الخدمة."
                )
                if kind == "tools":
                    self.connected = False
                    self.connection_error.emit(message)
                else:
                    self.cancellation.emit(False, message)
        reply.deleteLater()
        if not self.busy:
            self.idle.emit()

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        if self.run_id:
            self.cancel()
        for reply, context in list(self.requests.items()):
            if context["kind"] != "cancel":
                # Aborting SSE closes its generator, which also signals backend cancellation.
                self._abort(reply, "أُغلق التطبيق؛ طُلب إلغاء التحليل.")
