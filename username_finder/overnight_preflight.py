"""Point-in-time local checks; never start jobs, probe providers or rewrite evidence."""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import x, x_connection
from .overnight import MAX_CANDIDATE_RETRIES, MAX_JOB_RETRIES, STATES
from .roblox_storage import check_interval
from .x_recovery import XRecovery


@dataclass(frozen=True)
class JobIntent:
    platform: str
    identity: str | None
    busy: bool = False
    stopping: bool = False
    closing: bool = False


@dataclass
class Readiness:
    platform: str
    identity: str | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)

    @property
    def verdict(self):
        return "محظورة" if self.errors else "جاهزة مع تنبيه" if self.warnings else "جاهزة"

    def text(self):
        return "\n".join(
            [f"{self.platform}: {self.verdict}", f"العملية: {self.identity or 'غير محددة'}"]
            + self.facts
            + ["سبب المنع: " + value for value in self.errors]
            + ["تنبيه: " + value for value in self.warnings]
        )


def probe_storage(path, *, disk_usage=shutil.disk_usage):
    """Acquire and roll back an empty write transaction; no rows or schema are changed."""
    errors, warnings = [], []
    if not os.access(path, os.W_OK) or not os.access(path.parent, os.W_OK):
        errors.append("ملف SQLite أو مجلده غير قابل للكتابة.")
    try:
        # mode=rw refuses to create a missing database. Zero timeout never waits on a writer.
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
    except (OSError, sqlite3.Error):
        errors.append("تعذر حجز معاملة كتابة SQLite؛ قد تكون مقفلة أو غير قابلة للكتابة. أعد الفحص.")
    try:
        free = disk_usage(path.parent).free
        if free < 16 * 1024 * 1024:
            errors.append("المساحة الحرة أقل من الحد الاحترازي المحلي 16 MiB.")
        elif free < 256 * 1024 * 1024:
            warnings.append("المساحة الحرة أقل من 256 MiB؛ راقب مساحة حفظ النتائج.")
    except OSError:
        warnings.append("تعذر قياس المساحة الحرة.")
    return errors, warnings


def _date(value):
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError("Naive waiting deadline")
    return date.astimezone(UTC)


def check_job(repo, night, intent, *, storage=probe_storage, clock=lambda: datetime.now(UTC)):
    result = Readiness(intent.platform, intent.identity)
    if intent.platform not in ("Roblox", "X"):
        result.errors.append("منصة غير مدعومة لهذا الفحص.")
        return result
    if not intent.identity:
        result.errors.append("حضّر وحدد عملية أولًا.")
        return result
    try:
        with repo.store.lock:
            run = repo.progress(intent.identity)
            if not run:
                raise ValueError("Missing run")
            control = repo.control()
            data = night.get(intent.identity) or {}
            prefix = "roblox" if intent.platform == "Roblox" else "x"
            counts = dict(
                repo.store.db.execute(
                    f"SELECT state,COUNT(*) FROM {prefix}_queue WHERE run_id=? GROUP BY state",
                    (intent.identity,),
                )
            )
            active = repo.store.db.execute(
                f"SELECT COUNT(*) FROM {prefix}_runs WHERE state='Running'"
            ).fetchone()[0]
            pending = counts.get("Pending", 0)
            result.facts.append(f"المتبقي: {pending} / {run['total']} • الحالة: {run['state']}")
            if run["state"] not in ("Ready", "Paused"):
                result.errors.append("العملية ليست جاهزة أو متوقفة مؤقتًا؛ حضّرها أو راجع حالتها.")
            if not pending:
                result.errors.append("لا توجد أسماء معلقة للفحص.")
            if counts.get("InFlight") or set(counts) - {"Pending", "Done", "Skipped", "InFlight"}:
                result.errors.append("طابور غير محسوم؛ يلزم مراجعة الاستعادة قبل البدء.")
            if intent.busy or intent.stopping or active or control.get("active_run"):
                result.errors.append("يوجد عامل أو إيقاف جارٍ لهذه المنصة؛ لن يبدأ عامل ثانٍ.")
            if intent.closing:
                result.errors.append("التطبيق يغلق؛ انتظر إعادة فتحه.")
            if control.get("blocked"):
                result.errors.append("قيد منصة محفوظ يمنع البدء: " + str(control["blocked"]))
            if not data.get("enabled"):
                result.errors.append("فعّل التشغيل الليلي لهذه العملية أولًا.")
            result.facts.append("ليلي: " + STATES.get(data.get("state"), "معطّل أو غير معروف"))
            if data.get("state") in ("Stopped", "Completed", "Review", "Superseded"):
                result.errors.append("توقف يدوي أو حالة نهائية/مراجعة؛ لا استئناف ليلي تلقائي.")
            elif data.get("enabled") and data.get("state") not in (
                "Armed",
                "Running",
                "Connectivity",
                "Delay",
                "Interrupted",
            ):
                result.errors.append("حالة الاستعادة الليلية غير معروفة.")
            if data.get("enabled") and (
                type(data.get("auto_resume")) is not bool
                or data.get("state") == "Armed"
                and data.get("auto_resume")
                or data.get("state") in ("Running", "Connectivity", "Delay", "Interrupted")
                and not data.get("auto_resume")
                or data.get("state") == "Connectivity"
                and not data.get("retry_at")
            ):
                result.errors.append("موافقة الاستئناف أو حالة الانتظار غير متسقة.")
            interval = (
                check_interval(run["interval_seconds"])
                if prefix == "roblox"
                else x.interval(run["interval_seconds"])
            )
            result.facts.append(f"الفاصل المحفوظ: {interval:g} ثانية • الطلبات متسلسلة")
            if interval < 5:
                result.warnings.append("السرعة أقل من 5 ثوانٍ؛ ثباتها طويل المدة غير مثبت.")
            route = repo.connection_for_run(intent.identity) if prefix == "x" else dict(x_connection.DIRECT)
            result.facts.append("الاتصال المحفوظ: " + x_connection.label(route))
            if prefix == "x":
                result.warnings.append("X تجريبي؛ الإتاحة أولية ولا ضمان لاستمرار الخدمة.")
                if not XRecovery(repo).can_start(intent.identity):
                    result.errors.append("حراسة ما بعد تحرير X تمنع هذا العمل أو سرعته أو اتصاله.")
            for key, maximum in (("attempts", MAX_JOB_RETRIES), ("candidate_retries", MAX_CANDIDATE_RETRIES)):
                value = data.get(key, 0)
                if type(value) is not int or not 0 <= value <= maximum:
                    result.errors.append("عدادات الاستعادة غير متسقة.")
            attempts = data.get("attempts", 0)
            performed = data.get("retries_performed", 0)
            if type(performed) is not int or type(attempts) is not int or not 0 <= performed <= attempts:
                result.errors.append("عداد الإعادات الفعلية غير متسق.")
            result.facts.append(f"ميزانية الإعادة المحفوظة: {attempts}/{MAX_JOB_RETRIES}")
            if data.get("auto_resume"):
                _date(data["session_started_at"])
                result.facts.append("الاستعادة بعد إعادة الفتح: مؤهلة بشرط استمرار السماح وعدم اليقين.")
            else:
                result.facts.append("الاستعادة بعد إعادة الفتح: تُسلّح عند البدء الصريح فقط.")
            for value in (control.get("not_before"), data.get("retry_at"), data.get("waiting_until")):
                if value and _date(value) > clock():
                    result.warnings.append("لن يبدأ الطلب قبل الموعد المحفوظ: " + value)
            if data.get("retry_at") and data.get("auto_resume"):
                task = repo.store.db.execute(
                    f"SELECT state FROM {prefix}_queue WHERE run_id=? AND position=?",
                    (intent.identity, data.get("position")),
                ).fetchone()
                if not task or task[0] != "Pending" or not data.get("candidate_retries"):
                    result.errors.append("موعد إعادة المحاولة لا يطابق اسمًا معلقًا وميزانية صحيحة.")
        errors, warnings = storage(repo.store.path)
        result.errors.extend(errors)
        result.warnings.extend(warnings)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error):
        result.errors.append("تعذر قراءة أو التحقق من الإعدادات والحالة المحفوظة؛ يلزم مراجعتها.")
    return result
