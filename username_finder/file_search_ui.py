"""Independent Arabic local search UI; workers never access widgets or the database."""

from __future__ import annotations

import math
import queue
import re
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from threading import Event

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .file_search import (
    ERRORS,
    MAX_COPY_BYTES,
    MAX_QUERY_BYTES,
    PAGE_BYTES,
    WHOLE_VIEW_BYTES,
    FileInfo,
    SearchFailure,
    inspect_line,
    read_page,
    search_files,
    validate_search,
)
from .file_search_ids import search_account_ids
from .instagram_id import InstagramIdResolver
from .instagram_ui import normalize_username, syntax_error
from .platform_workspace import PlatformWorkspace
from .ui_cards import Card, Disclosure, PageHeading, caption, icon
from .ui_helpers import style_combo_popup


def size_text(size):
    return f"{size / 1024**3:,.2f} GiB" if size >= 1024**3 else f"{size / 1024**2:,.2f} MiB"


def text_label(text):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class MatchModel(QAbstractTableModel):
    HEADERS = ("الملف", "السطر", "موضع السطر (بايت)", "موضع الملف (بايت)", "النص المطابق")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.query = ""

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def columnCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section] if orientation == Qt.Orientation.Horizontal else section + 1
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        match = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                Path(match.file.path).name,
                match.line,
                match.byte_column,
                match.offset,
                self.query[:120] + ("…" if len(self.query) > 120 else ""),
            )[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return match.file.path if index.column() == 0 else str(self.data(index))
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(
                (
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignAbsolute
                    if index.column() in (0, 4)
                    else Qt.AlignmentFlag.AlignHCenter
                )
                | Qt.AlignmentFlag.AlignVCenter
            )
        return None

    def reset(self, query):
        self.beginResetModel()
        self.rows = []
        self.query = query
        self.endResetModel()

    def append(self, rows):
        if rows:
            self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows) + len(rows) - 1)
            self.rows.extend(rows)
            self.endInsertRows()


class SearchTask:
    def __init__(self, paths, query, limit, *, structured=False):
        self.paths, self.query, self.limit = paths, query, limit
        self.structured = structured
        self.cancel = Event()
        self.batches = queue.Queue(maxsize=8)
        self.latest = None

    def deliver(self, batch):
        while not self.cancel.is_set():
            try:
                self.batches.put(batch, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def progress(self, value):
        self.latest = value

    def run(self):
        scanner = search_account_ids if self.structured else search_files
        return scanner(
            self.paths,
            self.query,
            self.limit,
            cancel=self.cancel,
            on_batch=self.deliver,
            on_progress=self.progress,
        )


class ViewBridge(QObject):
    loaded = Signal(int, str, object)


class SourceViewer(QPlainTextEdit):
    """Copy original characters even where QTextDocument normalizes separators."""

    source = ""

    def set_source(self, text):
        self.source = text
        self.setPlainText(text)

    def copy(self):
        cursor = self.textCursor()
        if cursor.hasSelection():
            raw = self.source.encode("utf-16-le")
            selected = raw[cursor.selectionStart() * 2 : cursor.selectionEnd() * 2].decode("utf-16-le")
            QApplication.clipboard().setText(selected)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
            event.accept()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        action = menu.addAction("نسخ التحديد", self.copy)
        action.setEnabled(self.textCursor().hasSelection())
        menu.addAction("تحديد الكل", self.selectAll)
        menu.exec(event.globalPos())
        menu.deleteLater()


class FileSearchPanel(QScrollArea):
    idle = Signal()

    def __init__(self, parent=None, *, analysis=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="file-search")
        self.files = []
        self.task = self.future = None
        self.view_future = None
        self.view_futures = set()
        self.view_cancel = Event()
        self.view_token = 0
        self.info = self.page = None
        self.closing = False
        self.executor_closed = False
        self.last_summary = None
        self.resolver = InstagramIdResolver(analysis, self) if analysis is not None else None
        self.pending_resolution = None
        self.resolved_id = ""
        if self.resolver:
            self.resolver.resolved.connect(self._resolved)
            self.resolver.failed.connect(self._resolution_failed)
            self.resolver.status.connect(lambda message: self.resolution.setText(message))
            self.resolver.idle.connect(self._resolution_idle)
        self.bridge = ViewBridge(self)
        self.bridge.loaded.connect(self._view_done)
        self.poll = QTimer(self)
        self.poll.setInterval(40)
        self.poll.timeout.connect(self._poll)

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        layout.addWidget(
            PageHeading("البحث داخل الملفات", "بحث محلي متدفق • دون فهارس أو نسخ كاملة من الملفات", "search")
        )
        root_layout = layout
        self.workspace = PlatformWorkspace()
        root_layout.addWidget(self.workspace, 1)
        config_layout = self.workspace.add_page("الملفات وإعداد البحث")
        results_layout = self.workspace.add_page("النتائج والمصدر", expand=True)
        source_card = Card("الملفات المحلية", "folder")
        config_layout.addWidget(source_card)
        layout = source_card.body
        self.mode = QComboBox()
        self.mode.addItems(["البحث النصي العادي", "تحويل يوزر Instagram إلى ID ثم البحث"])
        self.mode.setAccessibleName("نمط البحث داخل الملفات")
        style_combo_popup(self.mode)
        layout.addWidget(self.mode)
        files_row = QHBoxLayout()
        self.choose = QPushButton("اختيار ملف")
        self.choose.setIcon(icon("folder", size=18))
        self.add = QPushButton("إضافة ملفات")
        self.remove = QPushButton("إزالة ملف")
        self.clear = QPushButton("مسح القائمة")
        self.choose.clicked.connect(lambda: self.pick_files(False))
        self.add.clicked.connect(lambda: self.pick_files(True))
        self.remove.clicked.connect(self.remove_file)
        self.clear.clicked.connect(self.clear_files)
        for button in (self.choose, self.add, self.remove, self.clear):
            files_row.addWidget(button)
        files_row.addStretch()
        layout.addLayout(files_row)
        self.file_list = QListWidget()
        self.file_list.setFixedHeight(70)
        self.file_list.setAccessibleName("ملفات البحث المحلية وأحجامها")
        self.file_details = Disclosure("الملفات المحددة · 0")
        layout.addWidget(self.file_details)
        self.file_details.body.addWidget(self.file_list)
        query_card = Card("إعداد البحث", "filter")
        config_layout.addWidget(query_card)
        layout = query_card.body
        self.query_controls = QWidget()
        query_row = QHBoxLayout(self.query_controls)
        query_row.setContentsMargins(0, 0, 0, 0)
        query_row.addWidget(text_label("النص المطلوب البحث عنه"))
        self.query = QLineEdit()
        self.query.setMaxLength(MAX_QUERY_BYTES + 1)
        self.query.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.query.setAccessibleName("النص المطلوب البحث عنه")
        self.query.setPlaceholderText("نص حرفي — مطابقة حالة الأحرف")
        query_row.addWidget(self.query, 1)
        layout.addWidget(self.query_controls)
        self.username_controls = QWidget()
        username_row = QHBoxLayout(self.username_controls)
        username_row.setContentsMargins(0, 0, 0, 0)
        username_row.addWidget(text_label("اسم مستخدم Instagram"))
        self.username = QLineEdit()
        self.username.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.username.setPlaceholderText("@username")
        self.username.setAccessibleName("اسم مستخدم Instagram للتحويل إلى ID")
        username_row.addWidget(self.username, 1)
        layout.addWidget(self.username_controls)
        self.resolution = text_label("")
        layout.addWidget(self.resolution)
        actions = QHBoxLayout()
        actions.addWidget(text_label("الحد الأقصى للنتائج"))
        self.limit = QComboBox()
        self.limit.setEditable(True)
        self.limit.addItems(["10", "100", "1,000"])
        self.limit.setCurrentText("100")
        self.limit.setFixedWidth(145)
        self.limit.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.limit.setAccessibleName("الحد الأقصى للنتائج")
        self.limit.setToolTip("عدد مرات التطابق، بما فيها المتداخلة؛ الحد الآمن للواجهة 100000.")
        style_combo_popup(self.limit)
        actions.addWidget(self.limit)
        self.start = QPushButton("بدء البحث")
        self.start.setObjectName("primary")
        self.start.clicked.connect(self.start_search)
        self.cancel = QPushButton("إلغاء البحث")
        self.cancel.setObjectName("danger")
        self.cancel.clicked.connect(self.cancel_work)
        actions.addWidget(self.start)
        actions.addWidget(self.cancel)
        actions.addStretch()
        root_layout.addLayout(actions)
        self.search_hint = text_label("")
        layout.addWidget(self.search_hint)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        root_layout.addWidget(self.progress_bar)
        self.status = text_label("اختر ملفات نصية وأدخل النص. لا تُنشأ فهارس أو نسخ من محتواها.")
        self.status.setObjectName("statusPill")
        root_layout.addWidget(self.status)
        result_card = Card("التطابقات ومعاينة المصدر", "file")
        results_layout.addWidget(result_card, 1)
        layout = result_card.body
        layout.setAlignment(Qt.AlignmentFlag(0))
        self.splitter = splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setHandleWidth(9)
        splitter.setChildrenCollapsible(False)
        splitter.setMinimumHeight(400)
        splitter.setStyleSheet("QSplitter::handle { background: #b9cce2; border-radius: 3px; }")
        splitter.setToolTip("اسحب الفاصل لتغيير ارتفاع النتائج ومعاينة السطر.")
        self.model = MatchModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(120)
        self.table.setAccessibleName("نتائج البحث المحلي")
        for column, width in enumerate((300, 70, 155, 155, 145)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.selectionModel().currentRowChanged.connect(self.open_result)
        splitter.addWidget(self.table)
        self.results_empty = caption("لا توجد نتائج معروضة. اختر ملفات وأدخل نص البحث للبدء.", "emptyTitle")
        self.results_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.results_empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        empty_layout = QVBoxLayout(self.table.viewport())
        empty_layout.addStretch()
        empty_layout.addWidget(self.results_empty)
        empty_layout.addStretch()
        self.model.modelReset.connect(lambda: self.results_empty.setVisible(not self.model.rowCount()))
        self.model.rowsInserted.connect(lambda: self.results_empty.hide())
        viewer_body = QWidget()
        viewer_layout = QVBoxLayout(viewer_body)
        viewer_layout.setContentsMargins(0, 4, 0, 0)
        self.line_status = text_label("اختر نتيجة لقراءة السطر الأصلي كاملًا. لا تُعرض ملفاتك في السجل.")
        viewer_layout.addWidget(self.line_status)
        self.viewer = SourceViewer()
        self.viewer.setReadOnly(True)
        self.viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.viewer.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.viewer.setMinimumHeight(90)
        self.viewer.setAccessibleName("السطر الأصلي — نص قابل للتحديد والنسخ")
        viewer_layout.addWidget(self.viewer, 1)
        pages = QHBoxLayout()
        self.previous = QPushButton("الصفحة السابقة")
        self.next = QPushButton("الصفحة التالية")
        self.previous.clicked.connect(lambda: self.load_page(max(0, self.page.requested - PAGE_BYTES)))
        self.next.clicked.connect(lambda: self.load_page(self.page.requested + PAGE_BYTES))
        self.page_number = QSpinBox()
        self.page_number.setRange(1, 1)
        self.page_number.setAccessibleName("رقم صفحة السطر")
        self.go = QPushButton("انتقال للصفحة")
        self.go.clicked.connect(lambda: self.load_page((self.page_number.value() - 1) * PAGE_BYTES))
        for widget in (self.previous, self.next, self.page_number, self.go):
            pages.addWidget(widget)
        pages.addStretch()
        self.page_controls = QWidget()
        self.page_controls.setLayout(pages)
        self.page_controls.hide()
        viewer_layout.addWidget(self.page_controls)
        copy_row = QHBoxLayout()
        self.copy_selected = QPushButton("نسخ التحديد")
        self.copy_selected.clicked.connect(self.viewer.copy)
        self.copy_line = QPushButton("نسخ السطر كاملًا")
        self.copy_line.clicked.connect(self.copy_whole_line)
        self.copy_location = QPushButton("نسخ المسار ورقم السطر")
        self.copy_location.clicked.connect(self.copy_source_location)
        for button in (self.copy_selected, self.copy_line, self.copy_location):
            copy_row.addWidget(button)
        copy_row.addStretch()
        viewer_layout.addLayout(copy_row)
        splitter.addWidget(viewer_body)
        splitter.setSizes([220, 300])
        layout.addWidget(splitter, 1)
        self.setWidget(body)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.username.textChanged.connect(self._invalidate_resolution)
        self._mode_changed()
        self._actions()

    def ensureWidgetVisible(self, child, xmargin=50, ymargin=50):
        for index in range(self.workspace.count()):
            area = self.workspace.widget(index)
            if area.widget().isAncestorOf(child):
                self.workspace.setCurrentIndex(index)
                area.ensureWidgetVisible(child, xmargin, ymargin)
                return
        super().ensureWidgetVisible(child, xmargin, ymargin)

    @property
    def busy(self):
        return (
            self.task is not None
            or bool(self.resolver and self.resolver.busy)
            or any(not future.done() for future in self.view_futures)
        )

    def _mode_changed(self, *_):
        self._invalidate_resolution()
        structured = self.mode.currentIndex() == 1
        self.query_controls.setVisible(not structured)
        self.username_controls.setVisible(structured)
        self.resolution.setVisible(structured)
        self.start.setText("تحويل إلى ID والبحث" if structured else "بدء البحث")
        self.search_hint.setText(
            "مطابقة حقل id العلوي كاملًا، كسلسلة أرقام أو عدد صحيح، في كائن JSON مستقل لكل سطر (UTF-8). "
            "النص الحر وملفات JSON متعددة الأسطر غير مدعومة هنا. تُحلّل أول سجلات الملف والسجلات المرشحة فقط؛ "
            "لا يُفحص بناء JSON للسجلات غير المرشحة. حد السجل 8 MiB. لا تغادر الملفات جهازك."
            if structured
            else "بحث محلي حرفي ضمن سطر واحد في UTF-8، دون تحليل JSON. السطر والموضع داخله يبدأان من 1؛ "
            "موضع الملف يبدأ من 0. المواضع بالبايت وليست بعدد الأحرف."
        )

    def _invalidate_resolution(self, *_):
        self.pending_resolution = None
        self.resolved_id = ""
        self.resolution.setText("لم يُحوّل اسم المستخدم بعد؛ لا يُرسل إلى الخدمة سوى الاسم الذي تُدخله.")
        if self.resolver and self.resolver.busy:
            self.resolver.cancel()
        self._actions()

    def _resolution_failed(self, message):
        self.pending_resolution = None
        self.resolved_id = ""
        self.resolution.setText(message)
        self._actions()

    def _resolution_idle(self):
        self._actions()
        self._poll()

    def _resolved(self, username, identifier):
        pending = self.pending_resolution
        self.pending_resolution = None
        if (
            self.closing
            or self.mode.currentIndex() != 1
            or pending is None
            or username != normalize_username(self.username.text())
            or username != pending[0]
        ):
            return
        self.resolved_id = identifier
        self.resolution.setText(
            f"الحساب: \u2066@{username}\u2069 — تم العثور على معرّف الحساب: \u2066{identifier}\u2069"
        )
        self._begin_scan(pending[1], identifier, pending[2], structured=True)

    def _actions(self):
        searching = self.task is not None
        resolving = bool(self.resolver and self.resolver.busy)
        viewing = any(not f.done() for f in self.view_futures)
        for widget in (self.choose, self.add, self.remove, self.clear, self.limit, self.start):
            widget.setEnabled(not searching and not resolving and not self.closing)
        for widget in (self.mode, self.username, self.query):
            widget.setEnabled(not searching and not self.closing)
        self.cancel.setEnabled((searching or resolving or viewing) and not self.closing)
        self.cancel.setText(
            "إلغاء التحويل"
            if resolving
            else "إلغاء البحث"
            if searching
            else "إلغاء تحميل السطر"
            if viewing
            else "إلغاء البحث"
        )
        usable = self.page is not None and not viewing and not self.closing
        self.copy_selected.setEnabled(usable)
        self.copy_line.setEnabled(usable)
        self.copy_location.setEnabled(usable)
        self.previous.setEnabled(usable and self.page.start > 0)
        self.next.setEnabled(usable and self.page.end < self.info.length)
        self.go.setEnabled(usable)

    def pick_files(self, append):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "اختيار ملفات البحث", "", "ملفات نصية (*.json *.jsonl *.txt);;جميع الملفات (*)"
        )
        if paths:
            self.set_files(paths, append=append)

    def set_files(self, paths, *, append=False):
        if self.task or self.closing or (self.resolver and self.resolver.busy):
            return
        chosen = list(self.files) if append else []
        keys = {file.key for file in chosen}
        skipped = 0
        for path in paths:
            try:
                file = FileInfo.capture(path)
            except SearchFailure as exc:
                self.status.setText(str(exc))
                continue
            if file.key in keys:
                skipped += 1
            else:
                chosen.append(file)
                keys.add(file.key)
        self.files = chosen
        self._file_rows()
        if skipped:
            self.status.setText(f"تُجاهلت {skipped} اختيارات مكررة؛ تُعامل الروابط إلى الملف نفسه كملف واحد.")

    def _file_rows(self):
        self.file_list.clear()
        self.file_details.toggle.setText(f"الملفات المحددة · {len(self.files)}  ▾")
        for file in self.files:
            self.file_list.addItem(f"{Path(file.path).name} — {size_text(file.size)}")
            self.file_list.item(self.file_list.count() - 1).setToolTip(file.path)

    def remove_file(self):
        if not self.task and 0 <= self.file_list.currentRow() < len(self.files):
            self.files.pop(self.file_list.currentRow())
            self._file_rows()

    def clear_files(self):
        if not self.task:
            self.files.clear()
            self._file_rows()

    def start_search(self):
        if self.task or self.closing or (self.resolver and self.resolver.busy):
            return
        try:
            text = self.limit.currentText()
            if not re.fullmatch(r"[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+", text):
                raise SearchFailure("limit")
            limit = int(text.replace(",", ""))
            structured = self.mode.currentIndex() == 1
            username = normalize_username(self.username.text())
            validate_search("1" if structured else self.query.text(), limit)
            if structured and (problem := syntax_error(username)):
                self.resolution.setText(problem)
                return
            if not self.files:
                self.status.setText("اختر ملفًا واحدًا على الأقل قبل بدء البحث.")
                return
        except (SearchFailure, ValueError) as exc:
            self.status.setText(str(exc) if isinstance(exc, SearchFailure) else ERRORS["limit"])
            return
        paths = tuple(file.path for file in self.files)
        if structured:
            self._invalidate_resolution()
            if self.resolver is None:
                self._resolution_failed("اتصال Osintgram غير متاح في هذه النافذة.")
                return
            self._cancel_view()
            self.info = self.page = None
            self.viewer.clear()
            self.page_controls.hide()
            self.model.reset("")
            self.last_summary = None
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("بانتظار تحويل اسم المستخدم")
            self.status.setText("لم يبدأ فحص الملفات؛ ننتظر معرّف الحساب المؤكد من الخدمة.")
            self.pending_resolution = (username, paths, limit)
            self.resolver.start(username)
            self._actions()
            return
        self._begin_scan(paths, self.query.text(), limit)

    def _begin_scan(self, paths, query, limit, *, structured=False):
        self.workspace.setCurrentIndex(1)
        self._cancel_view()
        self.info = self.page = None
        self.viewer.clear()
        self.page_controls.hide()
        self.line_status.setText("اختر إحدى النتائج لعرض سطرها الأصلي.")
        self.model.reset(query)
        if structured and self.table.columnWidth(4) == 145:
            self.table.setColumnWidth(4, min(260, self.table.fontMetrics().horizontalAdvance(query) + 28))
        self.last_summary = None
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("المفحوص فعليًا: %p%")
        self.status.setText("جارٍ البحث باستخدام معرّف الحساب…" if structured else "جارٍ البحث المحلي…")
        self.task = SearchTask(paths, query, limit, structured=structured)
        self.future = self.executor.submit(self.task.run)
        self.poll.start()
        self._actions()

    def _poll(self):
        if self.task:
            started = time.perf_counter()
            while time.perf_counter() - started < 0.012:
                try:
                    self.model.append(self.task.batches.get_nowait())
                except queue.Empty:
                    break
            value = self.task.latest
            if value:
                self.progress_bar.setValue(min(1000, value.processed * 1000 // max(1, value.total_bytes)))
                self.status.setText(
                    f"{Path(value.current_file).name} | الملفات: {value.files_done}/{value.files_total} | "
                    f"النتائج: {len(self.model.rows):,} | المقروء للفحص: {size_text(value.processed)} / "
                    f"{size_text(value.total_bytes)} | الزمن: {value.elapsed:.2f} ثانية"
                )
            if self.future.done() and self.task.batches.empty():
                try:
                    summary = self.future.result()
                    self.last_summary = summary
                    if summary.status == "limit":
                        message = (
                            f"اكتمل البحث حسب الحد المحدد — {self.task.limit} نتيجة\n"
                            "قد توجد نتائج إضافية لم تُفحص."
                        )
                    else:
                        message = {
                            "complete": "اكتمل البحث في جميع الملفات.",
                            "cancelled": "أُلغي البحث؛ النتائج المعروضة جزئية.",
                            "error": ERRORS.get(summary.error_code, ERRORS["unexpected"]),
                        }[summary.status]
                    p = summary.progress
                    self.progress_bar.setValue(min(1000, p.processed * 1000 // max(1, p.total_bytes)))
                    fraction = p.processed * 100 / max(1, p.total_bytes)
                    self.progress_bar.setFormat(
                        f"اكتمل حسب الحد — المفحوص فعليًا: {fraction:.3f}%"
                        if summary.status == "limit"
                        else "المفحوص فعليًا: %p%"
                    )
                    self.status.setText(
                        f"{message}\nالنتائج: {len(self.model.rows):,} | الملفات المكتملة: {p.files_done}/"
                        f"{p.files_total} | المفحوص: {size_text(p.processed)} | الزمن: {p.elapsed:.2f} ثانية"
                    )
                    if summary.status == "complete":
                        self.progress_bar.setValue(1000)
                except Exception:
                    self.status.setText(ERRORS["unexpected"])
                self.task = self.future = None
                self._actions()
        self.view_futures = {future for future in self.view_futures if not future.done()}
        if not self.busy:
            self.poll.stop()
            if self.closing and not self.executor_closed:
                self.executor.shutdown(wait=False, cancel_futures=True)
                self.executor_closed = True
            self.idle.emit()

    def _cancel_view(self):
        self.view_cancel.set()
        if self.view_future:
            self.view_future.cancel()
        self.view_token += 1
        self.view_cancel = Event()

    def _submit_view(self, kind, function):
        token = self.view_token
        future = self.executor.submit(function)
        self.view_future = future
        self.view_futures.add(future)

        def finished(result):
            try:
                self.bridge.loaded.emit(token, kind, result)
            except RuntimeError:
                pass  # A completed worker must not access a destroyed Qt receiver.

        future.add_done_callback(finished)
        self.poll.start()
        self._actions()

    def open_result(self, current, previous=None):
        if self.closing or not current.isValid():
            return
        match = self.model.rows[current.row()]
        self._cancel_view()
        self.info = self.page = None
        self.viewer.clear()
        self.page_controls.hide()
        self.line_status.setText("نقرأ السطر الأصلي ونتحقق من عدم تغيره…")
        cancel = self.view_cancel

        def load():
            info = inspect_line(match, cancel)
            start = max(0, match.offset - match.line_start - PAGE_BYTES // 4)
            return read_page(info, start, cancel)

        self._submit_view("page", load)

    def load_page(self, start):
        if self.info is None or self.closing:
            return
        self._cancel_view()
        info, cancel = self.info, self.view_cancel
        self._submit_view("page", lambda: read_page(info, start, cancel))

    def _view_done(self, token, kind, future):
        self.view_futures.discard(future)
        if token != self.view_token or self.closing:
            return
        try:
            page = future.result()
            if kind == "copy":
                QApplication.clipboard().setText(page.text)
                self.line_status.setText("نُسخ السطر كاملًا دون محرف نهاية السطر.")
            else:
                self.page, self.info = page, page.info
                self.viewer.set_source(page.text)
                match = page.info.match
                absolute = match.line_start + page.start
                relative = match.offset - absolute
                raw = page.text.encode("utf-8")
                highlights = []
                if 0 <= relative < len(raw):
                    before = raw[:relative].decode("utf-8")
                    position = len(before.encode("utf-16-le")) // 2
                    cursor = self.viewer.textCursor()
                    cursor.setPosition(position)
                    query = self.model.query
                    end = min(
                        len(page.text.encode("utf-16-le")) // 2,
                        position + len(query.encode("utf-16-le")) // 2,
                    )
                    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                    highlight = QTextEdit.ExtraSelection()
                    highlight.cursor = cursor
                    highlight.format.setBackground(QColor("#ffe2a0"))
                    highlights.append(highlight)
                    self.viewer.setTextCursor(cursor)
                    self.viewer.ensureCursorVisible()
                self.viewer.setExtraSelections(highlights)
                paged = self.info.length > WHOLE_VIEW_BYTES
                self.page_controls.setVisible(paged)
                self.page_number.setRange(1, max(1, math.ceil(self.info.length / PAGE_BYTES)))
                self.page_number.setValue(page.requested // PAGE_BYTES + 1)
                location = (
                    f"{Path(match.file.path).name} — السطر {match.line:,} — الموضع {match.byte_column:,} بايت"
                )
                detail = (
                    f"عرض صفحة من سطر كبير: البايتات {page.start + 1:,}–{page.end:,} "
                    f"من {self.info.length:,}. "
                    "جميع الصفحات متاحة دون اقتطاع صامت."
                    if paged
                    else "السطر كاملًا دون محرف نهايته."
                )
                if self.info.length > MAX_COPY_BYTES:
                    detail += " النسخ الكامل للحافظة محدود إلى 4 MiB؛ يمكن قراءة المحتوى كاملًا عبر الصفحات."
                self.line_status.setText(location + "\n" + detail)
                self.line_status.setToolTip(match.file.path)
        except CancelledError:
            self.line_status.setText("أُلغي تحميل السطر.")
        except SearchFailure as exc:
            self.info = self.page = None
            self.viewer.clear()
            self.line_status.setText(str(exc))
        except Exception:
            self.info = self.page = None
            self.viewer.clear()
            self.line_status.setText(ERRORS["unexpected"])
        self._actions()

    def copy_whole_line(self):
        if self.info is None or self.closing:
            return
        if self.info.length > MAX_COPY_BYTES:
            self.line_status.setText(ERRORS["copy_size"])
            return
        self._cancel_view()
        info, cancel = self.info, self.view_cancel
        self._submit_view("copy", lambda: read_page(info, 0, cancel, whole=True))

    def copy_source_location(self):
        if self.info:
            match = self.info.match
            QApplication.clipboard().setText(f"{match.file.path}:{match.line}")

    def cancel_work(self):
        if self.resolver and self.resolver.busy:
            self.pending_resolution = None
            self.resolver.cancel()
            self.resolution.setText("أُلغي تحويل اسم المستخدم؛ لم يبدأ البحث داخل الملفات.")
        if self.task:
            self.task.cancel.set()
        self.view_cancel.set()

    def shutdown(self):
        self.closing = True
        self.cancel_work()
        if self.resolver:
            self.resolver.shutdown()
        for future in self.view_futures:
            future.cancel()
        self._actions()
        if self.busy:
            self.poll.start()
        elif not self.executor_closed:
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.executor_closed = True
            self.poll.stop()
