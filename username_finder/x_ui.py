"""Experimental Arabic RTL X panel, isolated from every other platform."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
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

from . import x, x_connection
from .overnight_ui import OvernightControls
from .platform_workspace import PlatformWorkspace, ReadableColumns, ResultsPresentation, generation_fields
from .result_filters_ui import AdvancedResultFilters, ResultQueryController
from .result_search import ResultSearch
from .ui_cards import Card, CardGrid, Disclosure, PageHeading, caption, icon
from .ui_helpers import local_time, style_combo_popup
from .x_recovery import XRecovery
from .x_runner import XRunner
from .x_storage import XStore

STATES = {
    "Preparing": "تحضير الإدخال",
    "PreparationPaused": "التحضير متوقف",
    "PreparationError": "خطأ تحضير",
    "Ready": "جاهزة",
    "Running": "جارٍ الفحص",
    "Paused": "متوقفة — قابلة للاستئناف",
    "Restricted": "متوقفة للمراجعة",
    "Completed": "مكتملة",
    "Error": "خطأ",
}
SOURCES = {"generation": "توليد", "manual": "إدخال يدوي", "files": "إدخال TXT/CSV ويدوي"}


class XPanel(QScrollArea):
    idle = Signal()
    completed = Signal(object, object, str)
    progress_changed = Signal()
    preparation_progress = Signal(object)

    def __init__(self, store, make_table, fill_table, *, fetcher=None):
        super().__init__()
        self.repo = XStore(store)
        self.repo.recover()
        self.recovery = XRecovery(self.repo)
        self.recovery_timer = QTimer(self)
        self.recovery_timer.setInterval(1000)
        self.recovery_timer.timeout.connect(self.controls)
        self.make_table, self.fill_table = make_table, fill_table
        self.fetcher = fetcher
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="x-check")
        self.cancel = threading.Event()
        self.runner = None
        self.busy = self.closing = False
        self.stopping = False
        self.preparing = False
        self._preparation_snapshot = None
        self.preparation_progress.connect(self.preparation_updated)
        self.current_run = None
        self.rows = []
        self.offset = 0
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
        layout.addWidget(PageHeading("X · فحص الأسماء", "فحص تجريبي مستقل • الإتاحة غير مثبتة", "x"))
        notice = QLabel(
            "تحقق الإتاحة غير موثوق • إشارات available الحالية والقديمة تُقيَّم Unknown؛ الأدلة الأصلية محفوظة."
        )
        notice.setWordWrap(True)
        notice.setObjectName("simulation")
        layout.addWidget(notice)
        self.activity_status = caption("جاهز • يمكن تشغيل عمل X واحد بالتزامن مع Roblox.", "statusPill")
        root_layout = layout
        self.workspace = PlatformWorkspace()
        root_layout.addWidget(self.workspace, 1)
        input_layout = self.workspace.add_page("الأسماء والإعدادات")
        results_layout = self.workspace.add_page("النتائج", expand=True)
        history_layout = self.workspace.add_page("العمليات والاستعادة")
        connection_page = self.workspace.add_page("الاتصال والتنبيهات")
        layout = connection_page
        self.warning = QLabel(x.WARNING)
        self.warning.setObjectName("simulation")
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)
        connection_box = Card("الاتصال · للعمليات الجديدة", "layers")
        connection_layout = connection_box.body
        connection_form = QFormLayout()
        self.connection_mode = QComboBox()
        self.connection_mode.addItem("اتصال مباشر (الافتراضي)", "direct")
        self.connection_mode.addItem("بروكسي HTTP / HTTPS ثابت", "proxy")
        style_combo_popup(self.connection_mode)
        self.proxy_url = QLineEdit()
        self.proxy_url.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.proxy_url.setPlaceholderText("http://HOST:PORT — دون اسم مستخدم أو كلمة مرور")
        saved_route = self.repo.preferred_connection()
        self.connection_mode.setCurrentIndex(self.connection_mode.findData(saved_route["mode"]))
        self.proxy_url.setText(saved_route["proxy_url"])
        connection_form.addRow("طريقة الاتصال", self.connection_mode)
        connection_form.addRow("رابط البروكسي", self.proxy_url)
        connection_layout.addLayout(connection_form)
        self.save_connection_button, self.test_connection_button = self._actions(
            connection_layout,
            (
                ("حفظ اتصال العمليات الجديدة", self.save_connection),
                ("اختبار الاتصال فقط", self.test_connection),
            ),
        )
        self.compare_direct = QCheckBox(
            "مقارنة عنوان الخروج باتصال مباشر (طلب محايد إضافي؛ دون عرض العناوين)"
        )
        connection_layout.addWidget(self.compare_direct)
        self.saved_connection = QLabel()
        self.saved_connection.setWordWrap(True)
        connection_layout.addWidget(self.saved_connection)
        self.connection_status = QLabel("الاختبار لخدمة HTTPS محايدة فقط؛ لا يفحص X ولا يرفع أي توقف.")
        self.connection_status.setWordWrap(True)
        connection_layout.addWidget(self.connection_status)
        self.connection_mode.currentIndexChanged.connect(self.connection_controls)
        layout.addWidget(connection_box)
        connection_page.addStretch()
        self.overnight = OvernightControls(self, "X")
        history_layout.addWidget(self.overnight)
        history_card = Card("العملية المحددة وسجلها", "clock")
        history_layout.addWidget(history_card)
        history_root = history_layout
        history_layout = history_card.body
        input_card = Card("الأسماء · توليد أو استيراد", "users")
        settings_card = Card("التوقيت والتخطي", "clock")
        input_layout.addWidget(CardGrid((input_card, settings_card), columns=(2, 1), minimum=350))
        layout = input_card.body
        self.modes = QTabWidget()
        input_hint = QLabel("إعداد إدخال جديد؛ لا يغيّر مصدر العملية المحفوظة المحددة أدناه.")
        input_hint.setWordWrap(True)
        layout.addWidget(input_hint)
        generated = QWidget()
        form = QVBoxLayout(generated)
        self.minimum_length, self.maximum_length, self.quantity = QSpinBox(), QSpinBox(), QLineEdit("10")
        for control in (self.minimum_length, self.maximum_length):
            control.setRange(5, 15)
            control.setValue(8)
            control.setMaximumWidth(200)
        self.quantity.setMaximumWidth(200)
        form.addWidget(
            generation_fields(self.minimum_length, self.maximum_length, self.quantity),
            alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute,
        )
        self.modes.addTab(generated, "توليد أسماء")
        imported = QWidget()
        inputs = QVBoxLayout(imported)
        self.manual = QTextEdit()
        self.manual.setAcceptRichText(False)
        self.manual.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.manual.setPlaceholderText("اسم واحد في كل سطر؛ حروف ASCII وأرقام و_ فقط")
        self.manual.setFixedHeight(85)
        inputs.addWidget(self.manual)
        self.files = QListWidget()
        self.files.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.files.setMaximumHeight(65)
        inputs.addWidget(self.files)
        self.file_buttons = self._actions(
            inputs, (("إضافة TXT / CSV…", self.add_files), ("إزالة الملف المحدد", self.remove_file))
        )
        self.modes.addTab(imported, "إدخال واستيراد")
        self.modes.currentChanged.connect(self._fit_mode)
        layout.addWidget(self.modes)
        layout = settings_card.body
        self.skip_previous = QCheckBox("تجاوز أسماء X المفحوصة سابقًا")
        self.skip_previous.setToolTip("Skip Previously Checked")
        self.skip_previous.setChecked(True)
        layout.addWidget(self.skip_previous)
        speed_form = QFormLayout()
        speed_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        speed_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.speed = QComboBox()
        for value in x.INTERVALS:
            label = "0 ثانية — دون تأخير إضافي (متسلسل)" if value == 0 else f"{value:g} ثانية"
            self.speed.addItem(label + (" — الافتراضي" if value == 5 else ""), value)
        style_combo_popup(self.speed)
        self.speed.setToolTip("إعداد العمليات الجديدة فقط؛ لا يغيّر سرعة عملية محضّرة أو محفوظة.")
        self.speed.setCurrentIndex(self.speed.findData(self.repo.preferred_interval()))
        speed_form.addRow("التأخير بعد الاستجابة", self.speed)
        layout.addLayout(speed_form)
        self.speed_warning = QLabel("سرعة تجريبية؛ لم يثبت أمانها أو استقرارها لفحص X طويل المدة.")
        self.speed_warning.setWordWrap(True)
        self.speed_warning.setObjectName("notice")
        layout.addWidget(self.speed_warning)
        self.speed_warning.setVisible(self.speed.currentData() < 5)
        self.speed.currentIndexChanged.connect(self.speed_changed)
        self.job_settings = QLabel()
        self.job_settings.setWordWrap(True)
        history_layout.addWidget(self.job_settings)
        self.restriction = QLabel()
        self.restriction.setObjectName("simulation")
        self.restriction.setTextFormat(Qt.TextFormat.PlainText)
        self.restriction.setWordWrap(True)
        history_layout.addWidget(self.restriction)
        self.start_button, self.preview_button, self.stop_button = self._actions(
            root_layout,
            (
                ("بدء الفحص التجريبي", self.start),
                ("معاينة محلية", self.preview),
                ("إيقاف وحفظ", self.stop),
            ),
        )
        self.start_button.setObjectName("primary")
        self.stop_button.setObjectName("danger")
        self.stop_button.setIcon(icon("stop", "#ce425d", 17))
        root_layout.addWidget(self.activity_status)
        self.operations = QComboBox()
        style_combo_popup(self.operations)
        self.operations.currentIndexChanged.connect(self.select_operation)
        history_layout.addWidget(self.operations)
        self.resume_button, self.recheck_button, self.selected_button = self._actions(
            history_layout,
            (
                ("استئناف المحددة", self.resume),
                ("إعادة فحص السجل", self.recheck_all),
                ("إعادة فحص المحدد", self.recheck_selected),
            ),
        )
        recovery_card = Card("القيود والاستعادة اليدوية", "info")
        history_root.addWidget(recovery_card)
        history_layout = recovery_card.body
        history_layout.addWidget(self.restriction)
        self.review_restriction_button = QPushButton("مراجعة دليل قيد X — دون اتصال")
        self.review_restriction_button.clicked.connect(self.review_restriction)
        history_layout.addWidget(self.review_restriction_button)
        recovery_actions = self._actions(
            history_layout,
            (("طلب استعادة X واحد…", self.check_recovery), ("تحرير القيد يدويًا…", self.release_restriction)),
        )
        self.recovery_check_button, self.recovery_release_button = recovery_actions
        self.recovery_label = QLabel()
        self.recovery_label.setWordWrap(True)
        history_layout.addWidget(self.recovery_label)
        self.status = QLabel()
        self.status.setWordWrap(True)
        history_layout.addWidget(self.status)
        self.preparation_summary = QLabel()
        self.preparation_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.preparation_summary.setWordWrap(True)
        history_layout.addWidget(self.preparation_summary)
        self.preview_label = QLabel("الصيغة: 5–15 محرف ASCII؛ حروف وأرقام و_، بما في ذلك _ في الطرفين.")
        self.preview_label.setWordWrap(True)
        preview_card = Card("التحضير المحلي", "check")
        preview_card.body.addWidget(self.preview_label)
        input_layout.addWidget(preview_card)
        input_layout.addStretch()
        history_layout.addStretch()
        layout = results_layout
        self.counters = QLabel()
        self.counters.setWordWrap(True)
        layout.addWidget(self.counters)
        filters = QHBoxLayout()
        self.classification = QComboBox()
        self.classification.addItem("كل التصنيفات", "")
        for key, label in x.LABELS.items():
            self.classification.addItem(label, key)
        style_combo_popup(self.classification)
        # A line edit preserves normal selection and shared wheel behavior.
        self.search = QLineEdit()
        self.search.setPlaceholderText("بحث في سجل X")
        self.search.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        filters.addWidget(self.classification)
        filters.addWidget(self.search)
        layout.addLayout(filters)
        self.advanced_filters = AdvancedResultFilters("X", self.search, self.classification)
        self.table = make_table(["اسم X", "التصنيف الأولي", "آخر فحص", "عدد الملاحظات", "التغير", "سبب API"])
        self.table.setMinimumHeight(150)
        self.result_columns = ReadableColumns(self.table, self.fill_table, fill_width=True)
        self.result_query = ResultQueryController(self)
        self.result_query.ready.connect(self._show_results)
        self.result_query.cleared.connect(self._clear_results)
        self.advanced_filters.changed.connect(self.filter_changed)
        self.classification.currentIndexChanged.connect(self.advanced_filters.update_filters)
        self.search.textChanged.connect(self.advanced_filters.update_filters)
        self.advanced_filters.cancel_button.clicked.connect(lambda: self.result_query.cancel())
        self.results_presentation = ResultsPresentation(self.table, (0, 1), "X")
        layout.addWidget(self.results_presentation)
        layout.addWidget(self.table, 1)
        self.page_label = QLabel()
        layout.addWidget(self.page_label)
        self._actions(
            layout,
            (
                ("السابق", lambda: self.page(-1)),
                ("التالي", lambda: self.page(1)),
                ("عرض تاريخ الاسم", self.show_history),
                ("تصدير CSV", self.export),
            ),
        )
        self.export_history = QCheckBox("تضمين جميع الملاحظات التاريخية في التصدير")
        self.results_options = Disclosure("التصفية المتقدمة وخيارات التصدير")
        layout.addWidget(self.results_options)
        layout = self.results_options.body
        layout.addWidget(self.advanced_filters)
        self.export_history.setToolTip("عند تفعيله: تطبق الشروط على كل ملاحظة تاريخية بنفسها.")
        layout.addWidget(self.export_history)
        self.setWidget(content)
        self._fit_mode()
        self.refresh()

    def speed_changed(self):
        self.repo.set_interval(self.speed.currentData())
        self.controls()

    def connection_controls(self):
        idle = not self.busy and not self.closing
        proxy = self.connection_mode.currentData() == "proxy"
        self.connection_mode.setEnabled(idle)
        self.proxy_url.setEnabled(idle and proxy)
        self.compare_direct.setEnabled(idle and proxy)
        self.save_connection_button.setEnabled(idle)
        self.test_connection_button.setEnabled(idle)
        self.saved_connection.setText(
            "المحفوظ للعمليات الجديدة: " + x_connection.label(self.repo.preferred_connection())
        )

    def input_connection(self):
        return x_connection.configuration(self.connection_mode.currentData(), self.proxy_url.text())

    def save_connection(self):
        try:
            self.repo.set_connection(self.input_connection())
            self.connection_status.setText(
                "حُفظ الاتصال للعمليات الجديدة فقط؛ العمليات السابقة والقيود لم تتغير."
            )
            self.connection_controls()
        except ValueError as error:
            self.connection_status.setText(str(error))

    def connection_ready(self):
        try:
            if self.input_connection() != self.repo.preferred_connection():
                raise ValueError("احفظ إعداد الاتصال أولًا؛ لن يتغير مسار عملية موجودة.")
            return True
        except ValueError as error:
            self.connection_status.setText(str(error))
            self.activity_status.setText(str(error))
            return False

    def test_connection(self):
        try:
            route = self.input_connection()
        except ValueError as error:
            self.connection_status.setText(str(error))
            return
        compare = self.compare_direct.isChecked()

        def ready(result):
            if result.get("cancelled"):
                self.connection_status.setText("أُلغي الاختبار بعد انتهاء الطلب الجاري؛ لم يبدأ فحص X.")
                return
            mode = "البروكسي المحدد" if result["used_proxy"] else "الاتصال المباشر"
            text = ("نجح اختبار " if result["success"] else "فشل اختبار ") + mode
            text += f" • زمن الاستجابة: {result['latency_seconds']:.3f} ثانية."
            if not result["success"]:
                text += " تحقق من الشبكة والبروكسي وشهادة TLS؛ " + result.get("error", "خطأ غير متوقع")
            elif result["exit_changed"] is not None:
                text += " عنوان الخروج مختلف." if result["exit_changed"] else " لم يختلف عنوان الخروج."
            if result.get("comparison_error"):
                text += " تعذرت المقارنة المباشرة."
            self.connection_status.setText(text + " نجاح الاتصال لا يثبت توافق X أو دقة الإتاحة.")

        self.connection_status.setText("جارٍ اختبار خدمة HTTPS محايدة؛ لا طلبات إلى X…")
        self._submit(lambda: x_connection.test_connection(route, compare=compare, cancel=self.cancel), ready)

    def _fit_mode(self):
        height = self.modes.currentWidget().sizeHint().height() + self.modes.tabBar().sizeHint().height() + 12
        self.modes.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_mode()

    def _actions(self, layout, actions):
        row = QHBoxLayout()
        buttons = []
        for text, callback in actions:
            button = QPushButton(text)
            button.clicked.connect(callback)
            row.addWidget(button)
            buttons.append(button)
        layout.addLayout(row)
        return buttons

    def _submit(self, function, callback=lambda _: None):
        if self.busy or self.closing:
            return
        self.busy = True
        self.stopping = False
        self.cancel.clear()
        self.controls()
        future = self.executor.submit(function)

        def done(task):
            try:
                self.completed.emit(callback, task.result(), "")
            except Exception as error:
                self.completed.emit(callback, None, str(error))

        future.add_done_callback(done)

    def _finished(self, callback, result, error):
        self.busy = False
        self.stopping = False
        self.preparing = False
        self.refresh()
        if error:
            self.status.setText("تعذر الإكمال: " + error)
            self.activity_status.setText(self.status.text())
        elif not self.closing:
            callback(result)
        self.controls()
        if self.closing:
            self.executor.shutdown(wait=False)
        self.idle.emit()

    def controls(self):
        self.connection_controls()
        if self.busy and self.preparing:
            for widget in (
                self.modes,
                self.skip_previous,
                self.speed,
                self.operations,
                self.start_button,
                self.resume_button,
                self.preview_button,
                self.recheck_button,
                self.selected_button,
                self.recovery_check_button,
                self.recovery_release_button,
                self.review_restriction_button,
                self.overnight.prepare_button,
            ):
                widget.setEnabled(False)
            self.stop_button.setEnabled(True)
            return
        blocked = self.repo.control().get("blocked")
        running = any(run["state"] == "Running" for run in self.repo.runs())
        idle = not self.busy and not self.closing and not running
        run = self.repo.progress(self.current_run) if self.current_run else None
        resumable = bool(run and run["state"] in ("Ready", "Paused", "PreparationPaused", "PreparationError"))
        has_pending = bool(run and run["pending"])
        for widget in (self.modes, self.skip_previous, self.speed, self.operations):
            widget.setEnabled(idle)
        self.start_button.setEnabled(
            idle
            and not blocked
            and (
                run is None
                or run["state"] == "Completed"
                or (resumable and (has_pending or run["state"].startswith("Preparation")))
            )
            and (run is None or run["state"] == "Completed" or self.recovery.can_start(self.current_run))
        )
        self.start_button.setText(
            "لا أسماء مؤهلة للفحص"
            if run and run["state"] == "Ready" and not run["total"]
            else "بدء العملية الجاهزة"
            if run and run["state"] == "Ready"
            else "استئناف التحضير المحلي"
            if run and run["state"].startswith("Preparation")
            else "استئناف العملية المحددة"
            if resumable
            else "تحضير وبدء فحص جديد"
        )
        for button in (self.recheck_button, self.selected_button):
            button.setEnabled(idle and not blocked)
        self.preview_button.setEnabled(idle)
        self.stop_button.setEnabled(self.busy)
        self.speed_warning.setVisible(
            self.speed.currentData() < 5
            or bool(run and run["state"] in ("Ready", "Running", "Paused") and run["interval_seconds"] < 5)
        )
        self.resume_button.setEnabled(
            idle
            and not blocked
            and resumable
            and (has_pending or run["state"].startswith("Preparation"))
            and self.recovery.can_start(self.current_run)
        )
        self.resume_button.setText(
            "استئناف التحضير المحلي"
            if run and run["state"].startswith("Preparation")
            else "استئناف العملية المحددة"
        )
        self.restriction.setVisible(bool(blocked))
        self.restriction.setText(self.restriction_text())
        self.review_restriction_button.setEnabled(idle and bool(blocked))
        recovery = self.recovery.status()
        self.recovery_check_button.setEnabled(idle and recovery["eligible"])
        self.recovery_release_button.setEnabled(idle and recovery["release_eligible"])
        recovery_labels = {
            "None": "لا قيد محفوظ.",
            "Released": "حُرر القيد يدويًا؛ هذا لا يثبت استقرار الفحص المستمر.",
            "Eligible": "يمكن طلب فحص استعادة واحد بعد التأكيد؛ نهاية القيد غير معروفة.",
            "NewRestriction": "رُصد قيد جديد بعد التحرير؛ الفحص متوقف ويلزم تأكيد جديد لأي استعادة مسموحة.",
            "Waiting": "الاستعادة والتحرير ممنوعان حتى انتهاء الانتظار المحفوظ: ",
            "InFlight": "جارٍ طلب استعادة واحد؛ الفحص العادي ما زال ممنوعًا.",
            "AwaitingRelease": "نجح طلب واحد؛ القيد ما زال قائمًا بانتظار إقرار التحرير اليدوي.",
            "Failed": "فشل طلب الاستعادة؛ القيد باقٍ ولا إعادة تلقائية.",
            "Cancelled": "أُلغي طلب الاستعادة؛ القيد باقٍ.",
            "Uncertain": "طلب الاستعادة منقطع وغير محسوم؛ لا إعادة تلقائية.",
            "ReviewRequired": "الدليل لا يسمح باستعادة 429 آمنة؛ يلزم فحص تقني.",
        }
        self.recovery_label.setText(
            (
                "نجح طلب واحد؛ "
                if recovery["state"] == "Waiting"
                and recovery["latest"]
                and recovery["latest"]["state"] == "Succeeded"
                else ""
            )
            + recovery_labels[recovery["state"]]
            + (local_time(recovery["wait_until"]) if recovery["wait_until"] else "")
        )
        if recovery["state"] == "Waiting" and not self.recovery_timer.isActive():
            self.recovery_timer.start()
        elif recovery["state"] != "Waiting":
            self.recovery_timer.stop()
        text = f"إعداد العمليات الجديدة: {self.speed.currentData():g} ثانية. "
        if run:
            text += f"العملية المحددة تحتفظ بفاصل {run['interval_seconds']:g} ثانية وباتصالها المحفوظ. "
        self.job_settings.setText(
            text
            + "لتطبيق إعدادات مختلفة، اضغط «تحضير ومعاينة محلية» لإنشاء عملية جديدة."
            + (
                " أول عملية بعد التحرير يجب أن تكون جديدة، بالاتصال الأصلي وفاصل 5 ثوانٍ أو أكثر."
                if self.repo.control().get("recovery_gate")
                else ""
            )
        )

    def restriction_text(self):
        info = self.repo.restriction_info()
        if not info:
            return ""
        text = f"فحص X متوقف على مستوى المنصة للمراجعة: {info['reason']}. نهاية القيد غير معروفة. "
        text += (
            f"HTTP: {info['http_status'] or 'غير معروف'} • "
            f"وقت القيد: {local_time(info['recorded_at']) if info['recorded_at'] else 'غير معروف'}. "
        )
        if info["minimum_retry_at"]:
            text += "أدنى موعد انتظار معلن من الخادم: " + local_time(info["minimum_retry_at"]) + ". "
        else:
            text += "لم يُحفظ موعد انتظار صالح من الخادم؛ الموعد المحلي لا يثبت انتهاء القيد. "
        if info["invalid_wait_headers"]:
            text += "بعض رؤوس الانتظار غير صالحة. "
        return text + "تغيير السرعة أو الاتصال ونجاح اختبار الشبكة لا يرفع المنع؛ المراجعة محلية دون طلبات."

    def review_restriction(self):
        info = self.repo.restriction_info()
        if not info:
            return
        QMessageBox.information(
            self,
            "مراجعة قيد X — دون طلبات شبكة",
            self.restriction_text()
            + "\n\nالاستعادة تتطلب تأكيد طلب X واحد بالاتصال الأصلي، ثم إقرار تحرير منفصل عند نجاحه. "
            "نجاح طلب واحد لا يضمن استقرار الفحص. لن تُستأنف العملية القديمة.\n\n"
            + json.dumps(
                dict(restriction=info, recovery_events=self.recovery.events()), ensure_ascii=False, indent=2
            ),
        )

    def check_recovery(self):
        if self.busy or self.closing or not self.recovery.status()["eligible"]:
            return
        info = self.repo.restriction_info()
        if not self._confirm(
            "إرسال طلب X واحد للاسم OpenAI عبر اتصال القيد الأصلي: "
            + x_connection.label(info["connection"])
            + "؟ لن يرفع القيد تلقائيًا، ونجاحه لا يضمن استقرار الطلبات اللاحقة."
        ):
            return
        self._submit(
            lambda: self.recovery.check(
                confirmed=True, fetcher=self.fetcher, cancel=self.cancel, updated=self.progress_changed.emit
            ),
            lambda _: self.refresh(),
        )

    def release_restriction(self):
        if self.busy or self.closing or not self.recovery.status()["release_eligible"]:
            return
        if not self._confirm(
            "أقر بأن نجاح طلب واحد لا يثبت انتهاء القيود أو سلامة الفحص المستمر. "
            "هل أحرر القيد لعملية جديدة بالاتصال الأصلي وبفاصل افتراضي 5 ثوانٍ؟ "
            "لن تُستأنف العملية القديمة أو تتغير إعداداتها."
        ):
            return
        try:
            self.recovery.release(acknowledged=True)
            self.speed.setCurrentIndex(self.speed.findData(self.repo.preferred_interval()))
            self.refresh()
        except ValueError as error:
            self.status.setText(str(error))

    def spec(self):
        if self.modes.currentIndex() == 0:
            return {
                "generation": x.generation_range_spec(
                    self.minimum_length.value(), self.maximum_length.value(), int(self.quantity.text())
                )
            }
        return x.input_spec(
            self.manual.toPlainText(), [self.files.item(i).text() for i in range(self.files.count())]
        )

    def prepare(self, execute=False):
        if self.busy or self.closing:
            return
        if not self.connection_ready():
            return
        try:
            spec = self.spec()
        except (ValueError, OSError) as error:
            self.status.setText(str(error))
            self.activity_status.setText(str(error))
            return
        skip = self.skip_previous.isChecked()

        def ready(identity):
            self.current_run = identity
            self.overnight.arm(identity)
            self.refresh()
            if execute and self.repo.progress(identity)["state"] == "Ready":
                self._start_run(identity)

        self._prepare_local(
            lambda: self.repo.prepare(spec, skip=skip, cancel=self.cancel, notify=self.preparation_notice),
            ready,
        )

    def _prepare_local(self, function, callback):
        if self.busy or self.closing:
            return
        self.preparing = True
        self.runner = None
        self._preparation_snapshot = None
        self.activity_status.setText("جارٍ فتح مصدر الأسماء والتحضير محليًا؛ لم يبدأ الفحص.")
        self.status.setText("العدد النهائي غير معروف حتى اكتمال التحضير.")
        self.preparation_summary.setText("جارٍ إنشاء نقطة حفظ التحضير…")
        self._submit(function, callback)

    def preparation_notice(self, identity):
        tick = time.monotonic()
        if identity != getattr(self, "_last_preparation_identity", None) or (
            tick - getattr(self, "_last_preparation_notice", 0) >= 0.2
        ):
            self._last_preparation_identity = identity
            self._last_preparation_notice = tick
            self.preparation_progress.emit(self.repo.preparation_progress(identity))

    def preparation_updated(self, data):
        if not self.preparing:
            return
        self.current_run = data["id"]
        self._preparation_snapshot = data
        accepted = data["cursor"] - sum(data[key] for key in ("blank", "duplicates", "invalid", "skipped"))
        phase = "جارٍ الإيقاف وحفظ الدفعة" if self.stopping else "جارٍ التحضير محليًا"
        text = (
            f"{phase} • العملية {data['id'][:8]} • قُرئ: {data['cursor']:,} • مؤهل: {accepted:,} • "
            f"مكرر: {data['duplicates']:,} • غير صالح: {data['invalid']:,} • "
            f"فارغ: {data['blank']:,} • من السجل: {data['skipped']:,}"
        )
        self.activity_status.setText(text)
        self.preparation_summary.setText(text + " • العدد النهائي بعد اكتمال التحضير؛ لم تُرسل طلبات.")
        self.status.setText("التحضير محلي؛ الإيقاف يحفظ الدفعات المعتمدة للاستئناف دون بدء الفحص.")
        self.preview_label.setText("معاينة العملية الجديدة بعد اكتمال التحضير.")

    def preview(self):
        self.prepare()

    def start(self):
        if self.repo.control().get("blocked"):
            self.refresh()
            return
        run = self.repo.progress(self.current_run) if self.current_run else None
        if run and run["state"] != "Completed":
            self.resume()
        else:
            self.prepare(True)

    def _start_run(self, identity, *, automatic=False):
        if self.busy or self.closing:
            return
        self.current_run = identity
        self.overnight.arm(identity)
        run = self.repo.progress(identity)
        if (
            self.repo.control().get("blocked")
            or run["state"] not in ("Ready", "Paused")
            or not run["pending"]
            or not self.recovery.can_start(identity)
            or any(run["state"] == "Running" for run in self.repo.runs())
        ):
            self.refresh()
            return
        self.runner = XRunner(self.repo, fetcher=self.fetcher, updated=self.progress_changed.emit)
        self.activity_status.setText("X • جارٍ الفحص المتسلسل بالسرعة والاتصال المحفوظين.")
        self._submit(
            lambda: self.runner.run(identity, automatic=True) if automatic else self.runner.run(identity)
        )

    def stop(self, *, manual=True):
        self.stopping = self.busy
        self.cancel.set()
        if self.runner:
            if manual:
                self.runner.manual_stop.set()
            self.runner.cancel.set()
        self.activity_status.setText("جارٍ إيقاف X وحفظ النتائج؛ الطلب الجاري قد ينتظر مهلة الاتصال.")
        if self.preparing:
            self.activity_status.setText("جارٍ إيقاف التحضير المحلي وحفظ الدفعات؛ لم يبدأ الفحص.")
        if manual and self.current_run and not self.preparing:
            try:
                self.overnight.repo.stop(self.current_run, timeout_ms=0)
            except sqlite3.Error:
                self.activity_status.setText(
                    "أُرسل الإلغاء؛ حفظ الإيقاف لم يُؤكّد بعد بسبب تعذر الكتابة. انتظر تأكيده."
                )

    def resume(self):
        if not self.current_run:
            return
        identity = self.current_run
        if self.repo.progress(identity)["state"].startswith("Preparation"):

            def ready(_):
                if self.repo.progress(identity)["state"] == "Ready":
                    self.activity_status.setText(
                        "استُكمل التحضير المحلي؛ ابدأ الفحص صراحةً بعد مراجعة الجاهزية."
                    )

            self._prepare_local(
                lambda: self.repo.prepare(
                    run_id=identity, cancel=self.cancel, notify=self.preparation_notice
                ),
                ready,
            )
        else:
            self._start_run(identity)

    def _confirm(self, text):
        return (
            QMessageBox.question(
                self,
                "تأكيد فحص X التجريبي",
                text + "\n" + x.WARNING,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def recheck_all(self):
        if not self.connection_ready():
            return
        if self._confirm("إعادة فحص كل اسم X محفوظ مرة واحدة مع الحفاظ على تاريخه؟"):
            self._prepare_local(
                lambda: self.repo.prepare(
                    kind="recheck_all", cancel=self.cancel, notify=self.preparation_notice
                ),
                self._start_run,
            )

    def selected(self):
        indexes = self.table.selectionModel().selectedRows()
        index = indexes[0].row() if indexes else self.table.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def recheck_selected(self):
        if not self.connection_ready():
            return
        row = self.selected()
        if row and self._confirm("إعادة فحص " + row["username"] + "؟"):
            self._prepare_local(
                lambda: self.repo.prepare(
                    x.input_spec(row["username"]),
                    skip=False,
                    cancel=self.cancel,
                    notify=self.preparation_notice,
                ),
                self._start_run,
            )

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "ملفات أسماء X", "", "TXT / CSV (*.txt *.csv)")
        existing = {self.files.item(i).text() for i in range(self.files.count())}
        for path in paths:
            if path not in existing:
                self.files.addItem(path)
                existing.add(path)

    def remove_file(self):
        self.files.takeItem(self.files.currentRow())

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

    def _show_results(self, rows, total, offset, counts=None):
        if counts is not None:
            self.counters.setText(
                " • ".join(f"{label}: {counts.get(key, 0):,}" for key, label in x.LABELS.items())
            )
        selected = self.selected() or getattr(self, "_result_selection", None)
        self._result_selection = None
        self.rows, self.offset = rows, offset
        self.result_columns.fill(
            [
                [
                    r["username"],
                    x.LABELS[r["classification"]],
                    local_time(r["checked_at"]),
                    r["completed_checks"],
                    (r["previous_classification"] + " → " + r["classification"]) if r["changed"] else "—",
                    json.loads(r["evidence"]).get("api_reason", ""),
                ]
                for r in self.rows
            ],
        )
        for index, row in enumerate(self.rows):
            if row.get("recorded_classification") == "Available":
                self.table.item(index, 1).setToolTip(
                    "التصنيف المسجل أصلًا: Available • التقييم الحالي: Unknown. "
                    "إشارة API غير موثوقة؛ اعرض تاريخ الاسم لمراجعة الدليل الأصلي."
                )
        if selected:
            for index, row in enumerate(self.rows):
                if row["username_key"] == selected["username_key"]:
                    self.table.selectRow(index)
                    break
        self.page_label.setText(f"{self.offset + 1 if total else 0}–{self.offset + len(rows)} من {total:,}")
        if not total:
            self.page_label.setText("لا نتائج مطابقة • 0")

    def queue_refresh(self):
        if not self.closing and not self.refresh_timer.isActive():
            self.refresh_timer.start()

    def refresh(self):
        if self.busy and self.preparing:
            if self._preparation_snapshot is not None:
                self.preparation_updated(self._preparation_snapshot)
            self.controls()
            return
        counts = self.repo.counts()
        self.counters.setText(" • ".join(f"{label}: {counts.get(key, 0)}" for key, label in x.LABELS.items()))
        try:
            filters = self.advanced_filters.filters()
        except ValueError as exc:
            self.result_query.cancel(str(exc))
        else:
            if self.advanced_filters.is_default():
                self.result_query.cancel(None)
                rows, total = self.repo.results(offset=self.offset)
                self._show_results(rows, total, self.offset)
            else:
                self.result_query.request(self.repo.store.path, "X", filters, self.offset)
        self.operations.blockSignals(True)
        self.operations.clear()
        runs = self.repo.runs()
        if self.current_run is None and runs:
            self.current_run = runs[0]["id"]
        for run in runs:
            spec = json.loads(run["spec"])
            source = "generation" if spec.get("generation") else "files" if spec.get("files") else "manual"
            state_label = STATES.get(run["state"], run["state"])
            if run["state"] == "Ready" and run["cursor"] == sum(
                run[key] for key in ("skipped", "duplicates", "invalid", "blank")
            ):
                state_label = "لا أسماء مؤهلة"
            self.operations.addItem(
                f"{state_label} — {SOURCES[source]} — {run['created_at'][:19]} — {run['id'][:8]}",
                run["id"],
            )
        if self.current_run:
            self.operations.setCurrentIndex(self.operations.findData(self.current_run))
            run = self.repo.progress(self.current_run)
            self.preview_label.setText(
                "معاينة محلية — أول 20 مرشحًا: "
                + "، ".join(f"{name} (الطول {len(name)})" for name in self.repo.preview(self.current_run))
            )
            state_label = STATES.get(run["state"], run["state"])
            if run["state"] == "Ready":
                if not run["total"]:
                    state_label = "لا أسماء مؤهلة للفحص"
                elif self.repo.control().get("blocked"):
                    state_label = "جاهزة — الفحص ممنوع بقيد X"
            self.status.setText(
                f"{state_label} • {run['completed']}/{run['total']} • "
                f"تجاوز سابق: {run['skipped']} • مكرر: {run['duplicates']} • غير صالح: {run['invalid']} • "
                f"فاصل العملية الفعلي: {run['interval_seconds']:g} ثانية • {run['error']}"
                + " • اتصال العملية الفعلي: "
                + x_connection.label(self.repo.connection_for_run(self.current_run))
            )
            self.activity_status.setText(
                f"X • {state_label} • {run['completed']}/{run['total']}"
                + (" • راجع صفحة العمليات والاستعادة" if self.repo.control().get("blocked") else "")
            )
            source = SOURCES[run["input_source"]]
            summary = f"العملية المحفوظة {run['id'][:8]} • المصدر: {source} • "
            if run["input_source"] == "generation":
                summary += (
                    f"الطول {run['minimum_length']}–{run['maximum_length']} • "
                    f"المطلوب: {run['requested_candidates']} • "
                    f"المُولَّد المقروء: {run['generated_candidates']} • "
                )
            else:
                summary += f"عناصر الإدخال المقروءة: {run['cursor']} • "
            self.preparation_summary.setText(
                summary + f"غير صالح: {run['invalid']} • مكرر: {run['duplicates']} • "
                f"فارغ: {run['blank']} • تجاوز سابق: {run['skipped']} • "
                f"إجمالي المؤهل: {run['total']} • المتبقي للفحص: {run['pending']}"
            )
            if run["state"] == "Ready" and not run["total"]:
                self.status.setText(
                    self.status.text()
                    + " • راجع مصدر العملية وعدّاداتها؛ تحضير جديد يستخدم إعداد الإدخال الحالي."
                )
                self.preview_label.setText(
                    "لا مرشحين مؤهلين؛ راجع عدّادات الإدخال غير الصالح والمكرر والمتجاوز."
                )
        elif self.repo.control().get("blocked"):
            self.status.setText("X متوقف للمراجعة: " + self.repo.control()["blocked"])
            self.activity_status.setText("X متوقف بقيد محفوظ • راجع صفحة العمليات والاستعادة.")
        self.operations.blockSignals(False)
        self.controls()
        if self.stopping and self.busy:
            self.activity_status.setText("جارٍ إيقاف X وحفظ النتيجة الحالية…")

        self.overnight.refresh()

    def show_history(self):
        row = self.selected()
        if not row:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("تاريخ X — " + row["username"])
        dialog.resize(760, 480)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(
            x.WARNING + "\n\n" + json.dumps(self.repo.history(row["username"]), indent=2, ensure_ascii=False)
        )
        layout.addWidget(text)
        dialog.exec()

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "تصدير X الأولي", "x-results.csv", "CSV (*.csv)")
        if path:
            try:
                filters = self.advanced_filters.filters()
            except ValueError as exc:
                self.page_label.setText(str(exc))
                return
            history = self.export_history.isChecked()
            self._submit(
                lambda: ResultSearch(self.repo.store.path).export(path, "X", filters, history=history),
                lambda count: self.status.setText(f"صُدّرت {count} ملاحظة أولية مع التحذير والدليل."),
            )

    def shutdown(self):
        self.result_query.shutdown()
        self.recovery_timer.stop()
        self.closing = True
        self.refresh_timer.stop()
        self.stop(manual=False)
        if not self.busy:
            self.executor.shutdown(wait=False)
