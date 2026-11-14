"""Explicit maintenance access to existing permission/recovery rules, never a checking engine."""

import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import rovalid, rovalid_review
from .ui_cards import caption
from .ui_helpers import local_time

X_STATES = {
    "None": "لا قيد محفوظ يحتاج استعادة.",
    "Released": "سُجل التحرير سابقًا؛ لا استعادة مطلوبة ولا استئناف تلقائي.",
    "Eligible": "يسمح المحرك بطلب استعادة واحد بعد تأكيد مستقل.",
    "NewRestriction": "قيد جديد؛ لا يُستعمل نجاح سابق لتحريره تلقائيًا.",
    "Waiting": "الانتظار المحفوظ لم ينتهِ؛ حدّث الحالة بعد الموعد، دون طلب تلقائي.",
    "InFlight": "طلب استعادة غير منتهٍ؛ لا يُرسل طلب آخر ولا يُحرر القيد.",
    "AwaitingRelease": "نجح طلب واحد؛ القيد باقٍ حتى انتهاء الانتظار وإقرار التحرير المستقل.",
    "Failed": "فشل الطلب السابق؛ القيد باقٍ وأي محاولة مسموحة تحتاج تأكيدًا جديدًا.",
    "Cancelled": "أُلغي الطلب السابق؛ الأدلة والقيد باقية ولا إعادة تلقائية.",
    "Uncertain": "طلب سابق غير محسوم؛ لا إعادة تلقائية. الإجراء المتاح تحدده قواعد المحرك.",
    "ReviewRequired": "الدليل لا يؤهل للاستعادة: نوع قيد أو انتظار أو اتصال غير صالح؛ يلزم تدقيق تقني.",
}


class PlatformMaintenanceDialog(QDialog):
    changed = Signal()

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.x_panel = window.x_panel
        self.request_active = False
        self.close_pending = False
        self.roblox_state = {}
        self.setWindowTitle("صيانة المنصات — إجراءات صريحة")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setMinimumSize(500, 420)
        self.resize(min(760, window.width() - 40), min(600, window.height() - 80))
        layout = QVBoxLayout(self)
        layout.addWidget(caption("عرض الحالة محلي؛ لا طلب أو تفويض أو تحرير أو استئناف تلقائي."))
        self.pages = QTabWidget()
        layout.addWidget(self.pages, 1)
        self.build_roblox()
        self.build_x()
        actions = QHBoxLayout()
        self.refresh_button = QPushButton("تحديث الحالة محليًا")
        self.refresh_button.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_button)
        close = QPushButton("إغلاق")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.changed.connect(self.refresh)
        self.x_panel.idle.connect(self.x_idle)
        window.roblox.idle.connect(self.refresh)
        self.refresh()

    def page(self, title):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        form = QFormLayout(body)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        scroll.setWidget(body)
        self.pages.addTab(scroll, title)
        return form

    @staticmethod
    def readout():
        field = QPlainTextEdit()
        field.setReadOnly(True)
        field.setMinimumHeight(100)
        field.setMaximumHeight(160)
        return field

    def build_roblox(self):
        form = self.page("Roblox / RoValid")
        self.mode, self.connection = QComboBox(), QComboBox()
        self.mode.addItem("Bulk ثم Validator", "two_stage")
        self.mode.addItem("Validator فقط", "validator")
        for label, value in (
            ("مباشر", "direct"),
            ("proxies خاصة (TXT أو لصق)", "private"),
            ("مصادر عامة / prescreen", "sources"),
        ):
            self.connection.addItem(label, value)
        panel = self.window.roblox
        selected_mode = panel.record.get("settings", {}).get("mode", panel.checking_mode.currentData())
        self.mode.setCurrentIndex(self.mode.findData(selected_mode))
        self.connection.setCurrentIndex(self.connection.findData(panel.connection_scope()))
        form.addRow("النمط محل القرار", self.mode)
        form.addRow("نطاق الاتصال", self.connection)
        form.addRow(
            caption(
                "جلب المصادر وفحص اتصالها يحتاج two_stage/sources؛ "
                "Validator عبرها يحتاج validator/sources أيضًا. القرار لا يغيّر إعداد عملية محفوظة."
            )
        )
        self.roblox_reason = self.readout()
        form.addRow(self.roblox_reason)
        self.reviewer, self.evidence = QLineEdit(), QLineEdit()
        self.reviewer.setMaxLength(100)
        self.evidence.setMaxLength(1000)
        form.addRow("اسم المراجع", self.reviewer)
        form.addRow("مرجع الدليل ونطاقه وسبب القرار", self.evidence)
        form.addRow(caption("لا تكتب أسرارًا. نجاح CLI أو HTTP 200 لا يثبت السعة أو زوال القيد."))
        self.decision = QComboBox()
        form.addRow("القرار", self.decision)
        self.save_review = QPushButton("حفظ قرار محدد بتأكيد…")
        self.save_review.clicked.connect(self.record_review)
        form.addRow(self.save_review)
        self.roblox_result = caption("")
        form.addRow(self.roblox_result)
        self.mode.currentIndexChanged.connect(self.refresh)
        self.connection.currentIndexChanged.connect(self.refresh)
        self.reviewer.textChanged.connect(self.review_controls)
        self.evidence.textChanged.connect(self.review_controls)

    def build_x(self):
        form = self.page("X")
        self.x_reason = self.readout()
        form.addRow(self.x_reason)
        form.addRow(
            caption(
                "الطلب يستخدم اتصال القيد الأصلي. نجاح طلب واحد لا يثبت زوال القيد "
                "أو استقرار الفحص المستمر. التحرير خطوة مستقلة ولا يستأنف العملية القديمة."
            )
        )
        self.x_check = QPushButton("طلب استعادة واحد بتأكيد…")
        self.x_release = QPushButton("إقرار تحرير القيد بتأكيد مستقل…")
        self.x_stop = QPushButton("إيقاف طلب الصيانة بأمان")
        for button, callback in (
            (self.x_check, self.check_x),
            (self.x_release, self.release_x),
            (self.x_stop, self.stop_x),
        ):
            button.clicked.connect(callback)
            form.addRow(button)
        self.x_result = caption("")
        form.addRow(self.x_result)

    def idle(self):
        if (
            self.close_pending
            or self.window.roblox.busy
            or self.window.roblox.closing
            or self.x_panel.busy
            or self.x_panel.closing
        ):
            return False
        try:
            with self.x_panel.repo.store.lock:
                return (
                    self.x_panel.repo.store.db.execute(
                        "SELECT 1 FROM x_runs WHERE state='Running' LIMIT 1"
                    ).fetchone()
                    is None
                )
        except sqlite3.Error:
            return False

    def refresh(self, *_):
        self.roblox_state = rovalid.permission(
            self.window.store.path,
            mode=self.mode.currentData(),
            connection=self.connection.currentData(),
        )
        state = self.roblox_state
        self.roblox_reason.setPlainText(
            state["reason"] + "\nبصمة الحالة: " + state.get("fingerprint", "غير متاحة")
        )
        chosen = self.decision.currentData()
        self.decision.blockSignals(True)
        self.decision.clear()
        self.decision.addItem("تسجيل المراجعة فقط — بلا تفويض", "review")
        self.decision.addItem("رفض التشغيل لهذا النطاق", "deny")
        if state.get("local_allowed"):
            self.decision.addItem("تفويض صريح لهذا النمط والاتصال والدليل فقط", "authorize")
        index = self.decision.findData(chosen)
        self.decision.setCurrentIndex(max(0, index))
        self.decision.blockSignals(False)
        self.review_controls()
        try:
            status = self.x_panel.recovery.status()
            text = X_STATES[status["state"]] + "\nحالة المحرك: " + status["state"]
            info = status["info"]
            if info:
                text += f"\nالسبب المحفوظ: {info['reason']} • HTTP {info['http_status'] or 'غير معروف'}"
                route = info.get("connection") or {}
                text += "\nاتصال القيد الأصلي: " + (
                    "مباشر" if route.get("mode") == "direct" else "بروكسي محفوظ"
                )
            if status["wait_until"]:
                text += "\nموعد الانتظار: " + local_time(status["wait_until"])
            latest = status["latest"]
            if latest:
                evidence = latest.get("evidence") or {}
                http = evidence.get("http_status")
                http = f"HTTP {http}" if type(http) is int and 100 <= http <= 599 else "لا رد HTTP مؤكد"
                text += f"\nآخر طلب صيانة محفوظ: {latest['state']} • {http}."
            if not self.idle():
                text += "\nتوجد عملية جارية أو إغلاق مطلوب؛ إجراءات جديدة ممنوعة."
            self.x_reason.setPlainText(text)
            self.x_check.setEnabled(self.idle() and status["eligible"])
            self.x_release.setEnabled(self.idle() and status["release_eligible"])
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, sqlite3.Error):
            self.x_reason.setPlainText("دليل استعادة X غير سليم أو تعذرت قراءته؛ لا إجراء قبل التدقيق.")
            self.x_check.setEnabled(False)
            self.x_release.setEnabled(False)
        self.x_stop.setEnabled(self.request_active and self.x_panel.busy and not self.x_panel.stopping)

    def review_controls(self, *_):
        self.save_review.setEnabled(
            bool(
                self.idle()
                and self.roblox_state.get("fingerprint")
                and self.reviewer.text().strip()
                and self.evidence.text().strip()
            )
        )

    def confirm(self, text):
        return (
            QMessageBox.question(
                self,
                "تأكيد إجراء صيانة مستقل",
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def record_review(self):
        if not self.save_review.isEnabled() or not self.idle():
            return
        decision = dict(
            mode=self.mode.currentData(),
            connection=self.connection.currentData(),
            decision=self.decision.currentData(),
            reviewer=self.reviewer.text(),
            evidence=self.evidence.text(),
            fingerprint=self.roblox_state["fingerprint"],
        )
        if not self.confirm(
            f"تسجيل {self.decision.currentText()} للنمط {self.mode.currentData()} "
            f"والاتصال {self.connection.currentData()}؟ لا يبدأ فحصًا ولا يتجاوز قيدًا."
        ):
            return
        if not self.idle():
            return
        try:
            rovalid_review.record_review(
                self.window.store.path,
                **decision,
            )
            self.roblox_result.setText("حُفظ القرار وبقيت الأدلة الأصلية؛ التشغيل يحتاج تأكيده المعتاد.")
        except (ValueError, OSError, sqlite3.Error):
            self.roblox_result.setText(
                "لم يُقبل القرار: تغير الدليل أو تعذر تدقيقه وحفظه؛ حدّث الحالة وأعد المراجعة."
            )
        self.window.roblox.controls()
        self.refresh()

    def check_x(self):
        if not self.idle():
            return
        try:
            if not self.x_panel.recovery.status()["eligible"]:
                self.refresh()
                return
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, sqlite3.Error):
            self.refresh()
            return
        if (
            not self.confirm(
                "إرسال طلب X واحد للاسم OpenAI عبر اتصال القيد الأصلي؟ "
                "لن يُحرر القيد أو يعيد العملية القديمة تلقائيًا."
            )
            or not self.idle()
        ):
            return
        self.request_active = True
        self.x_result.setText("جارٍ طلب استعادة واحد؛ الإيقاف قد ينتظر مهلة الطلب قبل حفظه.")
        self.x_panel._submit(
            lambda: self.x_panel.recovery.check(
                confirmed=True,
                fetcher=self.x_panel.fetcher,
                cancel=self.x_panel.cancel,
                updated=self.changed.emit,
            )
        )
        self.refresh()

    def release_x(self):
        if not self.idle():
            return
        try:
            if not self.x_panel.recovery.status()["release_eligible"]:
                self.refresh()
                return
            if not self.confirm(
                "أقر بأن نجاح طلب واحد لا يضمن استقرار الفحص. هل أحرر القيد وفق قواعد المحرك؟ "
                "يلزم بحث جديد بالاتصال الأصلي وفاصل 5 ثوانٍ أو أكثر، دون استئناف القديم."
            ):
                return
            if not self.idle():
                return
            self.x_panel.recovery.release(acknowledged=True)
            blocked = self.x_panel.speed.blockSignals(True)
            self.x_panel.speed.setCurrentIndex(
                self.x_panel.speed.findData(self.x_panel.repo.preferred_interval())
            )
            self.x_panel.speed.blockSignals(blocked)
            self.x_panel.refresh()
            self.x_result.setText("سُجل التحرير الصريح؛ لم تُنشأ عملية ولم تُستأنف أي عملية محفوظة.")
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, sqlite3.Error):
            self.x_result.setText("لم يُقبل التحرير؛ راجع الدليل والانتظار والحالة الحالية.")
        self.refresh()

    def stop_x(self):
        if self.request_active and self.x_panel.busy:
            self.x_panel.stop(manual=False)
            self.x_result.setText("أُرسل الإلغاء؛ ننتظر انتهاء الطلب وحفظ دليله دون إعادة أو تحرير.")
            self.refresh()

    def x_idle(self):
        if self.request_active:
            self.request_active = False
            self.x_result.setText("انتهى طلب الصيانة؛ راجع حالة المحرك المحفوظة. لا تحرير أو إعادة تلقائية.")
        if self.close_pending:
            super().reject()
        else:
            self.refresh()

    def reject(self):
        if self.request_active and self.x_panel.busy:
            self.close_pending = True
            self.stop_x()
            return
        super().reject()

    def closeEvent(self, event):
        if self.request_active and self.x_panel.busy:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
