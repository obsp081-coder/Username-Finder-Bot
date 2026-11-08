"""Arabic Base Mode interface for the independently installed local backend."""

from __future__ import annotations

import json
import re
from html import escape

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .osintgram_api import (
    BASE_URL,
    ContractError,
    LocalOsintgramClient,
    build_payload,
    error_category,
    error_message,
)
from .osintgram_backend import BackendManager
from .osintgram_catalog import CATALOG, TITLES, explanation
from .osintgram_display import MISSING, dataset_label, field_hint, field_label
from .osintgram_results import (
    csv_compatible,
    datasets,
    display_value,
    export_csv,
    export_json,
    json_preview,
    safe_result,
    table_data,
)
from .osintgram_selection import CommandTree
from .osintgram_setup import AccountSetup
from .ui_helpers import style_combo_popup

PARAM_TITLES = {
    "limit": "حد النتائج",
    "limit_posts": "عدد المنشورات",
    "match_limit": "حد النتائج المطابقة",
    "max_checks": "حد الحسابات المفحوصة",
    "hashtag": "الوسم",
    "place": "الموقع",
    "post": "رابط المنشور أو معرّفه",
    "other_username": "الحساب الآخر",
    "scope": "نطاق المقارنة",
    "sort": "الترتيب",
}

PARAM_HELP = {
    "limit": "عدد النتائج المطلوب؛ قد تقلّله ميزانية طلبات التحليل.",
    "limit_posts": "عدد المنشورات المفحوصة؛ الحقل الفارغ يستخدم افتراضي الخدمة ضمن الميزانية.",
    "match_limit": "نتوقف بعد العثور على هذا العدد من جهات الاتصال العامة إن أمكن.",
    "max_checks": "أقصى عدد حسابات تُفحص، بما فيها نتائج الذاكرة المؤقتة؛ يختلف عن عدد الطلبات.",
    "other_username": "اسم الحساب الثاني للمقارنة، وليس حساب الدخول.",
    "hashtag": "اكتب الهاشتاق المطلوب؛ يمكن إضافة علامة # أو تركها.",
    "place": "اسم المكان المطلوب البحث عن منشوراته.",
    "post": "رابط منشور أو مقطع، أو معرّفه الذي أعاده المصدر.",
    "scope": "اختر مقارنة المتابعين أو الحسابات المتابَعة أو الجانبين.",
    "sort": "اختر المنشورات الأبرز أو الأحدث كما يوفّرها المصدر.",
}


def command_group(name):
    entry = CATALOG.get(name)
    return entry.category if entry else "other"


def label(text):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


class OsintgramPanel(QGroupBox):
    def __init__(self, parent=None, client=None, manager=None):
        super().__init__("تحليل حسابات Instagram — Osintgram", parent)
        self.client = client or LocalOsintgramClient(self)
        self.manager = manager or BackendManager(self)
        if self.manager.parent() is None:
            self.manager.setParent(self)
        self.auth_state = "unverified"
        self.tools = []
        self.values = {}
        self.editors = {}
        self.current_tool = None
        self.running = False
        self.connecting = False
        self.document = None
        self.result_items = []
        self.current_datasets = []
        self.current_data = None
        self.failed_commands = False
        self.stream_error = False
        self.cancel_acknowledged = False
        self.outcome = ""
        self.state = "disconnected"
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout = QVBoxLayout(self)
        self.root_layout = layout
        layout.setSpacing(8)
        connection = QHBoxLayout()
        self.connection_status = label("غير متصل — اضغط «اتصال / إعادة المحاولة»")
        self.connection_status.setObjectName("osintStatus")
        connection.addWidget(self.connection_status, 1)
        self.reconnect = QPushButton("إعادة الاتصال")
        self.reconnect.clicked.connect(self.connect_backend)
        connection.addWidget(self.reconnect)
        layout.addLayout(connection)
        self.endpoint = label(BASE_URL + " — اتصال محلي فقط؛ الاتصال لا يؤكد صلاحية جلسة Instagram.")
        self.endpoint.setObjectName("igHint")
        self.endpoint.hide()
        self.pages = QTabWidget()
        self.setup = AccountSetup(self.manager)
        self.command_page = QWidget()
        self.result_page = QWidget()
        self.pages.addTab(self.setup, "الحساب والاتصال")
        self.pages.addTab(self.command_page, "اختيار المعلومات")
        self.pages.addTab(self.result_page, "النتائج")
        self.pages.setCurrentIndex(1)
        layout.addWidget(self.pages)
        layout = QVBoxLayout(self.command_page)

        target_row = QGridLayout()
        self.target = QLineEdit()
        self.target.setPlaceholderText("username أو @username")
        self.target.setMaxLength(64)
        self.target.setMaximumWidth(480)
        self.target.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.target.setAccessibleName("هدف تحليل Osintgram")
        self.limit = QSpinBox()
        self.limit.setRange(1, 1000)
        self.limit.setValue(10)
        self.limit.setMaximumWidth(100)
        self.limit.setAccessibleName("حد طلبات Osintgram")
        target_row.addWidget(label("الحساب المطلوب تحليله"), 0, 0)
        target_row.addWidget(self.target, 0, 1)
        target_row.addWidget(label("حد طلبات التحليل"), 0, 2)
        target_row.addWidget(self.limit, 0, 3)
        target_row.setColumnStretch(1, 1)
        layout.addLayout(target_row)
        warning = label(
            "حد الطلبات يقلّل استهلاك التحليل. قد تستخدم تهيئة الحساب أو إعادة المحاولة طلبات إضافية. "
            "التحليل لا يثبت إتاحة الأسماء."
        )
        warning.setObjectName("igHint")
        layout.addWidget(warning)

        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("ابحث عن المعلومات التي تريدها…")
        self.search.setAccessibleName("البحث في معلومات التحليل")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(150)
        self.search.textChanged.connect(self.filter_commands)
        search_row.addWidget(self.search, 1)
        self.selection_count = label("لم تُحدّد أوامر بعد")
        search_row.addWidget(self.selection_count)
        self.recommended_button = QPushButton("تحديد الخيارات المقترحة")
        self.recommended_button.setToolTip(
            "يستبدل التحديد بمعلومات الحساب الأساسية فقط إذا كانت مدعومة؛ "
            "لا يبدأ التحليل ولا يغيّر حد الطلبات."
        )
        self.recommended_button.clicked.connect(self.select_recommended)
        search_row.addWidget(self.recommended_button)
        self.clear_selection_button = QPushButton("إلغاء التحديد")
        self.clear_selection_button.clicked.connect(self.clear_selection)
        search_row.addWidget(self.clear_selection_button)
        layout.addLayout(search_row)

        self.config_grid = QGridLayout()
        self.commands = CommandTree()
        self.commands.setHeaderLabels(["المعلومات المطلوبة", "المعرّف"])
        self.commands.hideColumn(1)
        self.commands.setAccessibleName("أوامر Osintgram المكتشفة")
        self.commands.setFixedHeight(300)
        self.commands.setMinimumWidth(0)
        self.commands.setColumnWidth(0, 190)
        self.commands.setWordWrap(True)
        self.commands.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.commands.currentItemChanged.connect(self.edit_command)
        self.commands.itemChanged.connect(self._refresh_actions)
        self.parameter_scroll = QWidget()
        self.parameter_scroll.setMinimumWidth(0)
        self.parameter_content = self.parameter_scroll
        self.parameter_layout = QVBoxLayout(self.parameter_content)
        self.parameter_title = label(
            "انقر اسم الخيار لتحديده. الأسهم تعرض الشرح، ومفتاح المسافة يبدّل علامة التحديد."
        )
        self.parameter_layout.addWidget(self.parameter_title)
        self.form = QFormLayout()
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.parameter_layout.addLayout(self.form)
        self.technical_toggle = QCheckBox("تفاصيل تقنية للأمر والاتصال")
        self.technical = label("")
        self.technical.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.technical.hide()
        self.technical_toggle.toggled.connect(self.technical.setVisible)
        self.parameter_layout.addWidget(self.technical_toggle)
        self.parameter_layout.addWidget(self.technical)
        self.parameter_layout.addStretch()
        self.config_grid.addWidget(self.commands, 0, 0, Qt.AlignmentFlag.AlignTop)
        self.config_grid.addWidget(self.parameter_scroll, 0, 1)
        self.config_grid.setColumnStretch(0, 1)
        self.config_grid.setColumnStretch(1, 1)

        actions = QHBoxLayout()
        self.start = QPushButton("بدء تحليل الحساب")
        self.start.setObjectName("primary")
        self.start.clicked.connect(self.start_analysis)
        self.cancel = QPushButton("إلغاء التحليل")
        self.cancel.clicked.connect(self.cancel_analysis)
        self.json_export = QPushButton("تصدير JSON")
        self.json_export.clicked.connect(self.save_json)
        self.csv_export = QPushButton("تصدير الجدول CSV")
        self.csv_export.setToolTip(
            "للبيانات المسطحة؛ قيم الخلايا بترميز JSON لحفظ null والأرقام والنصوص بأمان."
        )
        self.csv_export.clicked.connect(self.save_csv)
        for button in (self.start, self.cancel):
            actions.addWidget(button)
        results_link = QPushButton("عرض النتائج")
        results_link.clicked.connect(lambda: self.pages.setCurrentIndex(2))
        actions.addWidget(results_link)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addLayout(self.config_grid)
        self.execution_status = label("لم يبدأ أي تحليل. النتائج في الذاكرة فقط ما لم تصدّرها صراحةً.")
        self.root_layout.insertWidget(1, self.execution_status)

        layout = QVBoxLayout(self.result_page)
        exports = QHBoxLayout()
        exports.addWidget(self.json_export)
        exports.addWidget(self.csv_export)
        exports.addStretch()
        layout.addLayout(exports)
        self.profile_summary = label("")
        self.profile_summary.hide()
        layout.addWidget(self.profile_summary)

        self.result_picker = QComboBox()
        style_combo_popup(self.result_picker)
        self.result_picker.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.result_picker.setMinimumContentsLength(12)
        self.result_picker.setAccessibleName("نتائج أوامر Osintgram")
        self.result_picker.currentIndexChanged.connect(self.show_result)
        layout.addWidget(self.result_picker)
        self.result_status = label("لا توجد نتائج تحليل بعد.")
        layout.addWidget(self.result_status)
        self.dataset_picker = QComboBox()
        style_combo_popup(self.dataset_picker)
        self.dataset_picker.setMinimumContentsLength(12)
        self.dataset_picker.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.dataset_picker.currentIndexChanged.connect(self.show_dataset)
        self.dataset_picker.setAccessibleName("جداول نتيجة Osintgram")
        layout.addWidget(self.dataset_picker)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["الحقل", "القيمة"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(220)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAccessibleName("جدول تحليل Osintgram المستقل")
        layout.addWidget(self.table, 1)
        self.json_toggle = QPushButton("عرض / إخفاء معاينة JSON")
        self.json_toggle.setCheckable(True)
        self.json_toggle.setMaximumWidth(300)
        self.json_toggle.toggled.connect(self._toggle_json)
        layout.addWidget(self.json_toggle, 0, Qt.AlignmentFlag.AlignRight)
        self.json_view = QTextEdit()
        self.json_view.setReadOnly(True)
        self.json_view.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.json_view.setFixedHeight(180)
        self.json_view.hide()
        layout.addWidget(self.json_view)
        layout.addWidget(
            label("العرض محدود إلى 300 صف و32 عمودًا؛ JSON المعاين مختصر. التصدير يحفظ البيانات المحتفظ بها.")
        )

        self.client.tools_ready.connect(self._discovered)
        self.client.connection_error.connect(self._connection_failed)
        self.client.event_received.connect(self._event)
        self.client.finished.connect(self._finished)
        self.client.cancellation.connect(self._cancel_status)
        self.client.idle.connect(self._refresh_actions)
        self.manager.changed.connect(self._backend_changed)
        self.manager.ready.connect(self._backend_ready)
        self.manager.idle.connect(self._refresh_actions)
        self.manager.auth_event.connect(self._auth_event)
        self.setup.updated.connect(self._refresh_actions)
        self._columns = 2
        self._refresh_actions()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "_columns"):
            return
        columns = 2 if self.width() >= 700 else 1
        if columns != self._columns:
            self._columns = columns
            self.config_grid.removeWidget(self.parameter_scroll)
            self.config_grid.addWidget(
                self.parameter_scroll, 0 if columns == 2 else 1, 1 if columns == 2 else 0
            )
            self.config_grid.setColumnStretch(1, 1 if columns == 2 else 0)

    def _set_state(self, state, text):
        self.state = state
        self.connection_status.setText(text)
        background, foreground = {
            "connected": ("#e9f5ee", "#25583c"),
            "connecting": ("#edf3fc", "#254c7e"),
            "running": ("#edf3fc", "#254c7e"),
            "error": ("#fff3dd", "#79551d"),
            "disconnected": ("#f1f3f6", "#4e5d71"),
        }[state]
        self.connection_status.setStyleSheet(
            f"background: {background}; color: {foreground}; border-radius: 5px; padding: 6px;"
        )

    def _refresh_actions(self, *_):
        available = (
            not self.running
            and not self.connecting
            and not self.client.closing
            and not self.manager.busy
            and not getattr(self, "id_resolution_busy", False)
        )
        self.reconnect.setEnabled(available and not self.client.busy)
        self.start.setEnabled(
            available
            and self.client.connected
            and not self.client.busy
            and bool(self.selected())
            and self._session_usable()
        )
        cancel_pending = any(context["kind"] == "cancel" for context in self.client.requests.values())
        self.cancel.setEnabled(self.running and not self.cancel_acknowledged and not cancel_pending)
        self.commands.setEnabled(available)
        self.search.setEnabled(available)
        self.clear_selection_button.setEnabled(available)
        self.recommended_button.setEnabled(
            available and any(tool["name"] == "get_user_info" for tool in self.tools)
        )
        self.parameter_scroll.setEnabled(available)
        self.target.setEnabled(available)
        self.limit.setEnabled(available)
        self.setup.analysis_running = self.running or getattr(self, "id_resolution_busy", False)
        self.setup.refresh()
        self.selection_count.setText(f"المحدّد: {len(self.selected())}")
        exported = available and bool(self.document and self.document.get("results"))
        self.json_export.setEnabled(exported)
        self.csv_export.setEnabled(exported and csv_compatible(self.current_data))

    def connect_backend(self):
        if (
            self.client.busy
            or self.running
            or self.client.closing
            or getattr(self, "id_resolution_busy", False)
        ):
            return
        self.connecting = True
        self.auth_state = "unverified"
        self._set_state("connecting", "جارٍ الاتصال بالخدمة المحلية…")
        self.manager.state = "unconfigured"
        self.manager.ensure()
        self._refresh_actions()

    def activate(self):
        self.manager.ensure()

    def _session_usable(self):
        if self.auth_state in ("authentication_failed", "challenge_required"):
            return False
        if self.manager.backend != "instagrapi" or not self.manager.owned:
            return True  # Independent service manages its own account; never assume its session is valid.
        return bool(
            self.manager.session_exists
            and self.manager.settings.value("login_username", "")
            == self.manager.settings.value("session_account", "")
            and self.manager.settings.value("login_username", "")
        )

    def _backend_changed(self, state, message):
        self.connection_status.setToolTip(
            "\n".join(filter(None, (self.manager.failure_code, self.manager.probe_detail)))
            if state == "error"
            else ""
        )
        self.connecting = state in ("checking", "validating", "starting")
        if state != "ready":
            self.client.connected = False
        self._set_state(
            "connecting" if self.connecting else "connected" if state == "ready" else "error", message
        )
        if state in ("credentials_missing", "unconfigured", "error"):
            self.pages.setCurrentIndex(0)
        self._refresh_actions()

    def _backend_ready(self, tools):
        self.client.connected = True
        self._discovered(tools)
        if not self._session_usable():
            self.setup.status.setText(
                "المحرك متصل. احفظ حساب الدخول ثم تحقّق من الجلسة عبر تسجيل الدخول قبل التحليل."
            )
            self.pages.setCurrentIndex(0)
        else:
            self.pages.setCurrentIndex(1)
            if not self.manager.owned:
                self.connection_status.setText(
                    "متصل بخدمة Osintgram مستقلة؛ يدير المحرك حساب الدخول، والجلسة لم تُختبر بعد."
                )

    def _auth_event(self, kind):
        if kind in ("session_saved", "authentication_failed", "challenge_required", "authenticating"):
            self.auth_state = kind
        self._refresh_actions()

    def filter_commands(self, query):
        query = query.strip().casefold()
        for index in range(self.commands.topLevelItemCount()):
            group = self.commands.topLevelItem(index)
            visible = False
            for row in range(group.childCount()):
                item = group.child(row)
                name = item.data(0, Qt.ItemDataRole.UserRole)
                entry = CATALOG.get(name)
                match = query in (item.text(0) + name + (entry.purpose if entry else "")).casefold()
                item.setHidden(not match)
                visible |= match
            group.setHidden(not visible)

    def clear_selection(self):
        for index in range(self.commands.topLevelItemCount()):
            group = self.commands.topLevelItem(index)
            for row in range(group.childCount()):
                group.child(row).setCheckState(0, Qt.CheckState.Unchecked)

    def select_recommended(self):
        """A deliberately small starter set; selecting never executes a request."""
        if not self.recommended_button.isEnabled():
            return
        self.commands.blockSignals(True)
        try:
            self.clear_selection()
            for index in range(self.commands.topLevelItemCount()):
                group = self.commands.topLevelItem(index)
                for row in range(group.childCount()):
                    item = group.child(row)
                    if (
                        item.data(0, Qt.ItemDataRole.UserRole) == "get_user_info"
                        and item.flags() & Qt.ItemFlag.ItemIsUserCheckable
                    ):
                        item.setCheckState(0, Qt.CheckState.Checked)
        finally:
            self.commands.blockSignals(False)
        self._refresh_actions()

    def _discovered(self, tools):
        self.connecting = False
        self._save_editors()
        selected = set(self.selected())
        current = self.current_tool
        self.tools = tools
        self.commands.clear()
        self.current_tool = None
        groups = {}
        for tool in tools:
            previous = self.values.get(tool["name"], {})
            self.values[tool["name"]] = {
                param["name"]: previous.get(param["name"], self._default_text(param))
                for param in tool["params"]
            }
            group = command_group(tool["name"])
            if group not in groups:
                groups[group] = QTreeWidgetItem(
                    self.commands, ["أوامر إضافية" if group == "other" else group]
                )
                groups[group].setFlags(Qt.ItemFlag.ItemIsEnabled)
            item = QTreeWidgetItem(groups[group], [TITLES.get(tool["name"], tool["name"]), tool["name"]])
            item.setSizeHint(0, QSize(0, 46))
            item.setData(0, Qt.ItemDataRole.UserRole, tool["name"])
            item.setCheckState(
                0, Qt.CheckState.Checked if tool["name"] in selected else Qt.CheckState.Unchecked
            )
            entry = CATALOG.get(tool["name"])
            if entry and entry.hiker_only and self.manager.backend == "instagrapi":
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
                item.setText(0, entry.title + " — يتطلب HikerAPI")
            item.setToolTip(0, entry.purpose if entry else escape(str(tool.get("description", ""))[:4000]))
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignLeft)
            if tool["name"] == current:
                self.commands.setCurrentItem(item)
        self.commands.expandAll()
        self._set_state("connected", f"متصل — {len(tools)} أمرًا؛ لم تُختبر جلسة Instagram.")
        self._refresh_actions()

    def _connection_failed(self, message):
        self.connecting = False
        self._set_state("disconnected" if not self.tools else "error", self._friendly(message))
        self._refresh_actions()

    def selected(self):
        names = []
        for index in range(self.commands.topLevelItemCount()):
            group = self.commands.topLevelItem(index)
            for row in range(group.childCount()):
                item = group.child(row)
                if item.checkState(0) == Qt.CheckState.Checked:
                    names.append(item.data(0, Qt.ItemDataRole.UserRole))
        return names

    def _save_editors(self):
        if self.current_tool:
            self.values[self.current_tool] = {
                name: json.dumps(editor.isChecked())
                if isinstance(editor, QCheckBox)
                else str(editor.currentData())
                if isinstance(editor, QComboBox) and editor.currentData() is not None
                else editor.currentText()
                if isinstance(editor, QComboBox)
                else editor.text()
                for name, editor in self.editors.items()
            }

    @staticmethod
    def _default_text(param):
        value = param.get("default")
        if value is None:
            return ""
        return value if isinstance(value, str) else json.dumps(value)

    def edit_command(self, item, previous=None):
        self._save_editors()
        self.current_tool = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        self.editors = {}
        while self.form.rowCount():
            self.form.removeRow(0)
        spec = next((tool for tool in self.tools if tool["name"] == self.current_tool), None)
        if spec is None:
            self.parameter_title.setText("اختر أمرًا لعرض معاملاته.")
            return
        self.parameter_title.setText(
            TITLES.get(spec["name"], spec["name"]) + "\n\n" + explanation(spec, self.manager.backend)
        )
        self.technical.setText(
            BASE_URL + "\n" + spec["name"] + "\n" + str(spec.get("description", ""))[:4000]
        )
        for param in spec["params"]:
            text = self.values.get(spec["name"], {}).get(param["name"], self._default_text(param))
            if "choices" in param:
                editor = QComboBox()
                style_combo_popup(editor)
                names = {
                    "top": "الأبرز",
                    "recent": "الأحدث",
                    "both": "الجانبان",
                    "followers": "المتابعون",
                    "followings": "الحسابات المتابَعة",
                }
                for choice in param["choices"]:
                    editor.addItem(names.get(str(choice), str(choice)), choice)
                editor.setCurrentIndex(max(0, editor.findData(text)))
            elif param["type"] == "boolean":
                editor = QCheckBox("تفعيل الخيار")
                editor.setChecked(text.lower() == "true")
            else:
                editor = QLineEdit(text)
                editor.setMaxLength(8192)
                editor.setMinimumWidth(0)
                if param["type"] == "integer":
                    editor.setValidator(QIntValidator(1, 100000, editor))
                    editor.setPlaceholderText("تلقائي ضمن الميزانية")
                if param["type"] in ("array", "object"):
                    editor.setPlaceholderText("JSON")
            editor.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            editor.setMaximumWidth(140 if param["type"] in ("integer", "number") else 410)
            editor.setAccessibleName(PARAM_TITLES.get(param["name"], param["name"]))
            editor.setToolTip(PARAM_TITLES.get(param["name"], param["name"]))
            self.editors[param["name"]] = editor
            self.form.addRow(label(PARAM_TITLES.get(param["name"], param["name"])), editor)
            if param["name"] in PARAM_HELP:
                hint = label(PARAM_HELP[param["name"]])
                hint.setObjectName("igHint")
                self.form.addRow(hint)
        if not spec["params"]:
            self.form.addRow(label("لا توجد معاملات إضافية لهذا الأمر."))

    def start_analysis(self):
        if (
            self.running
            or self.client.busy
            or not self.client.connected
            or getattr(self, "id_resolution_busy", False)
        ):
            return
        self._save_editors()
        if not self._session_usable():
            self.execution_status.setText("تحقق من جلسة حساب الدخول من صفحة «الحساب والاتصال» أولًا.")
            return
        if self.manager.backend == "instagrapi" and any(
            CATALOG.get(name) and CATALOG[name].hiker_only for name in self.selected()
        ):
            self.execution_status.setText("بعض الأوامر المحددة تتطلب HikerAPI ولا يدعمها المحرك الحالي.")
            return
        try:
            payload = build_payload(
                self.tools,
                {name: self.values.get(name, {}) for name in self.selected()},
                self.target.text(),
                self.limit.value(),
            )
            self.client.run(payload)
        except (ContractError, ValueError) as exc:
            self.execution_status.setText(str(exc))
            return
        self.running = True
        self.outcome = ""
        self.failed_commands = self.stream_error = self.cancel_acknowledged = False
        self.document = {"request": payload, "status": "running", "results": [], "events": []}
        self.result_items = []
        self.result_picker.clear()
        self.dataset_picker.clear()
        self.table.setRowCount(0)
        self.current_data = None
        self.json_view.clear()
        self.result_status.setText("بانتظار أول نتيجة؛ لا توجد بيانات مصطنعة.")
        self.execution_status.setText("جارٍ تجهيز الحساب وتنفيذ الأوامر…")
        self._set_state("running", "التحليل جارٍ — لا تُعد الاتصال ولا تبدأ تشغيلًا آخر.")
        self._refresh_actions()

    def cancel_analysis(self):
        if self.running:
            self.client.cancel()
            self.execution_status.setText("طُلب الإلغاء؛ بانتظار رد الخدمة وانتهاء الاستجابة.")
            self._refresh_actions()

    def _cancel_status(self, acknowledged, message):
        self.cancel_acknowledged |= acknowledged
        if self.document:
            self.document["cancellation_acknowledged"] = self.cancel_acknowledged
        if self.running:
            self.execution_status.setText(
                "سُجّل طلب الإلغاء لدى المحرك؛ قد يكتمل الطلب الجاري أولًا." if acknowledged else message
            )
        self._refresh_actions()

    def _event(self, event):
        if not self.document:
            return
        detail = (
            event.get("detail", "")
            if event.get("type") == "error"
            else (event.get("result", {}).get("error", "") if isinstance(event.get("result"), dict) else "")
        )
        if detail:
            self._report_auth(error_category(detail))
        event = safe_result(event)
        kind = event["type"]
        # Bound diagnostics separately; tool results remain in their own result records.
        if kind != "tool_result" and len(self.document["events"]) < 2000:
            self.document["events"].append(event)
        if kind == "start":
            self.document["backend"] = event.get("backend")
            backend_name = str(event.get("backend", "")).casefold()
            if backend_name in ("instagrapi", "hikerapi"):
                self.manager.backend = backend_name
            self.document["target_id"] = event.get("target_id")
            self.execution_status.setText(f"بدأ التحليل؛ طلبات المحرك المسجلة: {event['api_calls_total']}.")
        elif kind == "api_call":
            total = event.get("total")
            self.execution_status.setText(
                f"التحليل جارٍ؛ طلبات المحرك المسجلة: {total if type(total) is int else 'غير معلن'}."
            )
        elif kind == "tool_call":
            self.execution_status.setText(
                f"جارٍ تنفيذ: {TITLES.get(event['name'], event['name'])} — "
                f"اكتمل {len(self.result_items)} من {len(self.document['request']['calls'])}"
            )
        elif kind == "tool_result":
            event["is_error"] |= isinstance(event["result"], dict) and "error" in event["result"]
            self.failed_commands |= event["is_error"]
            self.document["results"].append(event)
            self.result_items.append(event)
            status = "فشل" if event["is_error"] else "نتيجة"
            self.result_picker.addItem(f"{TITLES.get(event['name'], event['name'])} — {status}")
            self.result_picker.setCurrentIndex(len(self.result_items) - 1)
            self.show_result(len(self.result_items) - 1)
        elif kind == "error":
            self.stream_error = True
            self.execution_status.setText(str(event.get("detail", error_message())))
            self.document["error"] = self.execution_status.text()

    def _finished(self, outcome, message):
        self.running = False
        message = self._friendly(message)
        self._report_auth(self.client.last_error_kind)
        expected = len(self.document["request"]["calls"]) if self.document else 0
        received = len(self.result_items)
        if outcome == "complete" and (self.stream_error or received != expected):
            outcome = "failed"
            message = "انتهى الاتصال دون جميع النتائج المطلوبة؛ المتاح نتائج جزئية فقط."
        elif outcome == "complete" and self.failed_commands:
            all_failed = all(item["is_error"] for item in self.result_items)
            outcome = "failed" if all_failed else "partial"
            message = (
                "فشلت جميع الأوامر؛ راجع رسائل الأخطاء."
                if all_failed
                else "انتهى التنفيذ مع أخطاء في بعض الأوامر؛ راجع نتيجة كل أمر."
            )
        if outcome == "cancelled":
            message = "انتهت الاستجابة بعد طلب الإلغاء؛ النتائج المعروضة جزئية محتملة."
            if not self.cancel_acknowledged:
                message += " لم تؤكد الخدمة تسجيل الإلغاء."
        if not message:
            message = f"اكتمل التنفيذ — {received} نتيجة؛ لا علاقة لها بإتاحة الأسماء."
        if self.stream_error and self.document:
            message = self.document.get("error", error_message()) + " النتائج المتاحة جزئية."
        self.outcome = outcome
        if outcome == "complete":
            self.auth_state = "analysis_succeeded"
            self.setup.status.setText(
                "أعاد المحرك نتائج دون خطأ. قد تكون بعض البيانات مؤقتة، وصلاحية الوصول قد تختلف بين الأوامر."
            )
        if self.document:
            self.document["status"] = outcome
            self.document["summary"] = message
        self.execution_status.setText(message)
        if outcome == "failed" and not received:
            self.result_status.setText(message)
        self._set_state(
            "error"
            if outcome in ("failed", "partial")
            else "connected"
            if self.client.connected
            else "disconnected",
            "خطأ في التحليل — " + message
            if outcome in ("failed", "partial")
            else "متصل — انتهت عملية التحليل.",
        )
        self._refresh_actions()
        self.pages.setCurrentIndex(2)
        self._update_capabilities()

    def _update_capabilities(self):
        for index in range(self.commands.topLevelItemCount()):
            group = self.commands.topLevelItem(index)
            for row in range(group.childCount()):
                item = group.child(row)
                name = item.data(0, Qt.ItemDataRole.UserRole)
                entry = CATALOG.get(name)
                if not entry or not entry.hiker_only:
                    continue
                if self.manager.backend == "instagrapi":
                    item.setCheckState(0, Qt.CheckState.Unchecked)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    item.setText(0, entry.title + " — يتطلب HikerAPI")
                else:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setText(0, entry.title)
        self.edit_command(self.commands.currentItem())

    @staticmethod
    def _friendly(message):
        return re.sub(r"^HTTP [0-9]+ — ", "", message)

    def _report_auth(self, category):
        if category in ("authentication", "challenge"):
            self.auth_state = "challenge_required" if category == "challenge" else "authentication_failed"
            self.setup.auth_status(self.auth_state)

    def show_result(self, index):
        self.current_datasets = []
        self.current_data = None
        self.dataset_picker.clear()
        self.table.setRowCount(0)
        self.profile_summary.hide()
        if not 0 <= index < len(self.result_items):
            self._refresh_actions()
            return
        event = self.result_items[index]
        value = event["result"]
        is_profile = event["name"] == "get_user_info" and isinstance(value, dict) and not event["is_error"]
        self.profile_summary.setVisible(is_profile)
        if is_profile:
            fields = (
                "username",
                "full_name",
                "id",
                "follower_count",
                "following_count",
                "media_count",
                "is_verified",
                "is_private",
            )
            pairs = [
                field_label(key) + ": " + display_value(value[key], key) for key in fields if key in value
            ]
            if "id" not in value and "pk" in value:
                pairs.insert(2, field_label("pk") + ": " + display_value(value["pk"], "pk"))
            biography = display_value(value.get("biography", MISSING), "biography")
            self.profile_summary.setText(
                "\n".join("   |   ".join(pairs[i : i + 2]) for i in range(0, len(pairs), 2))
                + "\nالنبذة: "
                + biography[:240]
                + ("…" if len(biography) > 240 else "")
            )
        self.result_status.setText(
            display_value(event["result"].get("error", error_message()))
            if event["is_error"] and isinstance(event["result"], dict)
            else "فشل الأمر؛ راجع JSON."
            if event["is_error"]
            else "لا توجد نتائج لهذا الخيار."
            if event["result"] == []
            else "لا توجد تفاصيل لهذا الخيار."
            if event["result"] == {}
            else "بيانات أعادها المحرك؛ الحقول الغائبة غير مفترضة."
        )
        self.current_datasets = datasets(event["result"])
        self.dataset_picker.addItems([dataset_label(name) for name, value in self.current_datasets])
        self.dataset_picker.setVisible(len(self.current_datasets) > 1)
        self.show_dataset(0)
        self.json_view.setPlainText(json_preview(event))

    def show_dataset(self, index):
        if not 0 <= index < len(self.current_datasets):
            return
        self.current_data = self.current_datasets[index][1]
        headers, rows = table_data(self.current_data)
        result_index = self.result_picker.currentIndex()
        command = (
            self.result_items[result_index]["name"] if 0 <= result_index < len(self.result_items) else ""
        )
        if "previews" in self.current_datasets[index][0].split("."):
            command = "post_preview"
        is_fields = isinstance(self.current_data, dict)
        self.table.setUpdatesEnabled(False)
        try:
            self.table.clearContents()
            self.table.setColumnCount(len(headers))
            self.table.setHorizontalHeaderLabels([field_label(header, command) for header in headers])
            for column, header in enumerate(headers):
                self.table.horizontalHeaderItem(column).setToolTip(field_hint(header))
            self.table.setRowCount(len(rows))
            for row, cells in enumerate(rows):
                for column, value in enumerate(cells):
                    key = str(cells[0]) if is_fields else headers[column]
                    text = (
                        field_label(key, command) if is_fields and column == 0 else display_value(value, key)
                    )
                    ltr = bool(re.search(r"[A-Za-z0-9]", text)) and not bool(
                        re.search(r"[\u0600-\u06ff]", text)
                    )
                    if ltr:
                        text = "\u2066" + text + "\u2069"
                    item = QTableWidgetItem(text)
                    item.setToolTip(field_hint(key) + "\n" + display_value(value, key))
                    item.setTextAlignment(
                        (Qt.AlignmentFlag.AlignLeft if ltr else Qt.AlignmentFlag.AlignRight)
                        | Qt.AlignmentFlag.AlignVCenter
                        | Qt.AlignmentFlag.AlignAbsolute
                    )
                    self.table.setItem(row, column, item)
            for column in range(len(headers)):
                self.table.resizeColumnToContents(column)
                self.table.setColumnWidth(column, min(360, max(170, self.table.columnWidth(column))))
        finally:
            self.table.setUpdatesEnabled(True)
        self._refresh_actions()

    def _toggle_json(self, checked):
        self.json_view.setVisible(checked)

    def save_json(self):
        if not self.json_export.isEnabled():
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "تصدير تحليل Osintgram", "osintgram-results.json", "JSON (*.json)"
        )
        if filename:
            try:
                export_json(filename, self.document)
                self.execution_status.setText("صُدّر JSON صراحةً؛ لم تُكتب النتائج في قاعدة بيانات التطبيق.")
            except (OSError, ValueError):
                self.execution_status.setText("تعذر حفظ JSON؛ تحقق من المسار وصلاحية الكتابة.")

    def save_csv(self):
        if not self.csv_export.isEnabled():
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "تصدير جدول Osintgram", "osintgram-table.csv", "CSV (*.csv)"
        )
        if filename:
            try:
                export_csv(filename, self.current_data)
                self.execution_status.setText("صُدّر الجدول كاملًا؛ قيم CSV بترميز JSON لحفظ أنواع البيانات.")
            except (OSError, ValueError):
                self.execution_status.setText("تعذر حفظ CSV؛ تحقق من المسار وتوافق الجدول.")
