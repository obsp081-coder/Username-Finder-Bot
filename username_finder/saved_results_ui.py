"""Unified saved-result search, reachable from the Job Manager without a new platform."""

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from .platform_workspace import ReadableColumns, ResultsPresentation
from .result_filters_ui import AdvancedResultFilters, ResultQueryController, choice
from .result_search import CLASSES, ResultSearch, SearchCancelled
from .roblox import LABELS as ROBLOX_LABELS
from .ui_helpers import local_time
from .x import LABELS as X_LABELS


class SavedResultsDialog(QDialog):
    exported = Signal(str)

    def __init__(self, window):
        super().__init__(window)
        self.setWindowTitle("البحث في النتائج المحفوظة — Roblox وX")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.resize(1050, 650)
        self.database = window.store.path
        self.offset = 0
        self.export_cancel = threading.Event()
        self.exporting = False
        self.rows = []
        layout = QVBoxLayout(self)
        note = QLabel("أحدث نتيجة لكل اسم ومنصة • الإتاحة أولية ولا تضمن التسجيل • لا طلبات شبكة.")
        note.setWordWrap(True)
        layout.addWidget(note)
        basic = QHBoxLayout()
        self.scope = choice([("كل النتائج المدعومة", "All"), ("Roblox", "Roblox"), ("X", "X")])
        self.classification = choice([("كل التصنيفات", ""), *[(name, name) for name in CLASSES]])
        self.search = QLineEdit()
        self.search.setPlaceholderText("اسم المستخدم")
        self.search.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        for control in (self.scope, self.classification, self.search):
            basic.addWidget(control)
        layout.addLayout(basic)
        self.filters = AdvancedResultFilters("All", self.search, self.classification)
        layout.addWidget(self.filters)
        self.query = ResultQueryController(self)
        self.query.ready.connect(self.show_results)
        self.query.cleared.connect(self.clear_results)
        self.table = window.make_table(
            ["المنصة", "الاسم", "التصنيف الأولي", "آخر فحص", "الملاحظات", "التغير"]
        )
        self.columns = ReadableColumns(self.table, window.fill_table)
        self.table.setMinimumHeight(140)
        self.results_presentation = ResultsPresentation(self.table, (0, 1, 2), "Roblox / X")
        layout.addWidget(self.results_presentation)
        layout.addWidget(self.table, 1)
        self.page_label = QLabel()
        self.page_label.setWordWrap(True)
        layout.addWidget(self.page_label)
        pages = QHBoxLayout()
        self.previous_button, self.next_button = QPushButton("السابق"), QPushButton("التالي")
        self.previous_button.clicked.connect(lambda: self.page(-1))
        self.next_button.clicked.connect(lambda: self.page(1))
        pages.addWidget(self.previous_button)
        pages.addWidget(self.next_button)
        layout.addLayout(pages)
        self.history = QCheckBox("تصدير الملاحظات التاريخية المطابقة بدل أحدث نتيجة فقط")
        self.history.setToolTip("تطبق الشروط على كل ملاحظة تاريخية؛ قد تتكرر الأسماء عند اختيار هذا الخيار.")
        layout.addWidget(self.history)
        actions = QHBoxLayout()
        self.export_button = QPushButton("تصدير جميع المطابقات CSV…")
        self.export_button.clicked.connect(self.export)
        self.stop_export = QPushButton("إلغاء التصدير")
        self.stop_export.setEnabled(False)
        self.stop_export.clicked.connect(self.export_cancel.set)
        close = QPushButton("إغلاق")
        close.clicked.connect(self.reject)
        for button in (self.export_button, self.stop_export, close):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.export_status = QLabel()
        self.export_status.setWordWrap(True)
        layout.addWidget(self.export_status)
        self.exported.connect(self.export_done)
        self.scope.currentIndexChanged.connect(lambda: self.filters.set_scope(self.scope.currentData()))
        self.search.textChanged.connect(self.filters.update_filters)
        self.classification.currentIndexChanged.connect(self.filters.update_filters)
        self.filters.changed.connect(self.filter_changed)
        self.filters.cancel_button.clicked.connect(lambda: self.query.cancel())
        self.finished.connect(self.shutdown)
        self.refresh()

    def clear_results(self, message):
        self.rows = []
        self.columns.fill([])
        self.page_label.setText(message)
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)

    def filter_changed(self):
        self.offset = 0
        self.refresh()

    def refresh(self):
        try:
            filters = self.filters.filters()
        except ValueError as exc:
            self.query.cancel(str(exc))
            return
        self.query.request(self.database, self.scope.currentData(), filters, self.offset)

    def show_results(self, rows, total, offset, counts=None):
        self.rows, self.offset = rows, offset
        self.columns.fill(
            [
                [
                    row["platform"],
                    row["username"],
                    (ROBLOX_LABELS if row["platform"] == "Roblox" else X_LABELS)[row["classification"]],
                    local_time(row["checked_at"]),
                    row["completed_checks"],
                    f"{row['previous_classification']} → {row['classification']}" if row["changed"] else "—",
                ]
                for row in rows
            ]
        )
        self.page_label.setText(
            f"{offset + 1 if total else 0}–{offset + len(rows)} من {total:,}"
            if total
            else "لا نتائج مطابقة؛ جرّب إعادة التصفية أو توسيع النطاق."
        )
        self.previous_button.setEnabled(offset > 0)
        self.next_button.setEnabled(offset + len(rows) < total)

    def page(self, direction):
        self.offset = max(0, self.offset + direction * 100)
        self.refresh()

    def export(self):
        if self.exporting:
            return
        try:
            filters = self.filters.filters()
        except ValueError as exc:
            self.export_status.setText(str(exc))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "تصدير النتائج المحفوظة", "saved-results.csv", "CSV (*.csv)"
        )
        if not path:
            return
        scope, history = self.scope.currentData(), self.history.isChecked()
        self.export_cancel.clear()
        self.exporting = True
        self.export_button.setEnabled(False)
        self.stop_export.setEnabled(True)
        self.export_status.setText("جارٍ تصدير جميع المطابقات؛ يحتفظ التصدير بشروط لحظة البدء.")

        def work():
            try:
                count = ResultSearch(self.database).export(
                    path, scope, filters, history=history, cancel=self.export_cancel
                )
                message = f"صُدّرت {count:,} ملاحظة أولية مع الدليل ومعرّف المنصة عند البحث الموحّد."
            except SearchCancelled:
                message = "أُلغي التصدير؛ لم يُستبدل ملف الوجهة."
            except Exception as exc:
                message = "تعذر التصدير: " + str(exc)
            try:
                self.exported.emit(message)
            except RuntimeError:
                pass

        self.query.executor.submit(work)

    def export_done(self, message):
        self.exporting = False
        self.export_status.setText(message)
        self.export_button.setEnabled(True)
        self.stop_export.setEnabled(False)

    def shutdown(self):
        self.export_cancel.set()
        self.query.shutdown()
