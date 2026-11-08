"""Optional RTL account-evidence workspace; never dispatches without review."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QGridLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .platform_workspace import ResultsPresentation
from .roblox_bulk import BulkExperiment

LABELS = {
    "CurrentAccountMatchByAPI": "تطابق حساب حالي API",
    "HistoricalOrDifferentCurrentName": "اسم سابق أو حالي مختلف",
    "NoMatchingBulkRecord": "لم يُعد سجل مطابق — الإتاحة مجهولة",
    "Unknown": "مجهول",
    "UnsafeOrContradictoryResponse": "استجابة غير آمنة أو متعارضة",
}


class BulkPanel(QWidget):
    changed = Signal()
    finished = Signal(str)
    idle = Signal()

    def __init__(self, parent_panel):
        super().__init__()
        self.owner = parent_panel
        self.busy = False
        self.closing = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="roblox-bulk")
        database = parent_panel.repo.store.path
        self.experiment = BulkExperiment(
            database.parent / "roblox-bulk-account-lookup",
            database,
            control=parent_panel.repo.control,
            updated=self.changed.emit,
        )
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout = QVBoxLayout(self)
        warning = QLabel(
            "تجريبي: بحث عن الحسابات فقط، وليس فحص إتاحة التسجيل. "
            "وجود حساب لا يثبت Taken التسجيل، وغياب السجل لا يعني Available.\n"
            "1000 اسم مجمّد: المرحلة الأولى 200، والثانية 800 بتفويض مستقل. "
            "فاصل لا يقل عن 10 ثوانٍ؛ لا إعادة إرسال بعد التوقف أو الانقطاع."
        )
        warning.setWordWrap(True)
        warning.setObjectName("simulation")
        layout.addWidget(warning)
        self.identity = QLabel("لم تُحضّر العينة بعد؛ لا طلبات تلقائية.")
        self.identity.setWordWrap(True)
        self.identity.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.identity)
        grid = QGridLayout()
        self.buttons = {}
        for i, (key, text, callback) in enumerate(
            (
                ("prepare", "تحضير العينة محليًا", self.prepare),
                ("inspect", "مراجعة العينة والأدلة", self.inspect),
                ("stage1", "تفويض المرحلة 1 — 10 طلبات", lambda: self.start("stage1")),
                ("comparison", "تفويض المقارنة — 20 طلبًا", lambda: self.start("comparison")),
                ("stage2", "تفويض المرحلة 2 — 40 طلبًا", lambda: self.start("stage2")),
                ("stop", "إيقاف آمن دون إعادة إرسال", self.stop),
                ("csv", "تصدير CSV…", lambda: self.export("csv")),
                ("json", "تصدير JSON والأدلة الخام…", lambda: self.export("json")),
            )
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            grid.addWidget(button, i // 2, i % 2)
            self.buttons[key] = button
        layout.addLayout(grid)
        self.status = QLabel("جاهز — لم يبدأ أي طلب.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 50)
        layout.addWidget(self.progress)
        self.counters = QLabel()
        self.counters.setWordWrap(True)
        layout.addWidget(self.counters)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["الاسم", "دليل الحساب", "الاسم الحالي", "الفردي الأولي", "التعارض"]
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(160)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.results_presentation = ResultsPresentation(self.table, (0, 1), "Bulk · دليل حسابات")
        layout.addWidget(self.results_presentation)
        layout.addWidget(self.table)
        for label in (warning, self.identity, self.status, self.counters):
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.changed.connect(self.refresh)
        self.finished.connect(self.done)
        self.refresh()

    def notify(self, message):
        self.status.setText(str(message))

    def prepare(self):
        try:
            root = Path(__file__).resolve().parents[1] / "experiments/roblox-bulk-validation/output"
            self.experiment.prepare(
                [
                    root / "manifest.json",
                    root / "random-pilot/manifest.json",
                    root / "expanded-100/manifest.json",
                ]
            )
            self.refresh()
            self.notify("حُفظت العينة وبصمتها والمقارنة العشوائية دون اتصال؛ راجعها قبل التفويض.")
        except Exception as exc:
            self.notify(exc)

    def refresh(self):
        exists = self.experiment.manifest_path.exists()
        for key, button in self.buttons.items():
            button.setEnabled(not self.busy and not self.closing and (key == "prepare" or exists))
        self.buttons["stop"].setEnabled(self.busy)
        self.owner.checking_mode.setEnabled(not self.busy and not self.owner.busy and not self.closing)
        if not exists:
            return
        try:
            report = self.experiment.report()
            self.identity.setText(
                "العينة: 1000 اسم • البصمة: "
                + report["manifest"]["fingerprint"]
                + "\nالمجلد: "
                + str(self.experiment.directory)
            )
            for phase in ("stage1", "comparison", "stage2"):
                try:
                    self.experiment.review(phase)
                except ValueError:
                    self.buttons[phase].setEnabled(False)
            self.buttons["prepare"].setEnabled(False)
            self.progress.setValue(report["completed_bulk_batches"])
            self.counters.setText(
                f"دفعات مكتملة: {report['completed_bulk_batches']}/50 • أسماء حاول إرسالها: "
                f"{report['requested_bulk_names']} • نيات: {report['dispatch_intents']} • "
                f"غير مؤكد: {report['uncertain_or_missing_responses']} • "
                f"المقارنة: {report['comparison_coverage']}/20\n"
                + " • ".join(f"{LABELS[k]}: {v}" for k, v in report["counts"].items())
            )
            rows = report["account_observations"]
            self.table.setRowCount(len(rows))
            for i, (name, row) in enumerate(rows.items()):
                values = [
                    name,
                    LABELS[row["state"]],
                    row.get("record", {}).get("name", ""),
                    report["individual_observations"].get(name, {}).get("classification", "لم يُفحص"),
                    "يتطلب مراجعة" if name in report["semantic_disagreements"] else "",
                ]
                for j, value in enumerate(values):
                    self.table.setItem(i, j, QTableWidgetItem(str(value)))
            if report["journal_integrity_warning"]:
                self.notify(
                    "الدليل غير مكتمل؛ العرض للجزء السليم فقط، والتشغيل محظور: "
                    + report["journal_integrity_warning"]
                )
            elif self.busy:
                self.notify("جارٍ تنفيذ المرحلة المفوضة فقط؛ طلب واحد في كل مرة، وإيقاف متاح.")
            elif report["phases"]:
                self.notify(
                    " • ".join(f"{p}: {v['state']} — {v['stop_reason']}" for p, v in report["phases"].items())
                )
        except Exception as exc:
            for phase in ("stage1", "comparison", "stage2"):
                self.buttons[phase].setEnabled(False)
            self.notify("تعذر تدقيق الدليل؛ لم يُسمح بالتشغيل: " + str(exc))

    def inspect(self):
        try:
            report = self.experiment.report()
            dialog = QDialog(self)
            dialog.setWindowTitle("مراجعة العينة والميزانية والأدلة — دون اتصال")
            dialog.resize(800, 560)
            layout = QVBoxLayout(dialog)
            text = QTextEdit()
            text.setReadOnly(True)
            text.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            text.setPlainText(json.dumps(report, ensure_ascii=False, indent=2))
            layout.addWidget(text)
            close = QPushButton("إغلاق")
            close.clicked.connect(dialog.accept)
            layout.addWidget(close)
            dialog.exec()
        except Exception as exc:
            self.notify(exc)

    def confirm(self, text):
        return (
            QMessageBox.question(
                self,
                "تفويض طلبات Roblox تجريبية",
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def start(self, phase):
        if self.busy or self.closing or self.owner.busy:
            return
        try:
            token = self.experiment.review(phase)
            manifest = self.experiment.manifest()
            budget = len(self.experiment.plan(phase))
            if not self.confirm(
                f"راجعت العينة والأدلة؟ المرحلة {phase}: {budget} طلبًا متسلسلًا، بفاصل لا يقل عن 10 ثوانٍ.\n"
                f"البصمة: {manifest['fingerprint']}\n"
                "نتائج الحساب ليست إتاحة تسجيل. لن تبدأ مرحلة تالية تلقائيًا، ولن يُعاد إرسال المنقطع.\n"
                "أؤكد مراجعة نتائج المرحلة الأولى قبل المرحلة الثانية وإيقاف أي فاحص خارجي. "
                "هل أفوّض هذه المرحلة فقط؟"
            ):
                return
            self.experiment.cancel.clear()
            self.busy = True
            self.refresh()
            self.notify("جارٍ التنفيذ المتسلسل — إيقاف متاح.")
            future = self.executor.submit(self.experiment.run, phase, token)

            def finished(future):
                try:
                    future.result()
                    error = ""
                except BaseException as exc:
                    error = str(exc)
                self.finished.emit(error)

            future.add_done_callback(finished)
        except Exception as exc:
            self.notify(exc)

    def done(self, error):
        self.busy = False
        self.refresh()
        if error:
            self.notify(error)
        if self.closing:
            self.executor.shutdown(wait=False)
        self.idle.emit()

    def stop(self):
        self.experiment.cancel.set()
        self.notify("طُلب الإيقاف؛ الطلب الجاري محدود بمهلة 10 ثوانٍ ثم يُحفظ الدليل.")

    def export(self, kind):
        path, _ = QFileDialog.getSaveFileName(
            self, "تصدير دليل تجريبي جديد", "", f"{kind.upper()} (*.{kind})"
        )
        if path:
            try:
                self.experiment.export(Path(path), kind)
                self.notify("صُدّر الدليل؛ لا تعديل لملف سابق أو سجل الإنتاج.")
            except Exception as exc:
                self.notify(exc)

    def shutdown(self):
        self.closing = True
        self.experiment.cancel.set()
        self.refresh()
        if not self.busy:
            self.executor.shutdown(wait=False)
