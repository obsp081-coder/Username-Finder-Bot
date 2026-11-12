"""Independent Arabic RTL Roblox workflow, backed by the shared SQLite store."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .overnight_ui import OvernightControls
from .platform_workspace import PlatformWorkspace, ReadableColumns, ResultsPresentation, generation_fields
from .result_filters_ui import AdvancedResultFilters, ResultQueryController
from .result_search import ResultSearch
from .roblox import LABELS, generation_range_spec, input_spec, prepare_inputs
from .roblox_bulk_ui import BulkPanel
from .roblox_concurrent import configure_execution, execution_policy
from .roblox_prefilter_ui import PrefilterPanel
from .roblox_restriction import (
    RestrictionDiagnostic,
    checking_permission,
    diagnostic_summary,
    restriction_status,
)
from .roblox_runner import RobloxRunner
from .roblox_storage import CHECK_INTERVALS, RobloxStore
from .ui_cards import Card, CardGrid, Disclosure, ModeStrip, caption, icon
from .ui_helpers import local_time, style_combo_popup

STATES = {
    "Ready": "جاهزة",
    "Running": "جارٍ الفحص",
    "Paused": "متوقفة — قابلة للاستئناف",
    "Restricted": "متوقفة بسبب قيد أو جواب مبهم",
    "Error": "تعذر إكمال العملية",
    "Completed": "مكتملة",
    "Preparing": "جارٍ تحضير الإدخال محليًا",
    "PreparationPaused": "التحضير متوقف — قابل للاستئناف",
    "PreparationError": "التحضير غير مكتمل — لم يبدأ الفحص",
}


class QuantityEdit(QLineEdit):
    """Decimal quantities are not limited by Qt's signed 32-bit spin box."""

    def setValue(self, value):
        self.setText(str(value))

    def value(self):
        try:
            return int(self.text())
        except ValueError as exc:
            raise ValueError("أدخل كمية صحيحة موجبة.") from exc


class RobloxPanel(QScrollArea):
    idle = Signal()
    completed = Signal(object, object, str)
    progress_changed = Signal()
    preparation_progress = Signal(object)

    @property
    def busy(self):
        return (
            self._busy
            or (hasattr(self, "bulk") and self.bulk.busy)
            or (hasattr(self, "prefilter") and self.prefilter.busy)
        )

    @busy.setter
    def busy(self, value):
        self._busy = value

    def __init__(self, store, make_table, fill_table, *, fetcher=None):
        super().__init__()
        self.repo = RobloxStore(store)
        self.repo.recover()
        self.make_table, self.fill_table = make_table, fill_table
        self.fetcher = fetcher
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="roblox")
        self.runner = None
        self.last_execution = None
        self.diagnostic = None
        self._restriction_automatic = False
        self.busy = self.closing = False
        self.stopping = False
        self.current_run = None
        self.view_progress = (None, None)
        self.preparation_cancel = threading.Event()
        self.preparing = False
        self.preparation_progress.connect(self.preparation_updated)
        self.offset = 0
        self.rows = []
        self.completed.connect(self._finished)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(100)
        self.refresh_timer.timeout.connect(self.refresh)
        self.progress_changed.connect(self.queue_refresh)
        self.setWidgetResizable(True)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(9)
        header = QHBoxLayout()
        platform_icon = QLabel()
        platform_icon.setPixmap(icon("roblox", size=42).pixmap(42, 42))
        header.addWidget(platform_icon)
        headings = QVBoxLayout()
        headings.setSpacing(3)
        headings.addWidget(caption("Roblox · فحص الأسماء", "pageTitle"))
        headings.addWidget(
            caption("نتائج التسجيل أولية ولا تضمن الإتاحة • لا نطلب بيانات دخول.", "pageSubtitle")
        )
        header.addLayout(headings, 1)
        layout.addLayout(header)
        restriction_actions = QHBoxLayout()
        self.restriction_button = self._button("تحقق من القيد", self.check_restriction)
        self.diagnostic_button = self._button("تشخيص بطلب واحد…", self.start_diagnostic)
        self.diagnostic_button.setEnabled(False)
        self.diagnostic_cancel_button = self._button("إيقاف التشخيص", self.stop)
        self.diagnostic_cancel_button.hide()
        restriction_actions.addWidget(self.restriction_button)
        restriction_actions.addWidget(self.diagnostic_button)
        restriction_actions.addWidget(self.diagnostic_cancel_button)
        restriction_actions.addStretch()
        layout.addLayout(restriction_actions)
        self.restriction_label = QLabel()
        self.restriction_label.setWordWrap(True)
        self.restriction_label.setTextFormat(Qt.TextFormat.PlainText)
        self.restriction_label.hide()
        layout.addWidget(self.restriction_label)
        self.activity_status = caption("جاهز • عمل واحد لكل منصة؛ يمكن تشغيل Roblox وX معًا.", "statusPill")
        root_layout = layout
        self.workspace = PlatformWorkspace()
        root_layout.addWidget(self.workspace, 1)
        input_layout = self.workspace.add_page("الأسماء والإعدادات")
        results_layout = self.workspace.add_page("النتائج", expand=True)
        history_layout = self.workspace.add_page("العمليات والسجل")
        self.workspace.currentChanged.connect(lambda _: self.queue_refresh())
        self.overnight = OvernightControls(self, "Roblox")
        history_layout.addWidget(self.overnight)
        input_card = Card("الأسماء · توليد أو استيراد", "users")
        settings_card = Card("إعداد الفحص", "clock")
        self.input_cards = CardGrid((input_card, settings_card), columns=(2, 1), minimum=350)
        input_layout.addWidget(self.input_cards)
        layout = input_card.body
        self.modes = QTabWidget()
        generated = QWidget()
        form = QVBoxLayout(generated)
        self.minimum_length, self.maximum_length, self.quantity = QSpinBox(), QSpinBox(), QuantityEdit()
        for control in (self.minimum_length, self.maximum_length):
            control.setRange(3, 20)
            control.setValue(8)
        self.quantity.setValue(20)
        for widget in (self.minimum_length, self.maximum_length, self.quantity):
            widget.setMaximumWidth(200)
        form.addWidget(
            generation_fields(self.minimum_length, self.maximum_length, self.quantity),
            alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute,
        )
        form.addWidget(caption("توليد محلي دون تكرار؛ تبدأ الأسماء بحرف وتتضمن حروفًا وأرقامًا."))
        self.modes.addTab(generated, "توليد أسماء")
        imported = QWidget()
        inputs = QVBoxLayout(imported)
        self.manual = QTextEdit()
        self.manual.setAcceptRichText(False)
        self.manual.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.manual.setPlaceholderText("اسم واحد في كل سطر؛ من 3 إلى 20 محرفًا")
        self.manual.setFixedHeight(100)
        inputs.addWidget(self.manual)
        self.files = QListWidget()
        self.files.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.files.setMaximumHeight(75)
        inputs.addWidget(self.files)
        file_actions = QHBoxLayout()
        self.add_files_button = self._button("إضافة TXT / CSV…", self.add_files)
        self.remove_file_button = self._button("إزالة المحدد", self.remove_file)
        file_actions.addWidget(self.add_files_button)
        file_actions.addWidget(self.remove_file_button)
        file_actions.addStretch()
        inputs.addLayout(file_actions)
        self.modes.addTab(imported, "إدخال واستيراد أسماء")
        self.modes.currentChanged.connect(self._fit_mode)
        self.files.setVisible(False)
        layout.addWidget(self.modes)
        layout = settings_card.body
        self.skip_previous = QCheckBox("تجاوز المفحوص سابقًا")
        self.skip_previous.setToolTip("Skip Previously Checked — لا نعيد إرسال اسم موجود في سجل Roblox.")
        self.skip_previous.setChecked(True)
        layout.addWidget(self.skip_previous)
        speed_form = QFormLayout()
        speed_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        speed_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.speed = QComboBox()
        self.speed.setMaximumWidth(340)
        for label, seconds in zip(
            (
                "5 ثوانٍ (محافظ / افتراضي)",
                "2 ثانية",
                "1 ثانية",
                "0.5 ثانية (سريع)",
                "0 ثانية (تجريبي / دون تأخير إضافي)",
            ),
            CHECK_INTERVALS,
            strict=True,
        ):
            self.speed.addItem(label, seconds)
        style_combo_popup(self.speed)
        self.speed.setToolTip("للعمليات الجديدة فقط؛ تحتفظ العملية المحفوظة بسرعتها عند الاستئناف.")
        speed_form.addRow("سرعة الفحص", self.speed)
        layout.addLayout(speed_form)
        self.parallel = QCheckBox("تفعيل فحص فردي متزامن محدود (اختياري)")
        layout.addWidget(self.parallel)
        parallel_form = QFormLayout()
        self.workers = QSpinBox()
        self.workers.setRange(2, 8)
        self.workers.setValue(2)
        self.request_rate = QComboBox()
        for rate in (0.25, 0.5, 1.0, 2.0, 5.0):
            self.request_rate.addItem(f"{rate:g} طلب/ثانية إجمالًا", rate)
        self.request_rate.setCurrentIndex(2)
        style_combo_popup(self.request_rate)
        parallel_form.addRow("عدد العمال", self.workers)
        parallel_form.addRow("سقف الإرسال المشترك", self.request_rate)
        layout.addLayout(parallel_form)
        parallel_note = QLabel(
            "التوازي يتطلب فاصل 0 ثانية ودون استرداد ليلي؛ الإعدادات تحفظ للعملية. "
            "القيم حدود محلية وليست حصة موفر مثبتة. القيود المحفوظة تبقى نافذة."
        )
        parallel_note.setWordWrap(True)
        layout.addWidget(parallel_note)
        self.parallel.toggled.connect(self.controls)
        self.execution_status = QLabel()
        self.execution_status.setWordWrap(True)
        history_layout.addWidget(self.execution_status)
        self.speed_warning = QLabel(
            "سرعة تجريبية: نجحت الاختبارات القصيرة، لكن استقرار التشغيل الطويل غير مضمون.\n"
            "Experimental speed. Short tests succeeded, but long-running stability is not guaranteed."
        )
        self.speed_warning.setWordWrap(True)
        self.speed_warning.setObjectName("simulation")
        layout.addWidget(self.speed_warning)
        self.effective_speed = QLabel()
        self.effective_speed.setWordWrap(True)
        history_layout.addWidget(self.effective_speed)
        self.speed.currentIndexChanged.connect(self.speed_changed)
        self.preview_label = QLabel("اضغط المعاينة لتقييم الصيغة والتكرار والسجل محليًا دون اتصال.")
        self.preview_label.setTextFormat(Qt.TextFormat.PlainText)
        self.preview_label.setWordWrap(True)
        preview_card = Card("المعاينة المحلية", "check")
        preview_card.body.addWidget(self.preview_label)
        input_layout.addWidget(preview_card)
        input_layout.addStretch()
        actions = QGridLayout()
        self.preview_button = self._button("معاينة محلية", self.preview)
        self.start_button = self._button("بدء الفحص", self.start)
        self.start_button.setObjectName("primary")
        self.stop_button = self._button("إيقاف وحفظ التقدم", self.stop)
        self.resume_button = self._button("استئناف العملية المختارة", self.resume)
        for i, widget in enumerate(
            (self.preview_button, self.start_button, self.stop_button, self.resume_button)
        ):
            actions.addWidget(widget, 0, i)
        self.individual_actions = QWidget()
        self.individual_actions.setLayout(actions)
        root_layout.addWidget(self.individual_actions)
        root_layout.addWidget(self.activity_status)
        self.status = QLabel("جاهز — لا توجد طلبات تلقائية.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        history_layout.addWidget(self.status)
        self.operations = QComboBox()
        style_combo_popup(self.operations)
        self.operations.currentIndexChanged.connect(self.select_operation)
        history_layout.addWidget(self.operations)
        history_group = QGroupBox("السجل المستقل وإعادة الفحص")
        history_actions = QGridLayout(history_group)
        self.recheck_all_button = self._button("إعادة فحص كل الأسماء السابقة", self.recheck_all)
        self.import_history_button = self._button("استيراد نتائج التجربة CSV…", self.import_history)
        self.history_button = self._button("عرض تاريخ الاسم المحدد", self.show_history)
        self.recheck_selected_button = self._button("إعادة فحص الاسم المحدد", self.recheck_selected)
        for i, widget in enumerate(
            (
                self.recheck_all_button,
                self.import_history_button,
                self.history_button,
                self.recheck_selected_button,
            )
        ):
            history_actions.addWidget(widget, i // 2, i % 2)
        history_layout.addWidget(history_group)
        history_layout.addStretch()
        layout = results_layout
        self.counters = QLabel()
        self.counters.setWordWrap(True)
        layout.addWidget(self.counters)
        filters = QHBoxLayout()
        self.class_filter = QComboBox()
        self.class_filter.addItem("كل التصنيفات", "")
        for key, label in LABELS.items():
            self.class_filter.addItem(label, key)
        style_combo_popup(self.class_filter)
        self.search = QLineEdit()
        self.search.setPlaceholderText("تصفية باسم المستخدم")
        self.search.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.search.setMaximumWidth(300)
        filters.addWidget(self.class_filter)
        filters.addWidget(self.search, 1)
        layout.addLayout(filters)
        self.changes_only = QCheckBox("الأسماء التي تغير آخر تصنيف لها فقط")
        self.advanced_filters = AdvancedResultFilters(
            "Roblox", self.search, self.class_filter, self.changes_only
        )
        self.result_table = make_table(
            ["اسم المستخدم", "التصنيف الأولي", "كود API", "آخر فحص", "الفحوص المكتملة", "التغيير", "المصدر"]
        )
        self.result_table.setMinimumHeight(150)
        self.result_columns = ReadableColumns(self.result_table, self.fill_table, fill_width=True)
        preview_results = Card("أحدث النتائج المعروضة", "file")
        self.input_results = make_table(["اسم المستخدم", "التصنيف الأولي", "آخر فحص"])
        self.input_results.setMaximumHeight(150)
        self.input_result_columns = ReadableColumns(self.input_results, self.fill_table, fill_width=True)
        preview_results.body.addWidget(self.input_results)
        self.input_results_empty = caption("لا توجد نتائج معروضة بعد. يظل البدء إجراءً صريحًا من المستخدم.")
        preview_results.body.addWidget(self.input_results_empty)
        open_results = self._button("فتح النتائج والتصفية ←", lambda: self.workspace.setCurrentIndex(1))
        open_results.setObjectName("textButton")
        preview_results.body.addWidget(open_results)
        input_layout.insertWidget(input_layout.count() - 1, preview_results)
        self.result_query = ResultQueryController(self)
        self.result_query.ready.connect(self._show_results)
        self.result_query.cleared.connect(self._clear_results)
        self.advanced_filters.changed.connect(self.filter_changed)
        self.advanced_filters.cancel_button.clicked.connect(lambda: self.result_query.cancel())
        self.result_table.cellDoubleClicked.connect(lambda *_: self.show_history())
        self.results_presentation = ResultsPresentation(self.result_table, (0, 1), "Roblox")
        layout.addWidget(self.results_presentation)
        layout.addWidget(self.result_table)
        pages = QHBoxLayout()
        self.previous_button = self._button("السابق", lambda: self.page(-1))
        self.next_button = self._button("التالي", lambda: self.page(1))
        self.page_label = QLabel()
        pages.addWidget(self.previous_button)
        pages.addWidget(self.page_label, 1)
        pages.addWidget(self.next_button)
        layout.addLayout(pages)
        self.results_options = Disclosure("التصفية المتقدمة والتصدير")
        layout.addWidget(self.results_options)
        layout = self.results_options.body
        layout.addWidget(self.changes_only)
        layout.addWidget(self.advanced_filters)
        self.export_history = QCheckBox("تضمين جميع الملاحظات التاريخية في التصدير")
        self.export_history.setToolTip("عند تفعيله: تطبق الشروط على كل ملاحظة تاريخية بنفسها.")
        self.export_button = self._button("تصدير CSV حسب التصفية…", self.export)
        layout.addWidget(self.export_history)
        layout.addWidget(self.export_button)
        self.setWidget(content)
        self.checking_mode = QComboBox()
        self.checking_mode.addItems(["الفحص الفردي (الدقيق)", "البحث الجماعي عن الحسابات Bulk (تجريبي)"])
        self.checking_mode.addItem("تصفية الملفات الكبيرة Bulk — دليل حسابات")
        style_combo_popup(self.checking_mode)
        self.mode_strip = ModeStrip(
            self.checking_mode,
            (
                ("الفحص الفردي", "check"),
                ("Bulk المجمّد", "layers"),
                ("تصفية الملفات الكبيرة", "filter"),
            ),
        )
        self.checking_mode.hide()
        root_layout.insertWidget(1, self.mode_strip)
        self.bulk = BulkPanel(self)
        self.bulk.idle.connect(self.idle.emit)
        root_layout.addWidget(self.bulk)
        self.bulk.hide()
        self.prefilter = PrefilterPanel(self)
        self.prefilter.idle.connect(self.idle.emit)
        root_layout.addWidget(self.prefilter)
        self.prefilter.hide()
        self.checking_mode.currentIndexChanged.connect(self.change_checking_mode)
        for signal in (
            self.class_filter.currentIndexChanged,
            self.search.textChanged,
            self.changes_only.toggled,
        ):
            signal.connect(self.advanced_filters.update_filters)
        self.refresh()

    def change_checking_mode(self, index):
        self.workspace.setVisible(index == 0)
        self.individual_actions.setVisible(index == 0)
        self.bulk.setVisible(index == 1)
        self.prefilter.setVisible(index == 2)
        self.activity_status.setVisible(index == 0)
        self.bulk.refresh()
        self.controls()

    @staticmethod
    def _button(label, callback):
        widget = QPushButton(label)
        widget.clicked.connect(callback)
        return widget

    def _fit_mode(self):
        height = self.modes.currentWidget().sizeHint().height() + self.modes.tabBar().sizeHint().height() + 12
        self.modes.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_mode()

    def _submit(self, function, callback):
        if self.busy or self.closing:
            return
        self.busy = True
        self.stopping = False
        self.controls()
        future = self.executor.submit(function)

        def done(result):
            try:
                value, error = result.result(), ""
            except Exception as exc:
                value = None
                error = (
                    str(exc) if isinstance(exc, ValueError) else f"تعذر إتمام العملية ({type(exc).__name__})."
                )
            self.completed.emit(callback, value, error)

        future.add_done_callback(done)

    def _finished(self, callback, value, error):
        was_diagnostic = self.diagnostic is not None
        self.busy = False
        self.stopping = False
        self.preparing = False
        if self.runner is not None and self.runner.execution is not None:
            self.last_execution = (self.current_run, self.runner.execution.snapshot())
        self.runner = None
        self.diagnostic = None
        if error and not self.closing:
            if was_diagnostic:
                self.restriction_label.setText(error + "\nتعذر إكمال التشخيص؛ لا إعادة تلقائية.")
            self.status.setText(error)
            self.refresh()
            self.status.setText(error)
            self.activity_status.setText(error)
        elif not self.closing:
            callback(value)
        if not self.busy:
            self.bulk.refresh()
            self.controls()
            if self.closing:
                self.executor.shutdown(wait=False)
            self.idle.emit()

    def controls(self):
        if self.closing:
            for button in self.findChildren(QPushButton):
                button.setEnabled(False)
            self.checking_mode.setEnabled(False)
            return
        permission = checking_permission(self.repo, purpose="individual")
        dispatch_allowed = permission["allowed"] and not self.busy and not self.closing
        self.diagnostic_cancel_button.setVisible(self.diagnostic is not None)
        self.diagnostic_cancel_button.setEnabled(self.diagnostic is not None and not self.closing)
        self.restriction_button.setEnabled(not self.closing)
        self.diagnostic_button.setEnabled(False)  # A fresh status review is required after each action.
        self.checking_mode.setEnabled(not self.busy and not self.closing)
        for widget in (
            self.modes,
            self.speed,
            self.parallel,
            self.skip_previous,
            self.preview_button,
            self.start_button,
            self.recheck_all_button,
            self.import_history_button,
            self.recheck_selected_button,
            self.export_button,
            self.operations,
        ):
            widget.setEnabled(not self.busy and not self.closing)
        for widget in (self.workers, self.request_rate):
            widget.setEnabled(self.parallel.isChecked() and not self.busy and not self.closing)
        if hasattr(self, "overnight"):
            concurrent = (
                self.runner is not None and execution_policy(self.repo, self.current_run)["workers"] > 1
            )
            self.overnight.enabled.setEnabled(not concurrent and not self.closing)
        self.stop_button.setEnabled(
            self.busy and (self.runner is not None or self.preparing or self.diagnostic is not None)
        )
        for widget in (self.start_button, self.recheck_all_button, self.recheck_selected_button):
            widget.setEnabled(dispatch_allowed)
            widget.setToolTip(permission["reason"])
        if hasattr(self, "bulk"):
            self.bulk.gate_dispatch()
        if hasattr(self, "prefilter"):
            self.prefilter.controls()
        if not self.busy:
            if not permission["allowed"] and (
                self._restriction_automatic or not self.restriction_label.text()
            ):
                self.restriction_label.setText(permission["reason"] + "\nحالة الموفر الآن غير معروفة.")
                self.restriction_label.show()
                self._restriction_automatic = True
            elif permission["allowed"] and self._restriction_automatic:
                self.restriction_label.clear()
                self.restriction_label.hide()
                self._restriction_automatic = False
        if self.busy and self.preparing:
            self.resume_button.setEnabled(False)
            return
        run = self.repo.progress(self.current_run) if self.current_run else None
        local_preparation = run and run["state"] in ("Preparing", "PreparationPaused", "PreparationError")
        self.resume_button.setText(
            "استئناف التحضير المحلي"
            if run and run["state"] in ("Preparing", "PreparationPaused", "PreparationError")
            else "استئناف العملية المختارة"
        )
        self.resume_button.setEnabled(
            not self.busy
            and not self.closing
            and run is not None
            and run["state"] != "Completed"
            and (permission["allowed"] or local_preparation)
        )

    def speed_changed(self):
        self.repo.set_preferred_interval(self.speed.currentData())
        self.show_effective_speed()

    def check_restriction(self):
        self._restriction_automatic = False
        status = restriction_status(self.repo)
        self.restriction_label.show()
        prefix = "واجهة Roblox مشغولة؛ التشخيص غير متاح حتى انتهاء العملية.\n" if self.busy else ""
        self.restriction_label.setText(prefix + status["text"])
        self.bulk.refresh()
        self.controls()
        self.diagnostic_button.setEnabled(status["allowed"] and not self.busy and not self.closing)

    def allow_dispatch(self):
        if self.busy or self.closing:
            return False
        permission = checking_permission(self.repo, purpose="individual")
        if not permission["allowed"]:
            self.restriction_label.setText(
                permission["reason"] + "\nلم يُرسل طلب. حالة الموفر الآن غير معروفة."
            )
            self.restriction_label.show()
            self.controls()
        return permission["allowed"]

    def start_diagnostic(self):
        if self.busy or self.closing:
            return
        self.check_restriction()
        if not self.diagnostic_button.isEnabled():
            return
        username, accepted = QInputDialog.getText(self, "تشخيص Roblox", "اسم واحد صالح للصيغة:")
        if not accepted or not self._confirm(
            "إرسال طلب Roblox واحد لهذا الاسم وحفظ دليله في السجل؟ لا إعادة تلقائية، "
            "ونجاحه لا يثبت زوال القيود أو سعة مستدامة."
        ):
            return
        if self.busy or self.closing:
            return
        self.diagnostic = RestrictionDiagnostic(self.repo, fetcher=self.fetcher)
        diagnostic = self.diagnostic
        self.restriction_label.setText("جارٍ تشخيص واحد؛ يمكن إيقافه. لا طلبات إضافية تلقائية.")

        def work():
            try:
                identity = diagnostic.run(username.strip())
                return diagnostic_summary(self.repo, identity)
            except ValueError:
                return restriction_status(self.repo)["text"] + "\nلم يبدأ التشخيص؛ راجع القيد وصيغة الاسم."

        def ready(text):
            self.refresh()
            self.restriction_label.setText(text + "\n" + restriction_status(self.repo)["text"])

        self._submit(work, ready)

    def show_effective_speed(self):
        run = self.repo.progress(self.current_run) if self.current_run else None
        seconds = run["interval_seconds"] if run else self.speed.currentData()
        self.speed_warning.setVisible(seconds == 0 or self.speed.currentData() == 0)
        prefix = "سرعة العملية المحفوظة (وعند الاستئناف)" if run else "سرعة العمليات الجديدة"
        policy = execution_policy(self.repo, self.current_run)
        if policy["workers"] > 1:
            self.effective_speed.setText(
                f"{prefix}: {policy['workers']} عمال، سقف مشترك {policy['rate']:g} طلب/ث. "
                "بدء يدوي فقط؛ أي قيد يوقف الإرسال ويتطلب مراجعة."
            )
            return
        self.effective_speed.setText(
            f"{prefix}: {seconds:g} ثانية بعد الاستجابة، دون توازٍ. تسري أي مهلة أطول يفرضها الخادم."
        )

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "ملفات أسماء Roblox", "", "TXT / CSV (*.txt *.csv)")
        existing = {self.files.item(i).text() for i in range(self.files.count())}
        for path in paths:
            if path not in existing:
                self.files.addItem(path)
                existing.add(path)
        self.files.setVisible(self.files.count() > 0)
        self._fit_mode()

    def remove_file(self):
        self.files.takeItem(self.files.currentRow())
        self.files.setVisible(self.files.count() > 0)
        self._fit_mode()

    def prepare(self, start_after=False, *, save_job=False):
        if start_after and not self.allow_dispatch():
            return
        if self.busy or self.closing:
            return
        generated = self.modes.currentIndex() == 0
        minimum, maximum = self.minimum_length.value(), self.maximum_length.value()
        quantity = self.quantity.text()
        manual = self.manual.toPlainText()
        paths = tuple(self.files.item(i).text() for i in range(self.files.count()))
        skip = self.skip_previous.isChecked()
        self.preparation_cancel.clear()
        self.preparing = True
        self.activity_status.setText("جارٍ تحضير الأسماء محليًا…")

        def work():
            spec = (
                {"generation": generation_range_spec(minimum, maximum, int(quantity))}
                if generated
                else input_spec(manual, paths)
            )
            if start_after or save_job:
                run = self.repo.create_run(
                    spec=spec, skip=skip, cancel=self.preparation_cancel, notify=self.preparation_notice
                )
                return None, 0, run
            prepared = prepare_inputs(
                spec=spec,
                cancel=self.preparation_cancel,
                known=self.repo.is_known if skip else lambda _: False,
                preview=True,
            )
            return prepared, prepared.skipped, None

        def ready(value):
            prepared, skipped, run = value
            if run:
                self.current_run = run
                self.overnight.arm(run)
                self.refresh()
                if start_after and self.repo.progress(run)["state"] == "Ready":
                    self._start_run(run)
                elif not start_after and self.repo.progress(run)["state"] == "Ready":
                    self.activity_status.setText("حُضّرت العملية محليًا؛ افحص جاهزيتها ثم ابدأها صراحةً.")
                else:
                    self.activity_status.setText("التحضير غير مكتمل؛ حُفظت الدفعات ويمكن استئنافه دون طلبات.")
                return
            examples = "، ".join(f"{name} (الطول {len(name)})" for name in prepared.names)
            invalid = "\n".join(f"{n}: {reason}" for n, reason in prepared.invalid[:5])
            self.preview_label.setText(
                (
                    "معاينة كاملة للإدخال الصغير • "
                    if prepared.complete
                    else f"عينة جزئية من أول {prepared.scanned:,} سجل؛ العدد الكلي غير محسوب • "
                )
                + f"صالح نحويًا: {prepared.total} • مكرر: {prepared.duplicates} • "
                f"غير صالح: {prepared.invalid_count} • فارغ: {prepared.blank} • من السجل سيُتجاوز: {skipped}\n"
                f"{examples}\n{invalid}"
            )
            self.activity_status.setText(
                "معاينة محلية فقط؛ لم يُستورد الملف أو يبدأ الفحص. حضّر عملية لحساب جميع الأسماء."
            )
            if not prepared.names:
                self.status.setText(
                    "لا توجد أسماء صالحة للفحص؛ راجع الإدخال أو الملف الفارغ."
                    if prepared.complete
                    else "لم يظهر اسم صالح في العينة؛ لم يُفحص باقي الملف."
                )
                self.activity_status.setText(self.status.text())

        self._submit(work, ready)

    def preview(self):
        self.prepare()

    def start(self):
        self.prepare(start_after=True)

    def _start_run(self, identity, *, automatic=False):
        if not self.allow_dispatch():
            return
        try:
            policy = execution_policy(self.repo, identity)
            saved = self.repo.control().get("individual_execution", {}).get(identity)
            if not automatic and saved is None and self.parallel.isChecked():
                configure_execution(
                    self.repo, identity, workers=self.workers.value(), rate=self.request_rate.currentData()
                )
            policy = execution_policy(self.repo, identity)
            if policy["workers"] > 1 and not self.parallel.isChecked():
                raise ValueError("يلزم تفعيل خيار التوازي صراحةً لاستئناف هذه العملية بإعداداتها المحفوظة.")
            if policy["workers"] > 1 and (
                automatic
                or self.overnight.enabled.isChecked()
                or (self.overnight.repo.get(identity) or {}).get("enabled")
            ):
                raise ValueError("التوازي يحتاج بدءًا يدويًا دون استرداد ليلي أو إعادة محاولات.")
        except (ValueError, TypeError) as exc:
            self.status.setText(str(exc))
            return
        self.current_run = identity
        self.last_execution = None
        self.overnight.arm(identity)
        self.runner = RobloxRunner(self.repo, fetcher=self.fetcher, updated=self.progress_changed.emit)
        runner = self.runner
        self.show_effective_speed()
        description = (
            f"فحص فردي متزامن: {policy['workers']} عمال، سقف {policy['rate']:g} طلب/ث"
            if policy["workers"] > 1
            else "فحص متسلسل بالسرعة المحفوظة"
        )
        self.status.setText(description)
        self.activity_status.setText("Roblox • " + description)
        self._submit(
            lambda: runner.run(identity, automatic=True) if automatic else runner.run(identity),
            lambda _: self.refresh(),
        )

    def stop(self, *, manual=True):
        if self.diagnostic is not None:
            self.diagnostic.cancel.set()
            self.restriction_label.setText("جارٍ إيقاف التشخيص وحفظ الدليل؛ قد ينتظر الطلب مهلة الاتصال.")
            return
        if self.prefilter.busy:
            self.prefilter.stop()
            return
        if self.bulk.busy:
            self.bulk.stop()
            return
        self.stopping = self.busy
        self.preparation_cancel.set()
        if self.runner:
            if manual:
                self.runner.manual_stop.set()
            self.runner.cancel.set()
            self.status.setText("جارٍ الإيقاف وحفظ النتيجة الحالية؛ قد ينتظر الطلب مهلة الاتصال.")
        self.activity_status.setText("جارٍ إيقاف Roblox وحفظ النتائج؛ الطلب الجاري قد ينتظر مهلة الاتصال.")
        if manual and self.current_run and not self.preparing:
            try:
                self.overnight.repo.stop(self.current_run, timeout_ms=0)
            except sqlite3.Error:
                self.activity_status.setText(
                    "أُرسل الإلغاء؛ حفظ الإيقاف لم يُؤكّد بعد بسبب تعذر الكتابة. انتظر تأكيده."
                )

    def resume(self):
        if self.current_run and not self.busy and not self.closing:
            run = self.repo.progress(self.current_run)
            if run["state"] in {"PreparationPaused", "PreparationError", "Preparing"}:
                self.preparation_cancel.clear()
                self.preparing = True
                identity = self.current_run
                self._submit(
                    lambda: self.repo.create_run(
                        run_id=identity, cancel=self.preparation_cancel, notify=self.preparation_notice
                    ),
                    self.preparation_ready,
                )
            else:
                self._start_run(self.current_run)

    def preparation_ready(self, identity, *, start_after=False):
        self.current_run = identity
        self.overnight.arm(identity)
        self.refresh()
        if start_after and self.repo.progress(identity)["state"] == "Ready":
            self._start_run(identity)
        elif self.repo.progress(identity)["state"] == "Ready":
            self.activity_status.setText("استُكمل التحضير المحلي؛ ابدأ الفحص صراحةً بعد مراجعة الجاهزية.")
        else:
            self.activity_status.setText("التحضير غير مكتمل؛ حُفظت الدفعات ويمكن استئنافه دون طلبات.")

    def preparation_notice(self, identity):
        tick = time.monotonic()
        if identity != getattr(self, "_last_preparation_identity", None) or (
            tick - getattr(self, "_last_preparation_notice", 0) >= 0.2
        ):
            self._last_preparation_identity = identity
            self._last_preparation_notice = tick
            self.preparation_progress.emit(self.repo.preparation_progress(identity))

    def preparation_updated(self, data):
        self.current_run = data["id"]
        self._preparation_snapshot = data
        self.activity_status.setText(
            f"جارٍ التحضير محليًا • قُرئ: {data['input_cursor']:,} • مكرر: {data['duplicates']:,} • "
            f"غير صالح: {data['invalid']:,} • من السجل: {data['skipped']:,} • لم تُرسل طلبات."
        )
        self.status.setText("العدد النهائي بعد اكتمال التحضير؛ يمكن الإيقاف وحفظ الدفعات المعتمدة.")

    def _confirm(self, text):
        return (
            QMessageBox.question(
                self,
                "تأكيد إعادة فحص Roblox",
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def recheck_all(self):
        if not self.allow_dispatch():
            return
        count = self.repo.known_count()
        if not count:
            self.status.setText("لا توجد أسماء Roblox سابقة لإعادة فحصها.")
            return
        if self._confirm(
            f"إعادة فحص {count:,} اسم مختلف مرة واحدة؟ سيُتجاهل تجاوز السجل لهذه العملية فقط، "
            "وتُحفظ كل النتائج السابقة. لا تعني الإتاحة ضمان التسجيل."
        ):
            self.preparation_cancel.clear()
            self.preparing = True
            self._submit(
                lambda: self.repo.create_run(
                    kind="recheck_all", cancel=self.preparation_cancel, notify=self.preparation_notice
                ),
                lambda identity: self.preparation_ready(identity, start_after=True),
            )

    def selected(self):
        index = self.result_table.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def recheck_selected(self):
        if not self.allow_dispatch():
            return
        row = self.selected()
        if row and not self.busy and self._confirm(f"إعادة فحص {row['username']} مع حفظ تاريخه؟"):
            self._submit(
                lambda: self.repo.create_run([row["username"]], kind="recheck_selected"), self._start_run
            )

    def import_history(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "استيراد نتائج Roblox المسجلة دون اتصال", "", "CSV (*.csv)"
        )
        if path:
            self.preparation_cancel.clear()
            self.preparing = True

            def ready(value):
                self.refresh()
                self.status.setText(
                    f"استُوردت {value['imported']} نتيجة مسجلة؛ "
                    f"تُجاوزت {value['duplicates']} مكررة. لم تُرسل طلبات جديدة."
                )
                self.activity_status.setText(self.status.text())

            self._submit(lambda: self.repo.import_csv(path, cancel=self.preparation_cancel), ready)

    def select_operation(self):
        self.current_run = self.operations.currentData()
        self.refresh()

    def filter_changed(self):
        self.offset = 0
        self.refresh()

    def page(self, direction):
        self.offset = max(0, self.offset + direction * 100)
        self.refresh()

    def _clear_results(self, message):
        self._result_selection = self.selected() or getattr(self, "_result_selection", None)
        self.rows = []
        self.result_columns.fill([])
        self.page_label.setText(message)
        self.counters.setText(message)
        self.input_result_columns.fill([])
        self.input_results.hide()
        self.input_results_empty.setText(message)
        self.input_results_empty.show()
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)

    def _show_results(self, rows, total, offset, counts=None):
        if counts is not None:
            self.counters.setText(
                " • ".join(f"{label}: {counts.get(key, 0):,}" for key, label in LABELS.items())
            )
        self.rows, self.offset = rows, offset
        values = [
            [
                r["username"],
                LABELS[r["classification"]],
                r["api_code"],
                local_time(r["checked_at"]),
                r["completed_checks"],
                f"{r['previous_classification']} → {r['classification']}" if r["changed"] else "—",
                "نتيجة تجربة مستوردة"
                if r["source"].startswith("experiment-csv:")
                else "Roblox API"
                if r["source"] == "roblox-api"
                else "طلب منقطع",
            ]
            for r in self.rows
        ]
        self.result_columns.fill(values)
        self.input_result_columns.fill([[r[0], r[1], r[3]] for r in values[:3]])
        self.input_results.setVisible(bool(values))
        self.input_results_empty.setVisible(not values)
        selected = getattr(self, "_result_selection", None)
        self._result_selection = None
        if selected:
            for index, row in enumerate(self.rows):
                if row["username_key"] == selected["username_key"]:
                    self.result_table.selectRow(index)
                    break
        for i, row in enumerate(self.rows):
            for column in range(self.result_table.columnCount()):
                item = self.result_table.item(i, column)
                item.setToolTip("نتيجة أولية؛ انقر مرتين لعرض التاريخ والدليل.")
                if row["changed"]:
                    item.setBackground(QColor("#fff0c2"))
        self.page_label.setText(
            f"{self.offset + 1 if total else 0}–{min(total, self.offset + 100)} من {total:,}"
        )
        self.previous_button.setEnabled(self.offset > 0)
        self.next_button.setEnabled(self.offset + 100 < total)
        if not total:
            self.page_label.setText("لا نتائج مطابقة • 0")

    def queue_refresh(self):
        if not self.closing and not self.refresh_timer.isActive():
            self.refresh_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self.queue_refresh()

    def refresh(self):
        if self.busy and self.preparing:
            if hasattr(self, "_preparation_snapshot"):
                self.preparation_updated(self._preparation_snapshot)
            return
        self.speed.blockSignals(True)
        self.speed.setCurrentIndex(self.speed.findData(self.repo.preferred_interval()))
        self.speed.blockSignals(False)
        self.show_effective_speed()
        snapshot = (
            self.runner.execution.snapshot()
            if self.runner and self.runner.execution
            else self.last_execution[1]
            if self.last_execution and self.last_execution[0] == self.current_run
            else None
        )
        if snapshot:
            self.execution_status.setText(
                f"هذه الجلسة: {snapshot['workers']} عمال • سقف {snapshot['rate']:g} طلب/ث • "
                f"حسم مقبول {snapshot['accepted_per_second']:.2f}/ث • "
                f"{snapshot['results_per_minute']:.1f}/دقيقة • "
                f"محاولات {snapshot['attempts']} ({snapshot['attempts_per_second']:.2f}/ث) • "
                f"ردود HTTP {snapshot['http_responses']} منها 200: {snapshot['http_200']} • "
                f"جارٍ {snapshot['active']} • غير محفوظ بعد {snapshot['outstanding']}\n"
                + " • ".join(f"{name}: {snapshot['counts'].get(name, 0)}" for name in LABELS)
                + (" • انتظار مهلة مشتركة" if snapshot["waiting"] else "")
                + (" • توقف للمراجعة: " + snapshot["reason"] if snapshot["reason"] else "")
            )
        else:
            self.execution_status.clear()
        if not self.busy or self.isVisible():
            counts = self.repo.counts()
            self.counters.setText(
                " • ".join(f"{label}: {counts.get(key, 0):,}" for key, label in LABELS.items())
            )
        try:
            filters = self.advanced_filters.filters()
        except ValueError as exc:
            self.result_query.cancel(str(exc))
        else:
            if self.busy and (not self.isVisible() or self.workspace.currentIndex() != 1):
                self.result_query.cancel(None)
            elif self.advanced_filters.is_default():
                self.result_query.cancel(None)
                rows, total = self.repo.results(offset=self.offset)
                self._show_results(rows, total, self.offset)
            else:
                self.result_query.request(self.repo.store.path, "Roblox", filters, self.offset)
        runs = self.repo.runs()
        self.operations.blockSignals(True)
        self.operations.clear()
        self.operations.addItem("اختر عملية محفوظة للاستئناف", None)
        for run in runs:
            self.operations.addItem(
                f"{local_time(run['created_at'])} — {STATES[run['state']]} — {run['id'][:8]}", run["id"]
            )
        self.operations.setCurrentIndex(max(0, self.operations.findData(self.current_run)))
        self.operations.blockSignals(False)
        if self.current_run:
            progress = self.repo.progress(self.current_run)
            self.view_progress = (self.current_run, progress)
            if progress:
                done = progress["counts"].get("Done", 0)
                self.status.setText(
                    f"{STATES[progress['state']]} • عولج {done} من {progress['total']} • "
                    f"تُجاوز من السجل: {progress['skipped']} • مكرر: {progress['duplicates']} • "
                    f"غير صالح محليًا: {progress['invalid']}"
                    + (" • راجع القيود أو مهلة الانتظار قبل الاستئناف." if progress["error"] else "")
                )
                self.activity_status.setText(
                    f"Roblox • {STATES[progress['state']]} • {done}/{progress['total']}"
                    + (" • يتطلب مراجعة القيد أو الخطأ" if progress["error"] else "")
                )
        self.controls()
        if self.stopping and self.busy:
            self.activity_status.setText("جارٍ إيقاف Roblox وحفظ النتيجة الحالية…")

        self.overnight.refresh()

    def show_history(self):
        row = self.selected()
        if not row:
            self.status.setText("حدد اسمًا من الجدول أولًا.")
            return
        history = self.repo.history(row["username"])
        dialog = QDialog(self)
        dialog.setWindowTitle(f"تاريخ Roblox — {row['username']} — آخر 200 ملاحظة")
        dialog.resize(800, 600)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("نتائج أولية محفوظة؛ لم تُؤكد الإتاحة بالتسجيل. حدد ملاحظة لعرض دليلها."))
        table = self.make_table(["الوقت", "التصنيف", "كود API", "التغيير", "المصدر"])
        self.fill_table(
            table,
            [
                [
                    local_time(r["checked_at"]),
                    LABELS[r["classification"]],
                    r["api_code"],
                    f"{r['previous_classification']} → {r['classification']}" if r["changed"] else "—",
                    r["source"],
                ]
                for r in history
            ],
        )
        layout.addWidget(table, 1)
        evidence = QTextEdit()
        evidence.setReadOnly(True)
        evidence.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        evidence.setMaximumHeight(230)
        layout.addWidget(evidence)

        def selected():
            index = table.currentRow()
            if 0 <= index < len(history):
                evidence.setPlainText(
                    json.dumps(json.loads(history[index]["evidence"]), ensure_ascii=False, indent=2)
                )

        table.itemSelectionChanged.connect(selected)
        table.selectRow(0)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.exec()

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "تصدير Roblox", "roblox-results.csv", "CSV (*.csv)")
        if path:
            try:
                filters = self.advanced_filters.filters()
            except ValueError as exc:
                self.page_label.setText(str(exc))
                return
            history = self.export_history.isChecked()

            def ready(count):
                self.status.setText(f"صُدرت {count:,} ملاحظة أولية إلى CSV.")

            self._submit(
                lambda: ResultSearch(self.repo.store.path).export(
                    Path(path), "Roblox", filters, history=history
                ),
                ready,
            )

    def shutdown(self):
        self.prefilter.shutdown()
        self.bulk.shutdown()
        self.result_query.shutdown()
        self.closing = True
        self.refresh_timer.stop()
        self.stop(manual=False)
        if not self.busy:
            self.executor.shutdown(wait=False)
