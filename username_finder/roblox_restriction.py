"""Read-only restriction presentation and an explicitly authorized one-name diagnostic."""

import json
import sqlite3
import threading
from datetime import UTC, datetime

from .roblox import syntax_error
from .roblox_activity import RobloxActivity
from .roblox_runner import RobloxRunner

RECORDED_REASONS = {
    "http_429": "HTTP 429 — تقييد معدل محفوظ",
    "http_403": "HTTP 403 — رفض وصول محفوظ",
    "http_401": "HTTP 401 — رفض وصول محفوظ",
    "challenge": "تحدٍ محفوظ من الموفر",
    "timeout": "انتهاء مهلة محفوظ",
    "network_error": "فشل شبكة محفوظ",
    "incomplete_body": "رد غير مكتمل محفوظ",
    "retry_after_review_required": "مهلة موفر محفوظة تحتاج مراجعة",
}


def _local_status(repo, at):
    guard = RobloxActivity(repo.store.path)
    if guard.block.exists():
        with guard.block.open("rb") as stream:
            reason = stream.read(256).decode("utf-8", errors="replace")
        return False, (
            "قيد أمان محلي محفوظ (تاريخي)، وليس فحصًا جديدًا للموفر: "
            + RECORDED_REASONS.get(reason, "سبب وقائي محفوظ غير معروف؛ لا يُعرض نصه الحر")
            + ". راجع دليل العملية المالكة يدويًا؛ لا يحرره نجاح المتصفح أو هذا الزر."
        )
    cooldown = guard.cooldown_state()
    if cooldown and cooldown["state"] == "Hold":
        return False, "مهلة Bulk محفوظة؛ راجع العملية المالكة وانتظارها وتفويضها المنفصل. لا تحرير هنا."
    if guard.pending.exists():
        with guard.pending.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 1024))
            tail = stream.read(1024)
        if not tail.endswith(b"\n") or not tail.splitlines() or tail.splitlines()[-1] != b"Clear":
            return False, "حالة Bulk غير محسومة أو تالفة؛ راجع دليل العملية قبل أي إرسال. لا إعادة تلقائية."
    control = repo.control()
    if not isinstance(control, dict):
        raise ValueError("Invalid restriction state")
    for counter in ("unknown_streak", "restriction_streak"):
        value = control.get(counter, 0)
        if type(value) is not int or value < 0:
            raise ValueError("Invalid restriction counter")
        if value >= 2:
            return False, "سجل نتائج غير محسومة أو قيود متكررة؛ يلزم تدقيق يدوي قبل التشخيص."
    if control.get("blocked"):
        return False, "قيد أمان فردي محفوظ؛ راجع آخر نتيجة وسبب توقف العملية يدويًا قبل أي تشغيل."
    if control.get("active_run"):
        return False, "توجد عملية Roblox مسجلة كنشطة؛ انتظر انتهائها أو راجع الاسترداد المعتاد."
    deadline = control.get("not_before")
    if deadline:
        parsed = datetime.fromisoformat(deadline)
        if parsed.tzinfo is None:
            raise ValueError("Naive restriction deadline")
        if parsed > at:
            return (
                False,
                f"موعد عدم الإرسال المحفوظ لم ينقضِ: {parsed.astimezone(UTC).isoformat()}. "
                "انتظر ثم تحقق محليًا.",
            )
    with repo.store.lock:
        inflight = repo.store.db.execute(
            "SELECT 1 FROM roblox_queue WHERE state='InFlight' LIMIT 1"
        ).fetchone()
    if inflight:
        return False, "توجد نية إرسال فردية غير محسومة؛ راجع الاسترداد المعتاد دون إعادة الطلب."
    return True, "الحواجز المحلية تسمح بطلب تشخيص واحد عند تأكيده؛ هذا لا يثبت أن Roblox سيسمح به."


def _last_observation(repo):
    with repo.store.lock:
        row = repo.store.db.execute(
            "SELECT evidence FROM roblox_checks WHERE source='roblox-api' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return "لا توجد محاولة فردية مرصودة محفوظة؛ لا تُقرأ أدلة Bulk التاريخية هنا."
    evidence = json.loads(row[0])
    if not isinstance(evidence, dict):
        raise ValueError("Invalid saved evidence")
    status = evidence.get("http_status")
    # Show only bounded typed metadata, never raw response messages or headers.
    http = f"HTTP {status}" if type(status) is int and 100 <= status <= 599 else "لم يصل رد HTTP"
    classification = evidence.get("classification")
    if classification not in {"Available", "Taken", "Reserved", "Invalid", "Unknown"}:
        classification = "Unknown"
    at = datetime.fromisoformat(evidence["checked_at"])
    return f"آخر محاولة فردية محفوظة: {http} • {classification} (أولي) • {at.isoformat()}."


def restriction_status(repo, *, at=None, lock_held=False):
    """Never recover, release markers, update SQL or send a request."""
    at = at or datetime.now(UTC)
    try:
        allowed, local = _local_status(repo, at)
        if allowed and not lock_held:
            # No recovery callbacks: the normal shared guard remains authoritative.
            try:
                with RobloxActivity(repo.store.path):
                    allowed, local = _local_status(repo, at)
            except ValueError as exc:
                allowed, local = False, str(exc)
        observation = _last_observation(repo)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        allowed = False
        local = "تعذر تأكيد الحواجز أو سلامة الحالة المحلية؛ يلزم تدقيق يدوي، ولن يُرسل طلب."
        observation = "تعذر تأكيد آخر محاولة محفوظة."
    return {
        "allowed": allowed,
        "text": (
            f"السماح المحلي: {'مسموح' if allowed else 'غير مسموح'}.\n{local}\n{observation}\n"
            "حالة الموفر الآن: غير معروفة؛ عرض الحالة لا يتصل بـRoblox."
        ),
    }


class RestrictionDiagnostic(RobloxRunner):
    """One new, non-overnight run under the same shared lock; never resume old work."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.identity = None
        self._once = threading.Lock()
        self._used = False

    def run(self, username):
        with self._once:
            if self._used:
                raise ValueError("هذا التشخيص استُخدم بالفعل؛ يلزم إجراء جديد بتأكيد منفصل.")
            self._used = True
        error = syntax_error(username)
        if error:
            raise ValueError(error)
        with RobloxActivity(self.repository.store.path):
            status = restriction_status(self.repository, at=self.clock(), lock_held=True)
            if not status["allowed"]:
                raise ValueError(status["text"])
            if self.cancel.is_set():
                return None
            self.identity = self.repository.create_run([username], kind="restriction_diagnostic", skip=False)
            # The ordinary runner handles InFlight, raw evidence, restrictions and cleanup.
            # No overnight record is armed, hence no retry or automatic resumption.
            super()._run(self.identity)
            return self.identity


def diagnostic_summary(repo, identity):
    with repo.store.lock:
        row = repo.store.db.execute(
            "SELECT evidence FROM roblox_checks WHERE run_id=? ORDER BY id DESC LIMIT 1", (identity,)
        ).fetchone()
    if row is None:
        return "لم يُحفظ رد تشخيص؛ راجع حالة العملية. لا إعادة تلقائية."
    evidence = json.loads(row[0])
    status = evidence.get("http_status")
    if status == 429:
        outcome = "HTTP 429: تقييد معدل؛ حُفظت المهلة والقيود."
    elif status in (401, 403):
        outcome = f"HTTP {status}: رفض وصول؛ النتيجة غير محسومة وحُفظ القيد الوقائي."
    elif status == 200 and evidence["classification"] != "Unknown":
        outcome = "HTTP 200: نجح هذا الطلب وحده بمؤشر API أولي."
    else:
        outcome = "الرد مبهم أو مقيد، أو تعذر الاتصال؛ النتيجة غير محسومة."
    return outcome + " لا يثبت ذلك سعة مستدامة أو زوال القيود نهائيًا. لا إعادة تلقائية."
