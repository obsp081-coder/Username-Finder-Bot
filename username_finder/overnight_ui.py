"""Per-job overnight consent and concise status; no scheduler or network probing."""

import json
import sqlite3

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .overnight import OvernightStore


class OvernightControls(QWidget):
    def __init__(self, panel, platform):
        super().__init__()
        self.panel = panel
        self.repo = OvernightStore(panel.repo.store, platform)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.enabled = QCheckBox("التشغيل الليلي التلقائي")
        self.enabled.setToolTip("للعملية المحددة أو التالية؛ اختيار الوضع وحده لا يبدأ أي عمل.")
        self.enabled.setToolTip(
            self.enabled.toolTip() + " الانتظار 30 ثم 60 ثم 120 ثانية؛ "
            "3 إعادات للاسم و12 للجلسة. التعطيل أثناء التشغيل يوقف العملية."
        )
        self.enabled.toggled.connect(self.changed)
        row.addWidget(self.enabled)
        self.details_button = QPushButton("تفاصيل التشغيل الليلي")
        self.details_button.clicked.connect(self.details)
        row.addWidget(self.details_button)
        self.prepare_button = QPushButton("تحضير عملية دون طلبات")
        self.prepare_button.clicked.connect(self.prepare_job)
        row.addWidget(self.prepare_button)
        panel.progress_changed.connect(self.update_prepare)
        panel.idle.connect(self.update_prepare)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.note = QLabel(
            "المحاولات مؤجلة ومحدودة؛ قيود المنصة و429 تحتاج مراجعة يدوية. "
            "لا يعمل الفحص أثناء سكون Windows أو إيقافه."
        )
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

    def update_prepare(self):
        self.prepare_button.setEnabled(not self.panel.busy and not self.panel.closing)

    def prepare_job(self):
        if self.panel.busy or self.panel.closing:
            return
        if self.repo.platform == "Roblox":
            self.panel.prepare(save_job=True)
        else:
            self.panel.prepare()
        self.update_prepare()

    def arm(self, identity):
        if self.repo.get(identity) is None and self.enabled.isChecked():
            self.repo.enable(identity, True)

    def changed(self, enabled):
        identity = self.panel.current_run
        if identity:
            active = bool(
                self.panel.busy
                and self.panel.runner
                and not self.panel.stopping
                and not self.panel.runner.cancel.is_set()
            )
            if active and not enabled:
                self.panel.runner.disable_overnight.set()
                self.panel.stop()
            try:
                self.repo.enable(identity, enabled, active=active and enabled, timeout_ms=0)
            except sqlite3.Error:
                self.refresh()
                if active and not enabled:
                    blocker = QSignalBlocker(self.enabled)
                    self.enabled.setChecked(False)
                    del blocker
                    self.status.setText("جارٍ الإيقاف؛ انتظر تأكيد حفظ التعطيل قبل إغلاق التطبيق.")
                else:
                    self.status.setText(
                        "تعذر حفظ التغيير؛ لم تُغيّر الموافقة المحفوظة. أعد المحاولة بعد زوال القفل."
                    )
                return
        self.refresh()
        self.panel.progress_changed.emit()

    def refresh(self):
        identity = self.panel.current_run
        data = self.repo.get(identity)
        if data:
            blocker = QSignalBlocker(self.enabled)
            self.enabled.setChecked(data["enabled"])
            del blocker
        self.status.setText(
            "ليلي مفعّل للعملية التالية؛ لم يبدأ أي طلب."
            if not data and self.enabled.isChecked()
            else self.repo.label(identity)
        )
        self.details_button.setEnabled(bool(data))

    def details(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("سجل التشغيل الليلي — آخر 100 حدث")
        dialog.resize(760, 500)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(self.repo.get(self.panel.current_run), ensure_ascii=False, indent=2))
        layout.addWidget(text)
        close = QPushButton("إغلاق")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def resume_after_launch(self):
        window = self.panel.window()
        if self.panel.busy or self.panel.closing or window.busy or window.closing or window.closed:
            return
        candidates = self.repo.startup_candidates(self.panel.repo)
        if candidates:
            if self.repo.platform == "X" and not self.panel.recovery.can_start(candidates[0]):
                self.repo.end(candidates[0], "Restricted", "existing_recovery_guard")
                self.refresh()
                return
            self.panel._start_run(candidates[0], automatic=True)
        self.refresh()


def schedule_startup_recovery(window):
    # Once per application launch, after both platform repositories recovered uncertain tasks.
    # No polling, no restricted-job probing, and no automatic restart after Stop.
    for panel in (window.roblox, window.x_panel):
        QTimer.singleShot(0, panel.overnight.resume_after_launch)
