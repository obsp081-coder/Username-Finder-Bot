from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QObject, QSignalBlocker, QSize, Qt, QTime, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from .dashboard_ui import DashboardPanel
from .domain import Filters, JobState, SearchConfig, normalize
from .engine import ExecutionEngine
from .file_generator_ui import FileGeneratorPanel
from .file_search_ui import FileSearchPanel
from .generation import GeneratorSpace
from .instagram_ui import InstagramPanel
from .job_manager_ui import JobManagerPanel
from .platform_workspace import ResultsPresentation
from .presets import file_issues, make_payload, resolve_path, validate_payload
from .roblox_ui import RobloxPanel
from .scheduling import ScheduleConfig, Scheduler
from .snapscore_ui import SnapscorePanel
from .sources import MeaningLibrary, make_snapshot, preview_txt
from .storage import Store
from .ui_cards import Card, CardGrid, PageHeading, ScrollBody, icon
from .ui_helpers import REASONS, WheelGuard, local_time, status_text, style_combo_popup
from .ui_theme import LIGHT_THEME
from .x_ui import XPanel

CATEGORIES = {
    "english_names": "أسماء أشخاص باللاتينية",
    "english_words": "كلمات إنجليزية",
    "arabic_names_latin": "أسماء عربية باللاتينية",
    "gaming_terms": "مصطلحات ألعاب منتقاة",
}
STATE_LABELS = {
    "Ready": "جاهز",
    "Running": "جارٍ التشغيل",
    "Paused": "متوقف مؤقتًا",
    "Stopped": "متوقف — قابل للاستكمال",
    "Interrupted": "انقطع — قابل للاستكمال",
    "Completed": "مكتمل",
    "Blocked": "محظور وقائيًا",
}
STATUS_LABELS = {
    "Available": "متاح",
    "Taken": "مستخدم",
    "Reserved": "محجوز",
    "Invalid": "غير صالح",
    "Unknown": "غير محسوم",
}
ERROR_LABELS = {
    "LIVE_VERIFICATION_NOT_VALIDATED": "الفحص الحقيقي معطّل؛ لم يُثبت مصدر موثوق.",
    "ValueError": "تعارض في البيانات أو تغير مصدر الإدخال؛ راجع الملفات وابدأ بحثًا جديدًا.",
    "OperationalError": "تعذر حفظ البيانات؛ تحقق من مساحة القرص وصلاحية الوصول.",
    "403": "رفض الموفر الوصول؛ أُوقف التشغيل.",
    "429": "تكررت قيود الموفر؛ أُوقف التشغيل.",
    "SEMANTIC_MISMATCH": "تغيرت دلالة الموفر؛ حُجرت نتائجه.",
    "PROVIDER_QUARANTINED": "الموفر محجور ولا يُسمح باستكماله.",
}


class Bridge(QObject):
    updated = Signal(dict)
    finished = Signal()
    completed = Signal(object, object)
    failed = Signal(str, bool)


def spin(low, high, value):
    widget = QSpinBox()
    widget.setRange(low, high)
    widget.setValue(value)
    return widget


def text_input(placeholder=""):
    widget = QLineEdit()
    widget.setPlaceholderText(placeholder)
    widget.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    return widget


def combo(items):
    widget = QComboBox()
    for label, data in items:
        widget.addItem(label, data)
    style_combo_popup(widget)
    return widget


def button(label, callback):
    widget = QPushButton(label)
    widget.clicked.connect(callback)
    return widget


class MainWindow(QMainWindow):
    def __init__(self, database: Path, *, osintgram_options=None):
        super().__init__()
        self.setWindowTitle("Username Finder Bot")
        self.resize(1180, 850)
        self.setMinimumSize(880, 650)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        palette = self.palette()
        for role, color in (
            (QPalette.ColorRole.Window, "#f5f7fb"),
            (QPalette.ColorRole.WindowText, "#24344b"),
            (QPalette.ColorRole.Base, "#ffffff"),
            (QPalette.ColorRole.AlternateBase, "#f7f9fd"),
            (QPalette.ColorRole.Text, "#24344b"),
            (QPalette.ColorRole.Button, "#ffffff"),
            (QPalette.ColorRole.ButtonText, "#24344b"),
        ):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        self.store = Store(database)
        self.store.recover()
        self.current_job: str | None = None
        self.engine: ExecutionEngine | None = None
        self.engine_thread: threading.Thread | None = None
        self.scheduler: Scheduler | None = None
        self.closing = False
        self.closed = False
        self.busy = False
        self.preset_id = None
        self._loading_settings = False
        self._allow_close = False
        self.preset_files = []
        self.editor_timezone = "Asia/Riyadh"
        self.results_total = 0
        self._result_rows = []
        self._preview_request_id = 0
        self.last_preview_diagnostics = {}
        self.offset = 0
        self.history_offset = 0
        self.jobs_offset = 0
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="local-data")
        self.futures = set()
        self.bridge = Bridge()
        self.bridge.updated.connect(self.update_engine)
        self.bridge.finished.connect(self.engine_finished)
        self.bridge.completed.connect(self._callback)
        self.bridge.failed.connect(self.show_error)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 10, 12, 0)
        layout.setSpacing(10)
        title = QLabel("Username Finder Bot")
        title.setObjectName("brand")
        header_widget = QWidget()
        header_widget.setObjectName("appHeader")
        header = QHBoxLayout(header_widget)
        header.addWidget(title)
        banner = QLabel("مساحة عمل محلية • نتائج التحقق أولية")
        banner.setWordWrap(True)
        banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(banner, 1)
        self.page_heading = QLabel("لوحة التحكم")
        self.page_heading.setObjectName("heading")
        header.addWidget(self.page_heading)
        layout.addWidget(header_widget)
        self.tabs = QTabWidget()
        self.tabs.tabBar().setObjectName("platformNavigation")
        self.tabs.tabBar().hide()
        shell = QHBoxLayout()
        # RTL puts the first widget on the right; existing page instances and indices remain intact.
        self.sidebar = QWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(192)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(9, 12, 9, 12)
        brand = QLabel("Username Finder Bot")
        brand.setObjectName("sidebarBrand")
        brand.setWordWrap(True)
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side.addWidget(brand)
        label = QLabel("مساحة العمل")
        label.setObjectName("muted")
        side.addWidget(label)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setAccessibleName("التنقل بين مساحات العمل")
        self.navigation.setIconSize(QSize(23, 23))
        self.navigation.setUniformItemSizes(True)
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side.addWidget(self.navigation, 1)
        local = QLabel("بياناتك محلية\nAsia/Riyadh")
        local.setObjectName("muted")
        side.addWidget(local)
        shell.addWidget(self.sidebar)
        shell.addWidget(self.tabs, 1)
        layout.addLayout(shell, 1)
        self.setCentralWidget(root)
        self.snapchat_tabs = QTabWidget()
        self.search_tabs = QTabWidget()
        self.snapchat_tabs.addTab(self.search_tabs, "البحث")
        self.snapscore = SnapscorePanel()
        self.snapchat_tabs.addTab(self.snapscore, "سناب سكور")
        self.tabs.addTab(self.snapchat_tabs, "سناب")
        self.build_settings()
        self.build_run()
        self.build_results()
        self.build_history()
        self.instagram = InstagramPanel(self.make_table, osintgram_options=osintgram_options)
        self.instagram.analysis.client.idle.connect(self._close_after_osintgram)
        self.instagram.analysis.manager.idle.connect(self._close_after_osintgram)
        self.tabs.addTab(self.instagram, "Instagram")
        self.file_search = FileSearchPanel(analysis=self.instagram.analysis)
        self.file_search.idle.connect(self._close_after_osintgram)
        self.tabs.addTab(self.file_search, "البحث داخل الملفات")
        self.roblox = RobloxPanel(self.store, self.make_table, self.fill_table)
        self.roblox.idle.connect(self._close_after_osintgram)
        self.tabs.addTab(self.roblox, "Roblox")
        self.file_generator = FileGeneratorPanel()
        self.file_generator.idle.connect(self._close_after_osintgram)
        self.tabs.addTab(self.file_generator, "صانع ملفات اليوزرات")
        self.x_panel = XPanel(self.store, self.make_table, self.fill_table)
        self.x_panel.idle.connect(self._close_after_osintgram)
        self.tabs.addTab(self.x_panel, "X — تجريبي")
        self.job_manager = JobManagerPanel(self)
        self.tabs.addTab(self.job_manager, "إدارة العمليات")
        self.tabs.currentChanged.connect(lambda _: self.job_manager.refresh_if_visible())
        self.dashboard = DashboardPanel(self)
        self.tabs.addTab(self.dashboard, "لوحة التحكم")
        for index, label, symbol in (
            (7, "لوحة التحكم", "home"),
            (3, "Roblox", "roblox"),
            (1, "Instagram", "instagram"),
            (0, "سناب", "snapchat"),
            (5, "X · تجريبي", "x"),
            (6, "إدارة العمليات", "layers"),
            (4, "صانع اليوزرات", "generate"),
            (2, "بحث الملفات", "search"),
        ):
            item = QListWidgetItem(icon(symbol, size=23), label)
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(self.tabs.tabText(index))
            item.setSizeHint(QSize(150, 42))
            self.navigation.addItem(item)
        self.navigation.currentRowChanged.connect(self.navigate)
        self.tabs.currentChanged.connect(self.sync_navigation)
        self.setStyleSheet(LIGHT_THEME)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(250)
        self.wheel_guard = WheelGuard(self)
        QApplication.instance().installEventFilter(self.wheel_guard)
        self._baseline = self.editor_signature()
        self.connect_settings_changes()
        self.refresh_presets()
        self.refresh_jobs()
        self.refresh_history()
        self.statusBar().showMessage("جاهز • البيانات محلية • المنطقة الزمنية Asia/Riyadh")
        unfinished = self.store.latest_unfinished()
        if unfinished:
            self.load_job(unfinished)
        self.tabs.setCurrentWidget(self.dashboard)
        from .overnight_ui import schedule_startup_recovery

        schedule_startup_recovery(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "sidebar"):
            self.sidebar.setFixedWidth(168 if self.width() < 1050 else 192)

    def navigate(self, row):
        item = self.navigation.item(row)
        if item:
            self.tabs.setCurrentIndex(item.data(Qt.ItemDataRole.UserRole))

    def sync_navigation(self, index):
        with QSignalBlocker(self.navigation):
            for row in range(self.navigation.count()):
                if self.navigation.item(row).data(Qt.ItemDataRole.UserRole) == index:
                    self.navigation.setCurrentRow(row)
                    break
        self.page_heading.setText(self.tabs.tabText(index))

    def build_settings(self):
        self.settings_page = QWidget()
        layout = QVBoxLayout(self.settings_page)
        layout.setSpacing(10)
        layout.addWidget(
            PageHeading("سناب · إعداد البحث", "الفحص الحقيقي معطّل • جميع عمليات البحث محاكاة", "snapchat")
        )
        presets = Card("قوالب البحث المحفوظة", "folder")
        preset_layout = presets.body
        select_row = QHBoxLayout()
        preset_actions = QGridLayout()
        self.preset_selector = combo([("إعدادات جديدة — غير محفوظة", None)])
        self.preset_selector.setMinimumWidth(220)
        select_row.addWidget(self.preset_selector, 1)
        self.preset_save = button("حفظ القالب", self.save_preset)
        self.preset_save.setToolTip("حفظ التغييرات في القالب المحدد، أو إنشاء قالب عند عدم اختيار قالب.")
        preset_actions.addWidget(self.preset_save, 0, 0)
        save_as = button("نسخة باسم جديد…", lambda: self.save_preset(save_as=True))
        save_as.setToolTip("إنشاء قالب مستقل مع الاحتفاظ بالقالب السابق.")
        preset_actions.addWidget(save_as, 0, 1)
        self.preset_rename = button("إعادة تسمية", self.rename_preset)
        self.preset_delete = button("حذف", self.delete_preset)
        preset_actions.addWidget(self.preset_rename, 0, 2)
        preset_actions.addWidget(self.preset_delete, 0, 3)
        preset_layout.addLayout(select_row)
        preset_layout.addLayout(preset_actions)
        self.preset_notice = QLabel("عدّل الإعدادات، ثم احفظها كقالب لاستخدامها لاحقًا.")
        self.preset_notice.setWordWrap(True)
        preset_layout.addWidget(self.preset_notice)
        layout.addWidget(presets)

        basic = Card("مصدر الأسماء وحدود البحث", "users")
        form = QFormLayout()
        basic.body.addLayout(form)
        self._fit_settings_form(form)
        self.source = combo([("توليد تلقائي", "generator"), ("ملفات TXT", "txt")])
        self.mode = combo(
            [
                ("جميع التركيبات", "all"),
                ("أسماء ذات معنى — أصلية", "meaningful"),
                ("أسماء ذات معنى ثم التركيبات", "mixed"),
            ]
        )
        form.addRow("مصدر الأسماء", self.source)
        self.mode_label = QLabel("نمط التوليد")
        form.addRow(self.mode_label, self.mode)
        length_row = QHBoxLayout()
        self.min_length, self.max_length = spin(3, 15, 3), spin(3, 15, 5)
        length_row.addWidget(self.min_length)
        length_row.addWidget(QLabel("إلى"))
        length_row.addWidget(self.max_length)
        length_row.addStretch()
        form.addRow("طول الاسم", length_row)
        target_row = QHBoxLayout()
        self.limit_enabled = QCheckBox("حد الأسماء")
        self.limit_enabled.setChecked(True)
        self.limit = text_input()
        self.limit.setText("1000")
        self.limit.setMaximumWidth(190)
        target_row.addWidget(self.limit_enabled)
        target_row.addWidget(self.limit)
        target_row.addStretch()
        form.addRow("عدد الأسماء المستهدف", target_row)
        self.txt_group = QWidget()
        file_layout = QVBoxLayout(self.txt_group)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.files = QListWidget()
        self.files.setMaximumHeight(85)
        self.files.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        file_layout.addWidget(self.files)
        file_actions = QHBoxLayout()
        file_actions.addWidget(button("إضافة ملفات…", self.add_files))
        file_actions.addWidget(button("إزالة المحدد", self.remove_file))
        file_actions.addStretch()
        file_layout.addLayout(file_actions)
        form.addRow(self.txt_group)
        layout.addWidget(basic)

        self.advanced_toggle = QPushButton("إعدادات متقدمة")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolTip("الفلاتر والرموز والأداء والجدولة والسجل الذكي")
        layout.addWidget(self.advanced_toggle)
        self.advanced_tabs = QTabWidget()
        self.advanced_tabs.setMinimumHeight(410)
        self.advanced_tabs.setVisible(False)
        self.advanced_toggle.toggled.connect(self.advanced_tabs.setVisible)
        layout.addWidget(self.advanced_tabs)

        def advanced_page(title):
            content = QWidget()
            page_form = QFormLayout(content)
            self._fit_settings_form(page_form)
            page_form.setSpacing(14)
            page_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setWidget(content)
            self.advanced_tabs.addTab(area, title)
            return page_form

        generation = advanced_page("التوليد والرموز")
        self.letters = QCheckBox("السماح بالحروف بعد حرف البداية الإجباري")
        self.letters.setChecked(True)
        self.digits = QCheckBox("السماح بالأرقام")
        self.digits.setChecked(True)
        generation.addRow(self.letters)
        generation.addRow(self.digits)
        self.specials = {}
        symbols = QHBoxLayout()
        for symbol, label in [("_", "_  شرطة سفلية"), (".", ".  نقطة"), ("-", "-  شرطة")]:
            check = QCheckBox(label)
            check.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            self.specials[symbol] = check
            symbols.addWidget(check)
        generation.addRow("علامة واحدة كحد أقصى", symbols)
        self.meaning_group = QGroupBox("مكتبات الأسماء ذات المعنى")
        meaning_layout = QVBoxLayout(self.meaning_group)
        self.categories = {}
        for key, label in CATEGORIES.items():
            check = QCheckBox(label)
            check.setChecked(True)
            self.categories[key] = check
            meaning_layout.addWidget(check)
        meaning_layout.addWidget(button("حول المكتبات ومصادرها", self.about_data))
        generation.addRow(self.meaning_group)
        self.meaning_hint = QLabel("تُستخدم المكتبات عند اختيار أحد نمطي الأسماء ذات المعنى.")
        self.meaning_hint.setWordWrap(True)
        generation.addRow(self.meaning_hint)

        conditions = advanced_page("شروط الأسماء")
        self.prefix, self.suffix, self.required = text_input("مثال: a"), text_input("مثال: 9"), text_input()
        conditions.addRow("يبدأ بـ", self.prefix)
        conditions.addRow("ينتهي بـ", self.suffix)
        conditions.addRow("يحتوي على", self.required)
        self.repeat = QCheckBox("اشتراط تكرار حرف أو رقم")
        self.repeat_target = text_input("أي حرف أو رقم، أو رمز محدد")
        self.repeat_target.setText("أي")
        self.repeat_target.setToolTip("اكتب «أي» لاشتراط أي حرف أو رقم، أو اكتب رمزًا واحدًا مثل a أو 7.")
        self.repeat_count = spin(2, 15, 2)
        self.repeat_kind = combo([("بالضبط", True), ("على الأقل", False)])
        self.adjacency = combo(
            [("دون شرط تجاور", "any"), ("متجاور", "adjacent"), ("غير متجاور", "separated")]
        )
        conditions.addRow(self.repeat)
        conditions.addRow("رمز التكرار", self.repeat_target)
        conditions.addRow("مرات الظهور", self.repeat_count)
        conditions.addRow("نوع العدد", self.repeat_kind)
        conditions.addRow("التجاور", self.adjacency)

        performance = advanced_page("الأداء والسجل")
        self.auto_workers = QCheckBox("ضبط التزامن تدريجيًا")
        self.auto_workers.setChecked(True)
        self.workers, self.queue = spin(1, 1000, 5), spin(1, 10000, 128)
        self.smart_history = QCheckBox("تخطي النتائج الموثوقة التي ما زالت صالحة")
        self.smart_history.setChecked(True)
        self.ttl = QDoubleSpinBox()
        self.ttl.setRange(0, 87600)
        self.ttl.setValue(24)
        self.retries, self.retry_budget = spin(0, 10, 2), spin(0, 100000, 100)
        self.timeout, self.circuit = spin(1, 300, 10), spin(1, 100, 5)
        performance.addRow(self.auto_workers)
        performance.addRow("الحد الأقصى للفحوص المتزامنة", self.workers)
        self.workers.setToolTip("من 1 إلى 1000؛ تبقى حدود الموفر مستقلة عن هذا الإعداد.")
        performance.addRow("حد المهام قيد المعالجة", self.queue)
        performance.addRow(self.smart_history)
        performance.addRow("صلاحية التخطي بالساعات", self.ttl)
        performance.addRow("إعادات المحاولة لكل اسم", self.retries)
        performance.addRow("حد الإعادات للبحث كله", self.retry_budget)
        performance.addRow("مهلة الفحص بالثواني", self.timeout)
        performance.addRow("أخطاء متتالية قبل الإيقاف", self.circuit)

        scheduling = advanced_page("الجدولة")
        self.daily_start, self.daily_stop = QCheckBox("تشغيل يومي"), QCheckBox("إيقاف يومي")
        self.start_time, self.stop_time = QTimeEdit(QTime(9, 0)), QTimeEdit(QTime(17, 0))
        for time_edit in (self.start_time, self.stop_time):
            time_edit.setDisplayFormat("HH:mm")
            time_edit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        scheduling.addRow(self.daily_start, self.start_time)
        scheduling.addRow(self.daily_stop, self.stop_time)
        self.duration = QCheckBox("إيقاف بعد مدة تشغيل فعلية")
        self.duration_hours, self.duration_minutes = spin(0, 999, 1), spin(0, 59, 0)
        duration_row = QHBoxLayout()
        duration_row.addWidget(self.duration_hours)
        duration_row.addWidget(QLabel("ساعة"))
        duration_row.addWidget(self.duration_minutes)
        duration_row.addWidget(QLabel("دقيقة"))
        scheduling.addRow(self.duration)
        scheduling.addRow(duration_row)
        self.timezone_hint = QLabel(
            "التوقيت: الرياض • يلزم بقاء التطبيق مفتوحًا. الإيقاف المؤقت لا يُحتسب ضمن المدة."
        )
        self.timezone_hint.setWordWrap(True)
        scheduling.addRow(self.timezone_hint)
        actions = QHBoxLayout()
        actions.addWidget(button("معاينة البحث", self.preview))
        self.new_button = button("مراجعة وإنشاء بحث", self.prepare_job)
        self.new_button.setObjectName("primary")
        actions.addWidget(self.new_button)
        layout.addLayout(actions)
        self.preview_label = QLabel("ابدأ بضبط المصدر والطول والعدد، ثم عاين البحث قبل إنشائه.")
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)
        layout.addStretch()
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(self.settings_page)
        self.search_tabs.addTab(area, "الإعدادات")

        for widget in self.setting_controls().values():
            if isinstance(widget, (QSpinBox, QDoubleSpinBox, QTimeEdit)):
                widget.setMinimumWidth(115)
                widget.setMaximumWidth(175)
                widget.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            elif isinstance(widget, QLineEdit):
                widget.setMaximumWidth(320)
            elif isinstance(widget, QComboBox):
                widget.setMaximumWidth(410)
        self.source.setMinimumWidth(290)
        self.mode.setMinimumWidth(290)
        self.preset_selector.setMaximumWidth(480)

    @staticmethod
    def _fit_settings_form(form):
        form.setFormAlignment(
            Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)

    def build_run(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            PageHeading("سناب · تشغيل المحاكاة", "النتائج والسرعة تخص المحاكاة فقط؛ لا اتصال بسناب", "play")
        )
        row = QGridLayout()
        self.start_button = button("بدء / استكمال", self.start_engine)
        self.pause_button = button("إيقاف مؤقت", self.pause_engine)
        self.stop_button = button("إيقاف آمن", self.stop_engine)
        self.recover_button = button("مراجعة العائق والاستكمال", self.recover_blocked_job)
        self.recover_button.setToolTip("فحص العائق وتجهيز الاستكمال دون تجاوز القيود")
        self.start_button.setObjectName("primary")
        self.start_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.recover_button.setEnabled(False)
        self.stop_button.setObjectName("danger")
        row.addWidget(self.start_button, 0, 0)
        row.addWidget(self.pause_button, 0, 1)
        row.addWidget(self.stop_button, 1, 0)
        row.addWidget(self.recover_button, 1, 1)
        layout.addLayout(row)
        self.run_area = ScrollBody()
        run_body = QWidget()
        self.run_area.setWidget(run_body)
        layout.addWidget(self.run_area, 1)
        layout = QVBoxLayout(run_body)
        layout.setContentsMargins(0, 0, 0, 0)
        run_card = Card("حالة العملية وتقدمها", "layers")
        layout.addWidget(run_card)
        root_layout = layout
        layout = run_card.body
        self.state_label = QLabel("لا يوجد بحث محدد. أنشئ بحثًا من الإعدادات أو اختر عملية من السجل.")
        self.state_label.setObjectName("heading")
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        layout.addWidget(self.progress)
        summary = QHBoxLayout()
        self.completed_label = QLabel("المكتمل: —")
        self.total_label = QLabel("الإجمالي: —")
        self.remaining_label = QLabel("المتبقي: —")
        for label in (self.completed_label, self.total_label, self.remaining_label):
            label.setObjectName("metric")
            summary.addWidget(label)
        layout.addLayout(summary)
        cards = []
        self.status_cards = {}
        for key, value in STATUS_LABELS.items():
            card = QLabel(f"{value} · محاكاة\n—")
            card.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card.setObjectName("metric")
            card.setMinimumHeight(58)
            card.setWordWrap(True)
            self.status_cards[key] = card
            cards.append(card)
        layout.addWidget(CardGrid(cards, columns=(5, 3, 1), minimum=95))
        self.timing_label = QLabel("الوقت الفعلي: —    |    نتائج حاسمة في المحاكاة/دقيقة: —")
        self.timing_label.setWordWrap(True)
        layout.addWidget(self.timing_label)
        detail_toggle = QCheckBox("تفاصيل المعالجة والتخطي")
        layout.addWidget(detail_toggle)
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        self.stats.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.stats)
        self.stats.setVisible(False)
        detail_toggle.toggled.connect(self.stats.setVisible)
        self.schedule_label = QLabel()
        self.schedule_label.setWordWrap(True)
        layout.addWidget(self.schedule_label)
        self.run_note = QLabel("المحاكاة لا تتصل بسناب. السرعة المعروضة تخص نتائج المحاكاة فقط.")
        self.run_note.setWordWrap(True)
        layout.addWidget(self.run_note)
        summary_box = Card("إعدادات البحث المحدد", "info")
        summary_layout = summary_box.body
        self.job_summary = QLabel("لا يوجد بحث محدد بعد.")
        self.job_summary.setWordWrap(True)
        self.job_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        summary_layout.addWidget(self.job_summary)
        root_layout.addWidget(summary_box)
        root_layout.addStretch(1)
        self.search_tabs.addTab(page, "التشغيل")

    @staticmethod
    def make_table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(36)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    @staticmethod
    def fill_table(table, rows):
        selected = table.item(table.currentRow(), 0)
        selected_text = selected.text() if selected else None
        sorting = table.isSortingEnabled()
        header = table.horizontalHeader()
        table.setUpdatesEnabled(False)
        table.setSortingEnabled(False)
        blocker = QSignalBlocker(table)
        try:
            # ResizeToContents recalculates all rows for every inserted cell. Batch it once per page.
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            table.setRowCount(len(rows))
            for row_index, values in enumerate(rows):
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value if value is not None else "—"))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    table.setItem(row_index, column, item)
                if str(values[0]) == selected_text:
                    table.selectRow(row_index)
            if not table.property("columns_sized") and rows:
                table.resizeColumnsToContents()
                for column in range(table.columnCount()):
                    table.setColumnWidth(column, min(240, max(100, table.columnWidth(column))))
                table.setProperty("columns_sized", True)
        finally:
            del blocker
            table.setSortingEnabled(sorting)
            table.setUpdatesEnabled(True)

    def build_results(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            PageHeading("سناب · نتائج البحث", "سجل المحاكاة منفصل عن التحقق الحقيقي المعطّل", "file")
        )
        results_card = Card("النتائج والتصفية", "filter")
        layout.addWidget(results_card, 1)
        layout = results_card.body
        layout.setAlignment(Qt.AlignmentFlag(0))
        row = QGridLayout()
        self.status_filter = combo([("جميع الحالات", "")] + [(v, k) for k, v in STATUS_LABELS.items()])
        self.results_mode = combo([("محاكاة", "simulated"), ("حقيقي", "real")])
        self.result_search = text_input("بحث داخل اليوزر")
        self.result_sort = combo(
            [
                ("الأقدم أولًا", "oldest"),
                ("الأحدث أولًا", "newest"),
                ("الاسم تصاعديًا", "name"),
                ("الاسم تنازليًا", "name_desc"),
            ]
        )
        row.addWidget(self.status_filter, 0, 0)
        row.addWidget(self.results_mode, 0, 1)
        row.addWidget(self.result_search, 1, 0)
        row.addWidget(self.result_sort, 1, 1)
        row.addWidget(button("بحث / تحديث", lambda: self.refresh_results(reset=True)), 1, 2)
        layout.addLayout(row)
        self.result_table = self.make_table(
            [
                "اسم المستخدم",
                "النتيجة",
                "نوع الفحص",
                "وقت الفحص · الرياض",
                "المصدر",
                "تفسير النتيجة",
                "صلاحية الدليل",
            ]
        )
        self.results_presentation = ResultsPresentation(self.result_table, (0, 1, 2), "Snapchat")
        layout.addWidget(self.results_presentation)
        layout.addWidget(self.result_table, 1)
        self.result_table.cellDoubleClicked.connect(self.result_details)
        self.result_empty = QLabel("اختر بحثًا لعرض نتائجه. جميع النتائج الحالية محاكاة.")
        self.result_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result_empty.setWordWrap(True)
        layout.addWidget(self.result_empty)
        bottom = QHBoxLayout()
        self.results_previous = button("السابقة", lambda: self.page_results(-1))
        bottom.addWidget(self.results_previous)
        self.results_page_label = QLabel("صفحة 1")
        bottom.addWidget(self.results_page_label)
        self.results_next = button("التالية", lambda: self.page_results(1))
        bottom.addWidget(self.results_next)
        bottom.addWidget(button("تصدير TXT + CSV للبحث الحالي", self.export_job))
        layout.addLayout(bottom)
        self.search_tabs.addTab(page, "النتائج")
        for control in (self.results_mode, self.status_filter, self.result_sort):
            control.currentIndexChanged.connect(lambda *_: self.refresh_results(reset=True))
        self.result_search.returnPressed.connect(lambda: self.refresh_results(reset=True))

    def build_history(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            PageHeading(
                "سناب · السجل والعمليات", "استعادة العمليات المحفوظة وتصدير الأدلة حسب نوع الفحص", "clock"
            )
        )
        jobs_card = Card("العمليات المحفوظة", "layers")
        layout.addWidget(jobs_card)
        root_layout = layout
        layout = jobs_card.body
        self.job_table = self.make_table(
            ["البحث", "حالة التشغيل", "وقت الإنشاء · الرياض", "مدة التشغيل", "مصدر الأسماء"]
        )
        self.job_table.setMaximumHeight(125)
        self.job_table.setMinimumHeight(80)
        self.job_table.cellDoubleClicked.connect(lambda *_: self.load_selected_job())
        layout.addWidget(self.job_table)
        row = QHBoxLayout()
        row.addWidget(button("تحميل البحث المحدد", self.load_selected_job))
        row.addWidget(button("تحديث", self.refresh_jobs))
        row.addWidget(button("عمليات أقدم", lambda: self.page_jobs(1)))
        row.addWidget(button("عمليات أحدث", lambda: self.page_jobs(-1)))
        layout.addLayout(row)
        results_card = Card("تاريخ الأسماء", "file")
        root_layout.addWidget(results_card, 1)
        layout = results_card.body
        layout.setAlignment(Qt.AlignmentFlag(0))
        filters = QHBoxLayout()
        self.history_mode = combo([("سجل المحاكاة", "simulated"), ("السجل الحقيقي", "real")])
        self.history_search = text_input("بحث في السجل العام")
        filters.addWidget(self.history_mode)
        filters.addWidget(self.history_search)
        filters.addWidget(button("بحث", lambda: self.refresh_history(reset=True)))
        layout.addLayout(filters)
        self.history_table = self.make_table(
            [
                "اسم المستخدم",
                "آخر محاولة",
                "وقت المحاولة · الرياض",
                "آخر دليل صالح",
                "وقت الدليل · الرياض",
                "صلاحية المصدر",
            ]
        )
        self.history_table.setMinimumHeight(150)
        layout.addWidget(self.history_table, 1)
        self.history_empty = QLabel("لا توجد نتائج محفوظة ضمن هذا النوع من الفحص.")
        layout.addWidget(self.history_empty)
        pages = QHBoxLayout()
        pages.addWidget(button("السابقة", lambda: self.page_history(-1)))
        self.history_page_label = QLabel("صفحة 1")
        pages.addWidget(self.history_page_label)
        pages.addWidget(button("التالية", lambda: self.page_history(1)))
        pages.addWidget(button("إعادة فحص المحدد بمحاكاة جديدة", self.recheck_selected))
        layout.addLayout(pages)
        bottom = QHBoxLayout()
        bottom.addWidget(button("تصدير آخر النتائج الموثوقة", self.export_history))
        bottom.addWidget(button("نسخة احتياطية SQLite", self.backup_database))
        bottom.addWidget(button("استعادة نسخة احتياطية", self.restore_database))
        layout.addLayout(bottom)
        note = QLabel("نسخة قاعدة البيانات لا تشمل ملفات TXT الأصلية؛ يلزم إحضار الملفات المطابقة للاستكمال.")
        note.setWordWrap(True)
        layout.addWidget(note)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(page)
        self.search_tabs.addTab(area, "السجل")

    def read_config(self) -> SearchConfig:
        try:
            limit = int(self.limit.text()) if self.limit_enabled.isChecked() else None
        except ValueError:
            raise ValueError("حد الأسماء يجب أن يكون عددًا صحيحًا موجبًا.") from None
        repeat_target = normalize(self.repeat_target.text())
        if repeat_target == "أي":
            repeat_target = "any"
        filters = Filters(
            self.min_length.value(),
            self.max_length.value(),
            self.letters.isChecked(),
            self.digits.isChecked(),
            "".join(c for c, w in self.specials.items() if w.isChecked()),
            normalize(self.prefix.text()),
            normalize(self.suffix.text()),
            normalize(self.required.text()),
            repeat_target if self.repeat.isChecked() else "",
            self.repeat_count.value(),
            self.repeat_kind.currentData(),
            self.adjacency.currentData(),
        )
        config = SearchConfig(
            filters=filters,
            source=self.source.currentData(),
            generation_mode=self.mode.currentData(),
            categories=tuple(k for k, w in self.categories.items() if w.isChecked()),
            files=tuple(self.files.item(i).text() for i in range(self.files.count())),
            limit=limit,
            workers=self.workers.value(),
            auto_workers=self.auto_workers.isChecked(),
            queue_size=self.queue.value(),
            smart_history=self.smart_history.isChecked(),
            history_ttl_hours=self.ttl.value(),
            max_retries=self.retries.value(),
            retry_budget=self.retry_budget.value(),
            timeout_seconds=self.timeout.value(),
            circuit_threshold=self.circuit.value(),
        )
        config.validate()
        return config

    def read_schedule(self):
        config = ScheduleConfig(
            self.daily_start.isChecked(),
            self.start_time.time().toString("HH:mm"),
            self.daily_stop.isChecked(),
            self.stop_time.time().toString("HH:mm"),
            self.duration.isChecked(),
            self.duration_hours.value() * 60 + self.duration_minutes.value(),
            self.editor_timezone,
        )
        config.validate()
        return config

    def setting_controls(self):
        names = (
            "source",
            "mode",
            "min_length",
            "max_length",
            "letters",
            "digits",
            "prefix",
            "suffix",
            "required",
            "repeat",
            "repeat_target",
            "repeat_count",
            "repeat_kind",
            "adjacency",
            "limit_enabled",
            "limit",
            "auto_workers",
            "workers",
            "queue",
            "smart_history",
            "ttl",
            "retries",
            "retry_budget",
            "timeout",
            "circuit",
            "daily_start",
            "start_time",
            "daily_stop",
            "stop_time",
            "duration",
            "duration_hours",
            "duration_minutes",
        )
        return {
            **{name: getattr(self, name) for name in names},
            **self.categories,
            **{f"special_{key}": value for key, value in self.specials.items()},
        }

    def editor_signature(self):
        state = {}
        for name, widget in self.setting_controls().items():
            if isinstance(widget, QCheckBox):
                state[name] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                state[name] = widget.currentData()
            elif isinstance(widget, QTimeEdit):
                state[name] = widget.time().toString("HH:mm")
            elif isinstance(widget, QLineEdit):
                state[name] = widget.text()
            else:
                state[name] = widget.value()
        state["files"] = [self.files.item(i).text() for i in range(self.files.count())]
        state["timezone"] = self.editor_timezone
        return state

    @property
    def preset_dirty(self):
        return self.editor_signature() != self._baseline

    def connect_settings_changes(self):
        for widget in self.setting_controls().values():
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(self.settings_changed)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self.settings_changed)
            elif isinstance(widget, QTimeEdit):
                widget.timeChanged.connect(self.settings_changed)
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(self.settings_changed)
            else:
                widget.valueChanged.connect(self.settings_changed)
        self.preset_selector.currentIndexChanged.connect(self.select_preset)
        self.settings_changed()

    def settings_changed(self, *_):
        generated = self.source.currentData() == "generator"
        self.mode.setVisible(generated)
        self.mode_label.setVisible(generated)
        self.txt_group.setVisible(not generated)
        meaningful = generated and self.mode.currentData() != "all"
        self.meaning_group.setEnabled(meaningful)
        self.meaning_hint.setVisible(not meaningful)
        for widget in (self.repeat_target, self.repeat_count, self.repeat_kind, self.adjacency):
            widget.setEnabled(self.repeat.isChecked())
        self.limit.setEnabled(self.limit_enabled.isChecked())
        self.start_time.setEnabled(self.daily_start.isChecked())
        self.stop_time.setEnabled(self.daily_stop.isChecked())
        self.duration_hours.setEnabled(self.duration.isChecked())
        self.duration_minutes.setEnabled(self.duration.isChecked())
        self.ttl.setEnabled(self.smart_history.isChecked())
        self.retry_budget.setEnabled(self.retries.value() > 0)
        self.preset_rename.setEnabled(self.preset_id is not None)
        self.preset_delete.setEnabled(self.preset_id is not None)
        if not self._loading_settings:
            dirty = self.preset_dirty
            self.preset_notice.setText(
                "تعديلات غير محفوظة"
                if dirty
                else (
                    "القالب محفوظ. تحميله لا يشغّل بحثًا."
                    if self.preset_id
                    else "إعدادات جديدة؛ يمكنك حفظها باسم لاستخدامها لاحقًا."
                )
            )
            self.search_tabs.setTabText(0, "الإعدادات •" if dirty else "الإعدادات")

    def refresh_presets(self):
        with QSignalBlocker(self.preset_selector):
            self.preset_selector.clear()
            self.preset_selector.addItem("إعدادات جديدة — غير محفوظة", None)
            for row in self.store.presets():
                self.preset_selector.addItem(row["name"], row["id"])
            self.preset_selector.setCurrentIndex(max(0, self.preset_selector.findData(self.preset_id)))

    def ask(self, title, text, buttons=QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel):
        dialog = QMessageBox(QMessageBox.Icon.Question, title, text, buttons, self)
        labels = {
            QMessageBox.StandardButton.Ok: "متابعة",
            QMessageBox.StandardButton.Cancel: "إلغاء",
            QMessageBox.StandardButton.Yes: "نعم",
            QMessageBox.StandardButton.No: "لا",
            QMessageBox.StandardButton.Save: "حفظ",
            QMessageBox.StandardButton.Discard: "تجاهل التعديلات",
        }
        for key, label in labels.items():
            if dialog.button(key):
                dialog.button(key).setText(label)
        dialog.setDefaultButton(
            QMessageBox.StandardButton.Cancel
            if dialog.button(QMessageBox.StandardButton.Cancel)
            else QMessageBox.StandardButton.No
        )
        return dialog.exec()

    def unsaved_choice(self):
        return self.ask(
            "تعديلات غير محفوظة",
            "هل تريد حفظ إعدادات البحث قبل المتابعة؟",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )

    def guard_settings(self, action):
        if not self.preset_dirty:
            action()
            return
        choice = self.unsaved_choice()
        if choice == QMessageBox.StandardButton.Save:
            self.save_preset(on_saved=action)
        elif choice == QMessageBox.StandardButton.Discard:
            action()

    def select_preset(self, *_):
        selected = self.preset_selector.currentData()
        if selected == self.preset_id or self._loading_settings:
            return
        with QSignalBlocker(self.preset_selector):
            self.preset_selector.setCurrentIndex(max(0, self.preset_selector.findData(self.preset_id)))
        self.guard_settings(lambda: self.load_preset(selected))

    def load_preset(self, preset_id):
        if self.busy:
            return
        if preset_id is None:
            self.apply_settings(SearchConfig(), ScheduleConfig(), {})
            self.preset_id, self.preset_files = None, []
            self._baseline = self.editor_signature()
            self.refresh_presets()
            self.settings_changed()
            return
        self.busy = True
        self.settings_page.setEnabled(False)

        def read():
            try:
                row = self.store.preset(preset_id)
                return row, file_issues(row["payload"])
            except ValueError as exc:
                return None, [str(exc)]

        def loaded(result):
            row, issues = result
            if row is None:
                self.preset_id = preset_id
                self.preset_files = []
                self._baseline = None
                self.busy = False
                self.settings_page.setEnabled(True)
                self.refresh_presets()
                self.settings_changed()
                self.preset_notice.setText("؛ ".join(issues) + " يمكنك حذفه أو حفظ إعداداتك الحالية مكانه.")
                return
            config, schedule, editor = validate_payload(row["payload"])
            self.apply_settings(config, schedule, editor)
            self.preset_id = row["id"]
            self.preset_files = row["payload"].get("files", [])
            self._baseline = self.editor_signature()
            self.busy = False
            self.settings_page.setEnabled(True)
            self.refresh_presets()
            self.settings_changed()
            if issues:
                self.preset_notice.setText("؛ ".join(issues) + " — أعد اختيار الملفات قبل إنشاء البحث.")

        self.submit(read, loaded, exclusive=True)

    def apply_settings(self, config, schedule, editor):
        self._loading_settings = True
        try:
            values = {**asdict(config.filters), **asdict(config), **asdict(schedule)}
            values.pop("filters")
            values.update(
                mode=config.generation_mode,
                queue=config.queue_size,
                ttl=config.history_ttl_hours,
                retries=config.max_retries,
                timeout=config.timeout_seconds,
                circuit=config.circuit_threshold,
                limit_enabled=config.limit is not None,
                limit=editor.get("limit_text", str(config.limit or 1000)),
                repeat=bool(config.filters.repeat_target),
                repeat_kind=config.filters.repeat_exact,
                repeat_target=editor.get("repeat_target", config.filters.repeat_target or "any"),
                daily_start=schedule.start_enabled,
                daily_stop=schedule.stop_enabled,
                duration=schedule.duration_enabled,
                duration_hours=schedule.duration_minutes // 60,
                duration_minutes=schedule.duration_minutes % 60,
            )
            for name, widget in self.setting_controls().items():
                if name in self.categories:
                    widget.setChecked(name in config.categories)
                elif name.startswith("special_"):
                    widget.setChecked(name[-1] in config.filters.specials)
                elif name in values:
                    value = values[name]
                    if name == "repeat_target" and value == "any":
                        value = "أي"
                    if isinstance(widget, QCheckBox):
                        widget.setChecked(bool(value))
                    elif isinstance(widget, QComboBox):
                        widget.setCurrentIndex(widget.findData(value))
                    elif isinstance(widget, QTimeEdit):
                        widget.setTime(QTime.fromString(value, "HH:mm"))
                    elif isinstance(widget, QLineEdit):
                        widget.setText(str(value))
                    else:
                        widget.setValue(value)
            self.files.clear()
            self.files.addItems(list(config.files))
            self.editor_timezone = schedule.timezone
            self.timezone_hint.setText(
                f"التوقيت: {schedule.timezone} • يلزم بقاء التطبيق مفتوحًا؛ الإيقاف المؤقت لا يُحتسب."
            )
        finally:
            self._loading_settings = False

    def ask_preset_name(self, title, initial=""):
        dialog = QInputDialog(self)
        dialog.setWindowTitle(title)
        dialog.setLabelText("اسم القالب")
        dialog.setTextValue(initial)
        dialog.setOkButtonText("حفظ")
        dialog.setCancelButtonText("إلغاء")
        return dialog.textValue() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def save_preset(self, _checked=False, *, save_as=False, on_saved=None):
        if self.busy:
            return
        try:
            config, schedule = self.read_config(), self.read_schedule()
            preset_id = None if save_as else self.preset_id
            name = self.preset_selector.currentText() if preset_id else self.ask_preset_name("حفظ قالب بحث")
            if name is None:
                return
            self.store._preset_name(name)
            editor = {"repeat_target": self.repeat_target.text(), "limit_text": self.limit.text()}
            self.busy = True
            self.settings_page.setEnabled(False)
            expected_files = list(self.preset_files) if config.source == "txt" else []

            def save():
                issues = file_issues({"files": expected_files})
                if issues:
                    raise ValueError("؛ ".join(issues) + " — أعد اختيار الملفات قبل تحديث القالب.")
                payload = make_payload(config, schedule, editor)
                return self.store.save_preset(name, payload, preset_id), payload

            def saved(result):
                self.preset_id, payload = result
                self.preset_files = payload.get("files", [])
                self._baseline = self.editor_signature()
                self.busy = False
                self.settings_page.setEnabled(True)
                self.refresh_presets()
                self.settings_changed()
                missing = [
                    Path(item["path"]).name for item in payload.get("files", []) if not item.get("sha256")
                ]
                if missing:
                    self.preset_notice.setText(
                        "ملفات غير متاحة: " + "، ".join(missing) + " — أعد اختيارها قبل إنشاء البحث."
                    )
                if on_saved:
                    on_saved()

            self.submit(save, saved, exclusive=True)
        except (ValueError, OSError) as exc:
            self.show_error(str(exc))

    def rename_preset(self):
        if not self.preset_id or self.busy:
            return
        try:
            name = self.ask_preset_name("إعادة تسمية القالب", self.preset_selector.currentText())
            if name is not None:
                self.store.rename_preset(self.preset_id, name)
                self.refresh_presets()
        except ValueError as exc:
            self.show_error(str(exc))

    def delete_preset(self):
        if not self.preset_id or self.busy:
            return
        if (
            self.ask(
                "حذف القالب",
                "هل تريد حذف هذا القالب؟ لن تُحذف العمليات أو النتائج.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.store.delete_preset(self.preset_id)
        self.preset_id, self.preset_files = None, []
        self._baseline = None  # Keep the current settings as a visible unsaved draft.
        self.refresh_presets()
        self.settings_changed()

    def submit(self, function, callback, *, exclusive=False):
        if not exclusive and (self.busy or sum(not f.done() for f in self.futures) >= 2):
            self.statusBar().showMessage("انتظر اكتمال العمليات الجارية.")
            return
        future = self.executor.submit(function)
        self.futures.add(future)

        def done(result):
            try:
                value = result.result()
                self.bridge.completed.emit(callback, value)
            except Exception as exc:
                self.bridge.failed.emit(
                    str(exc) if isinstance(exc, ValueError) else type(exc).__name__, exclusive
                )

        future.add_done_callback(done)

    def _callback(self, callback, value):
        if self.closed:
            return
        self.futures = {f for f in self.futures if not f.done()}
        callback(value)

    def show_error(self, message, release_busy=True):
        if self.closed:
            return
        self.futures = {f for f in self.futures if not f.done()}
        if release_busy:
            self.busy = False
            self.new_button.setEnabled(True)
            self.settings_page.setEnabled(True)
        self.statusBar().showMessage("تعذر إتمام العملية")
        QMessageBox.warning(self, "تعذر إتمام العملية", ERROR_LABELS.get(message, message))

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "اختر ملفات أسماء المستخدمين", "", "Text files (*.txt)")
        existing = {self.files.item(i).text() for i in range(self.files.count())}
        for path in files:
            if path not in existing:
                self.files.addItem(path)
                existing.add(path)
        if files:
            self.preset_files = []
            self.settings_changed()

    def remove_file(self):
        row = self.files.currentRow()
        if row >= 0:
            self.files.takeItem(row)
            self.preset_files = []
            self.settings_changed()

    def about_data(self):
        try:
            manifest = MeaningLibrary().manifest
            text = "مجموعات منتقاة صغيرة؛ ليست قواميس شاملة. لا توجد درجات شيوع wordfreq في هذا الإصدار.\n\n"
            for p in manifest["packages"]:
                text += f"{CATEGORIES[p['category']]}: {p['count']} • {p['version']}\n{p['license']}\n"
                text += f"{p['source_url']}\n{p['attribution']}\nSHA-256: {p['sha256']}\n\n"
            dialog = QDialog(self)
            dialog.setWindowTitle("حول البيانات والمصادر")
            dialog.resize(780, 600)
            layout = QVBoxLayout(dialog)
            editor = QTextEdit()
            editor.setReadOnly(True)
            editor.setPlainText(text)
            layout.addWidget(editor)
            box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            box.rejected.connect(dialog.reject)
            layout.addWidget(box)
            dialog.exec()
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))

    @staticmethod
    def describe_config(config):
        if config.source == "txt":
            return "عدد المرشحين الفريدين غير معلوم قبل قراءة الملفات؛ يتحدث تدريجيًا أثناء التشغيل."
        if config.generation_mode == "meaningful":
            entries = MeaningLibrary().entries(config.categories)
            count = sum(config.filters.matches(name) for name, _ in entries)
            return f"عدد الأسماء الأصلية المطابقة الدقيق: {count:,}" + (
                " — لا توجد عناصر تطابق الشروط." if not count else ""
            )
        # All meaningful candidates satisfy the same full universe; mixed mode is a union, not a sum.
        return GeneratorSpace(config.filters).describe()

    def preview(self):
        if self.busy or sum(not future.done() for future in self.futures) >= 2:
            self.statusBar().showMessage("انتظر اكتمال العمليات الجارية ثم أعد المعاينة.")
            return
        self._preview_request_id += 1
        request_id = self._preview_request_id
        self.statusBar().showMessage("جارٍ إعداد معاينة البحث…")
        self.preview_label.setText("جارٍ إعداد المعاينة…")
        self.preview_label.setToolTip("")
        self.last_preview_diagnostics = {}
        try:
            config = self.read_config()
            signature = self.editor_signature()
            expected = {
                str(Path(resolve_path(item["path"])).resolve()): item["sha256"] for item in self.preset_files
            }

            def read():
                try:
                    if config.source == "txt":
                        return preview_txt(config, expected)
                    return {"message": self.describe_config(config), "level": "success", "diagnostics": {}}
                except (ValueError, OSError) as exc:
                    message = (
                        str(exc)
                        if isinstance(exc, ValueError)
                        else "تعذر قراءة مصدر الأسماء؛ تحقق من الملفات المختارة."
                    )
                    return {
                        "message": message,
                        "level": "warning",
                        "diagnostics": {"error": type(exc).__name__},
                    }

            def ready(result):
                if request_id != self._preview_request_id:
                    return
                if signature != self.editor_signature():
                    self.preview_label.setText("تغيرت الإعدادات؛ اضغط معاينة البحث لتحديث النتيجة.")
                    self.statusBar().showMessage("المعاينة السابقة لا تخص الإعدادات الحالية.")
                    return
                self.display_preview(result)

            self.submit(read, ready)
        except (ValueError, OSError) as exc:
            self.display_preview(
                {"message": str(exc), "level": "warning", "diagnostics": {"error": type(exc).__name__}}
            )

    def display_preview(self, result):
        self.preview_label.setText(result["message"])
        self.last_preview_diagnostics = result.get("diagnostics", {})
        self.preview_label.setToolTip(
            "تفاصيل تشخيصية:\n" + json.dumps(self.last_preview_diagnostics, ensure_ascii=False, indent=2)
        )
        self.statusBar().showMessage(
            "اكتملت المعاينة."
            if result["level"] == "success"
            else "انتهت المعاينة بملاحظات؛ راجع التفاصيل أسفل الإعدادات."
        )

    def prepare_job(self):
        if self.is_running() or self.busy:
            self.statusBar().showMessage("أوقف العملية الحالية وانتظر اكتمال تجهيز البيانات.")
            return
        try:
            config = self.read_config()
            schedule = self.read_schedule()
            description = self.describe_config(config)
            if config.source == "generator" and config.generation_mode != "meaningful":
                if GeneratorSpace(config.filters).total == 0:
                    raise ValueError(description)
            text = (
                f"{description}\nملفات TXT: {len(config.files)}\nسقف العمال: {config.workers}\n"
                f"حد الأسماء: {config.limit if config.limit else 'دون حد — قد تكون المساحة هائلة'}\n"
                f"تشغيل يومي: {schedule.start_time if schedule.start_enabled else 'معطّل'}\n"
                f"إيقاف يومي: {schedule.stop_time if schedule.stop_enabled else 'معطّل'}\n"
                f"مدة فعلية: {schedule.duration_minutes if schedule.duration_enabled else 'معطّلة'} دقيقة\n"
                "المنطقة Asia/Riyadh\nكل نتائج الفحص محاكاة محلية. إنشاء البحث لا يبدأ التشغيل تلقائيًا."
            )
            if (
                self.ask(
                    "مراجعة قبل الإنشاء",
                    text,
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                )
                != QMessageBox.StandardButton.Ok
            ):
                return
            self.busy = True
            self.new_button.setEnabled(False)
            self.statusBar().showMessage("تجهيز بصمات الملفات والإعدادات…")
            scheduler = Scheduler(schedule)
            expected_files = list(self.preset_files) if config.source == "txt" else []

            def create():
                issues = file_issues({"files": expected_files})
                if issues:
                    raise ValueError("؛ ".join(issues) + " — أعد اختيار ملفات TXT قبل إنشاء البحث.")
                return self.store.create_job(config, make_snapshot(config), scheduler.serialize())

            self.submit(create, self.load_job, exclusive=True)
        except (ValueError, OSError) as exc:
            self.show_error(str(exc))

    def load_job(self, job_id):
        if self.is_running():
            return
        self.busy = False
        self.new_button.setEnabled(True)
        self.current_job = job_id
        job = self.store.job(job_id)
        data = job["schedule"]
        self.scheduler = Scheduler(ScheduleConfig(**data.get("config", {})), data.get("journal", {}))
        self.scheduler.tick(datetime.now(UTC), job["state"], job["active_seconds"])
        self.offset = 0
        self.update_engine(ExecutionEngine(self.store).snapshot(job_id))
        self.refresh_jobs()
        self.refresh_results()
        self.tabs.setCurrentWidget(self.snapchat_tabs)
        self.snapchat_tabs.setCurrentWidget(self.search_tabs)
        self.search_tabs.setCurrentIndex(1)
        self.statusBar().showMessage("حُمّل البحث بإعداداته المحفوظة • محاكاة محلية")

    def load_selected_job(self):
        row = self.job_table.currentRow()
        if row >= 0 and not self.is_running():
            item = self.job_table.item(row, 0)
            self.load_job(item.data(Qt.ItemDataRole.UserRole) or item.text())

    def is_running(self):
        return bool(self.engine_thread and self.engine_thread.is_alive())

    def start_engine(self):
        if self.is_running() or self.busy or not self.current_job:
            return
        job = self.store.job(self.current_job)
        if job["state"] in {JobState.BLOCKED, JobState.COMPLETED}:
            self.statusBar().showMessage("هذا البحث مكتمل أو محظور؛ أنشئ بحثًا جديدًا عند الحاجة.")
            return
        self.engine = ExecutionEngine(self.store, on_update=self.bridge.updated.emit)
        job_id = self.current_job
        engine = self.engine

        def run():
            try:
                asyncio.run(engine.run(job_id))
            finally:
                self.bridge.finished.emit()

        self.engine_thread = threading.Thread(target=run, name="username-engine", daemon=False)
        self.engine_thread.start()
        self.start_button.setEnabled(False)
        self.new_button.setEnabled(False)

    def pause_engine(self):
        if self.engine and self.is_running():
            self.engine.request_pause()

    def stop_engine(self):
        if self.engine and self.is_running():
            self.engine.request_stop()

    def recover_blocked_job(self):
        if not self.current_job or self.is_running() or self.busy:
            return
        job_id = self.current_job
        job = self.store.job(job_id)
        if job["state"] != JobState.BLOCKED:
            return
        self.busy = True

        def validate():
            config = SearchConfig.from_dict(job["config"])
            if config.provider != "mock":
                raise ValueError(ERROR_LABELS["LIVE_VERIFICATION_NOT_VALIDATED"])
            if self.store.provider_quarantined("local-mock", "1"):
                raise ValueError(ERROR_LABELS["PROVIDER_QUARANTINED"])
            if make_snapshot(config) != job["snapshot"]:
                raise ValueError("تغير المصدر؛ لا يمكن استكمال هذا البحث. ابدأ بحثًا جديدًا.")
            # A short committed write verifies that the persistence layer accepts writes again.
            self.store.set_state(job_id, JobState.STOPPED)
            return job_id

        self.submit(validate, self.load_job, exclusive=True)

    def update_engine(self, data):
        state = data["state"]
        label = STATE_LABELS.get(state, state)
        if data["error"]:
            label += " • " + ERROR_LABELS.get(data["error"], data["error"])
        self.state_label.setText(f"البحث {data['job_id'][:12]} • {label} • محاكاة")
        c = data["counters"]
        stored_megabytes = (data["storage"]["database_bytes"] + data["storage"]["wal_bytes"]) / 2**20
        self.stats.setText(
            f"الأسطر / العناصر المقروءة: {c['lines']:,}    فارغ: {c['blank']:,}    تالف: {c['malformed']:,}\n"
            f"غير صالح محليًا: {c['invalid']:,}    مستبعد بالفلاتر: {c['filtered']:,}    "
            f"مكرر: {c['duplicate']:,}\n"
            f"فريد: {c['unique']:,}    متخطى بالسجل: {c['skipped']:,}    محاولات مرسلة: {c['dispatched']:,}\n"
            f"مكتمل: {c['completed']:,}    إعادات: {c['retries']:,}    غير مكتمل: {data['pending']:,}\n"
            f"الوقت الفعلي: {data['active_seconds']:.1f} ثانية    "
            f"العمال الفعليون: {data['effective_workers']}\n"
            f"النتائج الحاسمة في المحاكاة/دقيقة: {data['decisive_per_minute']:.1f}\n"
            f"المساحة الحرة: {data['storage']['free_bytes'] / 2**30:.2f} GB    "
            f"حجم البيانات: {stored_megabytes:.1f} MB"
        )
        job = self.store.job(data["job_id"])
        config = SearchConfig.from_dict(job["config"])
        mode = "simulated" if config.provider == "mock" else "real"
        counts = self.store.result_counts(data["job_id"], mode)
        for key, card in self.status_cards.items():
            card.setText(f"{status_text(key, mode)}\n{counts[key]:,}")
        complete = max(0, c["unique"] - data["pending"])
        self.completed_label.setText(f"أسماء عولجت: {complete:,}")
        if job["source_done"]:
            total = c["unique"]
            self.total_label.setText(f"الإجمالي: {total:,}")
            self.remaining_label.setText(f"المتبقي: {data['pending']:,}")
        elif config.limit:
            self.total_label.setText(f"الحد المستهدف: {config.limit:,}")
            self.remaining_label.setText(f"لبلوغ الحد: {max(0, config.limit - complete):,}")
        else:
            self.total_label.setText("الإجمالي: يُحدَّث أثناء القراءة")
            self.remaining_label.setText(f"قيد المعالجة: {data['pending']:,}")
        seconds = int(data["active_seconds"])
        elapsed = f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"
        self.timing_label.setText(
            f"وقت التشغيل الفعلي: {elapsed}  |  "
            f"نتائج حاسمة في المحاكاة/دقيقة: {data['decisive_per_minute']:.1f}"
        )
        resumable = state in {JobState.READY, JobState.PAUSED, JobState.STOPPED, JobState.INTERRUPTED}
        self.start_button.setEnabled(resumable and not self.is_running())
        self.start_button.setText(
            "البحث مكتمل"
            if state == JobState.COMPLETED
            else ("بدء البحث" if state == JobState.READY else "استكمال البحث")
        )
        modes = {"all": "جميع التركيبات", "meaningful": "أسماء ذات معنى", "mixed": "المعاني والتركيبات"}
        source = f"{len(config.files)} ملفات TXT" if config.source == "txt" else modes[config.generation_mode]
        self.job_summary.setText(
            f"المصدر: {source}    |    "
            f"الطول: من {config.filters.min_length} إلى {config.filters.max_length}\n"
            f"سقف الفحوص المتزامنة: {config.workers}    |    "
            f"السجل الذكي: {'مفعّل' if config.smart_history else 'معطّل'}\n"
            "هذه إعدادات العملية المحفوظة؛ تغيير القالب أو محرر الإعدادات لا يغيّرها."
        )
        if state == JobState.COMPLETED:
            self.progress.setRange(0, 1000)
            self.progress.setValue(1000)
        elif config.limit:
            self.progress.setRange(0, 1000)
            finished_candidates = max(0, c["unique"] - data["pending"])
            self.progress.setValue(min(1000, finished_candidates * 1000 // config.limit))
        else:
            self.progress.setRange(0, 0 if state == JobState.RUNNING else 1000)
        self.progress.setFormat(
            "%p% من حد الأسماء" if config.limit and state != JobState.COMPLETED else "%p%"
        )
        self.pause_button.setEnabled(state == JobState.RUNNING)
        self.stop_button.setEnabled(state == JobState.RUNNING)
        self.recover_button.setEnabled(state == JobState.BLOCKED)

    def engine_finished(self):
        state = self.store.job(self.current_job)["state"] if self.current_job else None
        self.start_button.setEnabled(state in {JobState.PAUSED, JobState.STOPPED, JobState.INTERRUPTED})
        self.new_button.setEnabled(True)
        self.refresh_results()
        self.refresh_history()
        self.refresh_jobs()

    def tick(self):
        if self.closing:
            if not self.is_running() and not any(not f.done() for f in self.futures):
                self.close()
            return
        if self.busy:
            return
        if self.current_job and self.scheduler:
            job = self.store.job(self.current_job)
            actions = self.scheduler.tick(datetime.now(UTC), job["state"], job["active_seconds"])
            for action in actions:
                if action == "stop":
                    self.stop_engine()
                else:
                    self.start_engine()
            # Save schedule decisions, not on every 250ms UI tick.
            serialized = self.scheduler.serialize()
            previous = job["schedule"].get("journal", {}).get("last_seen", "")
            now_seen = serialized["journal"].get("last_seen", "")
            if actions or not previous or now_seen[:16] != previous[:16]:
                self.store.update_schedule(self.current_job, serialized)
            remaining = max(
                0,
                self.scheduler.config.duration_minutes * 60
                - (job["active_seconds"] - self.scheduler.journal.get("duration_baseline", 0)),
            )
            events = self.scheduler.next_events(datetime.now(UTC))
            self.schedule_label.setText(
                "توقيت الرياض • "
                + " | ".join(
                    f"{'التشغيل التالي' if k == 'start' else 'الإيقاف التالي'}: {local_time(v)}"
                    for k, v in events.items()
                )
                + (
                    f" • متبقي من المدة: {remaining / 60:.1f} دقيقة"
                    if self.scheduler.config.duration_enabled
                    else ""
                )
            )

    def refresh_results(self, reset=False):
        if reset:
            self.offset = 0
        rows = []
        total = 0
        if self.current_job:
            self.result_empty.setText("جارٍ تحميل النتائج…")
            try:
                page = self.store.result_page(
                    self.current_job,
                    mode=self.results_mode.currentData(),
                    classification=self.status_filter.currentData(),
                    search=normalize(self.result_search.text()),
                    offset=self.offset,
                    sort=self.result_sort.currentData(),
                )
                rows, total, self.offset = page["rows"], page["total"], page["offset"]
            except (OSError, ValueError, sqlite3.Error):
                self._result_rows = []
                self.results_total = 0
                self.fill_table(self.result_table, [])
                self.result_empty.setText("تعذر تحميل النتائج؛ أعد المحاولة.")
                self.result_empty.setVisible(True)
                self.results_page_label.setText("تعذر تحميل الصفحة")
                self.results_next.setEnabled(False)
                self.results_previous.setEnabled(False)
                self.statusBar().showMessage("تعذر قراءة النتائج؛ استخدم بحث / تحديث لإعادة المحاولة.")
                return
        self._result_rows = rows
        self.results_total = total
        self.fill_table(
            self.result_table,
            [
                [
                    r["username"],
                    status_text(r["effective_classification"], r["mode"]),
                    "محاكاة محلية" if r["mode"] == "simulated" else "فحص حقيقي",
                    local_time(r["checked_at"]),
                    "المحاكي المحلي"
                    if r["provider"] == "local-mock"
                    else ("قواعد الصيغة المحلية" if r["provider"] == "local-syntax" else r["provider"]),
                    REASONS.get(r["kind"], "راجع تفاصيل الدليل"),
                    "محجور"
                    if r["quarantined"]
                    else (
                        "صالح للمحاكاة فقط"
                        if r["mode"] == "simulated" and r["trustworthy"]
                        else ("صالح" if r["trustworthy"] else "غير حاسم")
                    ),
                ]
                for r in rows
            ],
        )
        for row_index, row in enumerate(rows):
            self.result_table.item(row_index, 3).setToolTip(row["checked_at"])
            self.result_table.item(row_index, 5).setToolTip(
                f"{row['kind']} • {row['provider']} / {row['provider_version']}"
            )
        pages = max(1, (total + 99) // 100)
        self.results_page_label.setText(f"صفحة {self.offset // 100 + 1} من {pages}  ·  {total:,} نتيجة")
        self.results_previous.setEnabled(self.offset > 0)
        self.results_next.setEnabled(self.offset + 100 < total)
        self.result_empty.setText(
            "لا توجد نتائج تطابق البحث والتصفية."
            if self.current_job
            else "اختر بحثًا من السجل أو أنشئ بحثًا جديدًا لعرض النتائج."
        )
        self.result_empty.setVisible(not rows)

    def result_details(self, row, _column=0):
        if not 0 <= row < len(self._result_rows):
            return
        data = self._result_rows[row]
        dialog = QDialog(self)
        dialog.setWindowTitle("تفاصيل نتيجة محاكاة" if data["mode"] == "simulated" else "تفاصيل النتيجة")
        dialog.resize(650, 480)
        layout = QVBoxLayout(dialog)
        if data["mode"] == "simulated":
            layout.addWidget(QLabel("هذه نتيجة افتراضية؛ لا تؤكد حالة الاسم على Snapchat."))
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
        text.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        layout.addWidget(text)
        layout.addWidget(button("إغلاق", dialog.accept))
        dialog.exec()

    def refresh_jobs(self):
        jobs = self.store.jobs(offset=self.jobs_offset)
        self.fill_table(
            self.job_table,
            [
                [
                    f"بحث · {j['id'][:10]}",
                    STATE_LABELS.get(j["state"], j["state"]),
                    local_time(j["created_at"]),
                    f"{j['active_seconds']:.0f} ثانية",
                    "ملفات نصية"
                    if json.loads(j["config"])["source"] == "txt"
                    else {"all": "تركيبات", "mixed": "معانٍ وتركيبات", "meaningful": "أسماء ذات معنى"}.get(
                        json.loads(j["config"])["generation_mode"], "توليد"
                    ),
                ]
                for j in jobs
            ],
        )
        for index, job in enumerate(jobs):
            item = self.job_table.item(index, 0)
            item.setData(Qt.ItemDataRole.UserRole, job["id"])
            item.setToolTip(job["id"])

    def refresh_history(self, reset=False):
        if reset:
            self.history_offset = 0
        rows = self.store.history_page(
            mode=self.history_mode.currentData(),
            search=normalize(self.history_search.text()),
            offset=self.history_offset,
        )
        self.fill_table(
            self.history_table,
            [
                [
                    r["username"],
                    status_text(r["latest"], r["mode"]),
                    local_time(r["latest_at"]),
                    status_text(r["trusted"], r["mode"]),
                    local_time(r["trusted_at"]),
                    "محجور" if r["quarantined"] else "غير محجور",
                ]
                for r in rows
            ],
        )
        total = self.store.history_count(
            self.history_mode.currentData(), normalize(self.history_search.text())
        )
        self.history_page_label.setText(
            f"صفحة {self.history_offset // 100 + 1} من {max(1, (total + 99) // 100)} · {total:,} اسم"
        )
        self.history_empty.setVisible(not rows)

    def page_results(self, direction):
        self.offset = max(
            0, min(self.offset + direction * 100, max(0, (self.results_total - 1) // 100 * 100))
        )
        self.refresh_results()

    def page_history(self, direction):
        total = self.store.history_count(
            self.history_mode.currentData(), normalize(self.history_search.text())
        )
        self.history_offset = max(
            0, min(self.history_offset + direction * 100, max(0, (total - 1) // 100 * 100))
        )
        self.refresh_history()

    def page_jobs(self, direction):
        total = self.store.job_count()
        self.jobs_offset = max(0, min(self.jobs_offset + direction * 100, max(0, (total - 1) // 100 * 100)))
        self.refresh_jobs()

    def recheck_selected(self):
        row = self.history_table.currentRow()
        if row < 0 or self.is_running() or self.busy:
            return
        name = self.history_table.item(row, 0).text()
        from .domain import syntax_error

        if syntax_error(name):
            self.show_error("الاسم يخالف الصيغة المحلية؛ لا يحتاج اتصالًا أو إعادة فحص.")
            return
        config = SearchConfig(
            filters=Filters(len(name), len(name), specials="_.-", prefix=name), limit=1, smart_history=False
        )
        job = self.store.create_job(config, make_snapshot(config))
        self.load_job(job)
        self.start_engine()

    def export_job(self):
        if not self.current_job:
            return
        directory = QFileDialog.getExistingDirectory(self, "مجلد التصدير — أسماء الملفات توضح وضع المحاكاة")
        if directory:
            job_id = self.current_job
            mode = self.results_mode.currentData()
            self.submit(
                lambda: self.store.export(Path(directory), mode=mode, job_id=job_id),
                lambda paths: self.statusBar().showMessage(f"صُدّرت {len(paths)} ملفات مكتملة إلى {directory}"),
            )

    def export_history(self):
        directory = QFileDialog.getExistingDirectory(self, "تصدير السجل الموثوق — يتضمن وقت الدليل والوضع")
        if directory:
            mode = self.history_mode.currentData()
            self.submit(
                lambda: self.store.export(Path(directory), mode=mode, trusted=True),
                lambda paths: self.statusBar().showMessage(f"صُدّرت {len(paths)} ملفات للسجل"),
            )

    def backup_database(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "حفظ نسخة SQLite — دون ملفات TXT الأصلية", "history-backup.db", "SQLite (*.db)"
        )
        if filename:
            self.submit(
                lambda: self.store.backup(Path(filename)),
                lambda _: self.statusBar().showMessage("حُفظت نسخة احتياطية سليمة"),
            )

    def restore_database(self):
        if (
            self.is_running()
            or self.busy
            or self.roblox.busy
            or self.x_panel.busy
            or any(not f.done() for f in self.futures)
        ):
            self.statusBar().showMessage("أوقف المحرك وانتظر العمليات الجارية قبل الاستعادة.")
            return
        filename, _ = QFileDialog.getOpenFileName(self, "استعادة SQLite", "", "SQLite (*.db)")
        if not filename:
            return
        if (
            self.ask(
                "استعادة السجل",
                "سيُستبدل السجل الحالي وتُحفظ نسخة أمان منه أولًا. هل تريد المتابعة؟",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        safety = (
            self.store.path.parent
            / "Backups"
            / f"before-restore-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.db"
        )
        self.busy = True

        def restore():
            self.store.restore(Path(filename), safety)

        def restored(_):
            self.busy = False
            self.current_job = None
            self.scheduler = None
            self.refresh_jobs()
            self.refresh_history()
            self.refresh_results()
            for panel, platform in ((self.roblox, "Roblox"), (self.x_panel, "X")):
                panel.overnight.repo = type(panel.overnight.repo)(self.store, platform)
                panel.overnight.enabled.blockSignals(True)
                panel.overnight.enabled.setChecked(False)
                panel.overnight.enabled.blockSignals(False)
            self.roblox.current_run = None
            self.roblox.repo.recover()
            self.roblox.refresh()
            self.x_panel.repo = type(self.x_panel.repo)(self.store)
            self.x_panel.current_run = None
            self.x_panel.repo.recover()
            self.x_panel.recovery.repo = self.x_panel.repo
            self.x_panel.refresh()
            self.job_manager.offset = 0
            self.job_manager.refresh()
            self.statusBar().showMessage("استُعيدت قاعدة البيانات؛ ملفات TXT الخارجية لازمة للاستكمال.")

        self.submit(restore, restored, exclusive=True)

    def _close_after_osintgram(self):
        if self.closing:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QCloseEvent):
        if self.closed:
            event.accept()
            return
        self.snapscore.stop_work()
        if self.preset_dirty and not self._allow_close:
            event.ignore()

            def approved_close():
                self._allow_close = True
                QTimer.singleShot(0, self.close)

            self.guard_settings(approved_close)
            return
        self.instagram.analysis.client.shutdown()
        self.snapscore.shutdown()
        self.file_search.shutdown()
        self.roblox.shutdown()
        self.x_panel.shutdown()
        self.file_generator.shutdown()
        if not self.instagram.analysis.client.busy:
            self.instagram.analysis.manager.shutdown()
        if (
            self.is_running()
            or any(not f.done() for f in self.futures)
            or self.instagram.analysis.client.busy
            or self.instagram.analysis.manager.busy
            or self.file_search.busy
            or self.roblox.busy
            or self.x_panel.busy
            or self.file_generator.busy
        ):
            self.closing = True
            self.stop_engine()
            self.statusBar().showMessage("إيقاف آمن وحفظ التقدم قبل الإغلاق…")
            event.ignore()
            return
        if self.current_job and self.scheduler:
            self.store.update_schedule(self.current_job, self.scheduler.serialize())
        self.timer.stop()
        self.job_manager.shutdown()
        QApplication.instance().removeEventFilter(self.wheel_guard)
        self.executor.shutdown(wait=True)
        self.store.close()
        self.closed = True
        event.accept()


def run(database: Path):
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Segoe UI", 10))
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    from .instance import InstanceLock

    try:
        lock = InstanceLock(database.with_suffix(".instance.lock"))
    except RuntimeError:
        QMessageBox.warning(None, "التطبيق مفتوح", "توجد نافذة أخرى تستخدم قاعدة البيانات نفسها.")
        return 1
    try:
        window = MainWindow(database)
        window.show()
        return app.exec()
    except Exception as exc:
        QMessageBox.critical(
            None, "تعذر بدء التطبيق", "تعذر فتح التطبيق أو قاعدة البيانات: " + type(exc).__name__
        )
        return 1
    finally:
        lock.close()
