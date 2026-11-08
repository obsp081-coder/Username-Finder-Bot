"""Instagram local preparation and separately scoped Osintgram analysis UI."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .osintgram_ui import OsintgramPanel


def normalize_username(value: str) -> str:
    return value.strip().removeprefix("@").lower()


def syntax_error(username: str) -> str | None:
    """Basic local format checks, not Instagram acceptance or availability."""
    if not username:
        return "اسم المستخدم فارغ."
    if len(username) > 30:
        return "الحد الأقصى 30 محرفًا."
    if not re.fullmatch(r"[a-z0-9._]+", username):
        return "استخدم الحروف اللاتينية والأرقام والنقطة والشرطة السفلية فقط."
    if username.startswith(".") or username.endswith(".") or ".." in username:
        return "لا تضع نقطة في البداية أو النهاية، أو نقطتين متتاليتين."
    return None


def local_preview(manual: str, files: tuple[str, ...]) -> dict:
    """Read a small UTF-8 sample, independent of Snapchat normalization and rules."""
    counts = dict.fromkeys(("valid", "invalid", "duplicate", "blank", "malformed", "lines"), 0)
    seen = set()
    examples = []
    warnings = []
    consumed = 0
    limited = False

    def add(value, source):
        name = normalize_username(value)
        if not name:
            counts["blank"] += 1
            return
        if name in seen:
            counts["duplicate"] += 1
            return
        seen.add(name)
        error = syntax_error(name)
        counts["invalid" if error else "valid"] += 1
        if len(examples) < 20:
            examples.append((name, error, source))

    if manual.strip():
        add(manual, "إدخال يدوي")
    for index, filename in enumerate(files):
        if index >= 20 or counts["lines"] >= 200 or consumed >= 65536:
            limited = True
            break
        try:
            with Path(filename).open("rb") as stream:
                first_line = True
                while True:
                    remaining = 65536 - consumed
                    if counts["lines"] >= 200 or remaining <= 0:
                        limited = True
                        break
                    raw = stream.readline(remaining + 1)
                    consumed += len(raw)
                    if not raw:
                        break
                    if len(raw) > remaining:
                        limited = True
                        break
                    counts["lines"] += 1
                    try:
                        value = raw.decode("utf-8-sig" if first_line else "utf-8")
                    except UnicodeDecodeError:
                        counts["malformed"] += 1
                    else:
                        add(value, f"ملف {index + 1}")
                    first_line = False
        except OSError:
            warnings.append(f"تعذر قراءة الملف {index + 1}؛ تحقق من وجوده وصلاحية الوصول.")
    if counts["malformed"]:
        warnings.append("توجد أسطر بترميز غير مدعوم؛ احفظ ملفات TXT بترميز UTF-8.")
    return {"counts": counts, "examples": examples, "warnings": warnings, "limited": limited}


class InstagramPanel(QScrollArea):
    def __init__(self, make_table, *, osintgram_options=None):
        super().__init__()
        self.setWidgetResizable(True)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.last_preview = None
        content = QWidget()
        outer = QHBoxLayout(content)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.addStretch()
        self.body = QWidget()
        self.body.setMaximumWidth(1240)
        outer.addWidget(self.body, 1)
        outer.addStretch()
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.modes = QTabBar()
        self.modes.addTab("فحص أسماء المستخدمين")
        self.modes.addTab("تحليل حسابات Instagram")
        self.modes.setExpanding(False)
        layout.addWidget(self.modes)
        body_layout = layout
        self.names_page = QWidget()
        layout = QVBoxLayout(self.names_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        body_layout.addWidget(self.names_page, 1)
        header = QHBoxLayout()
        headings = QVBoxLayout()
        headings.setSpacing(3)
        self.title = QLabel("فحص يوزرات إنستغرام")
        self.title.setObjectName("igTitle")
        self.title.setWordWrap(True)
        headings.addWidget(self.title)
        subtitle = QLabel("تجهيز الأسماء ومعاينتها قبل تفعيل الفحص الحقيقي")
        subtitle.setObjectName("igSubtitle")
        subtitle.setWordWrap(True)
        headings.addWidget(subtitle)
        header.addLayout(headings, 1)
        self.indicator = QLabel("التحقق الحقيقي غير مفعّل")
        self.indicator.setObjectName("igStatus")
        self.indicator.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        header.addWidget(self.indicator)
        layout.addLayout(header)

        self.input_layout = QGridLayout()
        self.input_layout.setSpacing(10)
        self.manual_group = QGroupBox("إدخال يدوي")
        form = QVBoxLayout(self.manual_group)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        manual_row = QHBoxLayout()
        manual_row.addWidget(QLabel("اسم المستخدم"))
        self.manual = QLineEdit()
        self.manual.setPlaceholderText("username أو @username")
        self.manual.setAccessibleName("اسم مستخدم Instagram")
        self.manual.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.manual.setMaximumWidth(410)
        self.manual.setMinimumWidth(220)
        manual_row.addWidget(self.manual, 1)
        manual_row.addStretch()
        form.addLayout(manual_row)
        self.feedback = QLabel("أدخل الاسم ثم اضغط «معاينة الأسماء»، أو Enter.")
        self.feedback.setWordWrap(True)
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        form.addWidget(self.feedback)
        rules = QLabel("\u20661–30\u2069 محرفًا؛ حروف لاتينية وأرقام ونقطة وشرطة سفلية.")
        rules.setObjectName("igHint")
        rules.setWordWrap(True)
        form.addWidget(rules)
        form.addStretch()

        self.txt_group = QGroupBox("ملفات TXT")
        file_layout = QVBoxLayout(self.txt_group)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.setSpacing(6)
        self.file_count = QLabel("لم تُحدد ملفات بعد.")
        self.file_count.setObjectName("igHint")
        file_layout.addWidget(self.file_count)
        self.files = QListWidget()
        self.files.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.files.setAccessibleName("ملفات أسماء Instagram")
        file_layout.addWidget(self.files)
        self.files_empty = QLabel("اختر ملفات UTF-8؛ اسم مستخدم واحد في كل سطر.")
        self.files_empty.setObjectName("igFileEmpty")
        self.files_empty.setWordWrap(True)
        file_layout.addWidget(self.files_empty)
        file_actions = QHBoxLayout()
        file_actions.setSpacing(8)
        self.add_button = QPushButton("إضافة ملفات…")
        self.add_button.clicked.connect(self.add_files)
        file_actions.addWidget(self.add_button)
        self.remove_button = QPushButton("إزالة المحدد")
        self.remove_button.clicked.connect(self.remove_file)
        file_actions.addWidget(self.remove_button)
        file_actions.addStretch()
        file_layout.addLayout(file_actions)
        file_layout.addStretch()
        layout.addLayout(self.input_layout)

        preview_group = QGroupBox("معاينة الأسماء")
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(6)
        preview_header = QHBoxLayout()
        self.preview_button = QPushButton("معاينة الأسماء")
        self.preview_button.setObjectName("igPreview")
        self.preview_button.clicked.connect(self.preview)
        self.preview_summary = QLabel("أدخل اسمًا أو اختر TXT لعرض معاينة الصيغة فقط.")
        self.preview_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.preview_summary.setWordWrap(True)
        preview_header.addWidget(self.preview_summary, 1)
        preview_header.addWidget(self.preview_button)
        preview_layout.addLayout(preview_header)
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setFixedHeight(84)
        self.preview_text.setAccessibleName("معاينة صيغة أسماء Instagram")
        self.preview_text.setPlaceholderText("تظهر هنا عينة الأسماء وحالة صيغتها المحلية، دون نتائج توفر.")
        preview_layout.addWidget(self.preview_text)
        self.preview_text.hide()
        layout.addWidget(preview_group)

        verification = QHBoxLayout()
        self.start_button = QPushButton("بدء الفحص")
        self.start_button.setObjectName("primary")
        self.start_button.setEnabled(False)
        self.start_button.setToolTip("يلزم موفر Instagram موثوق ومتحقق منه قبل تفعيل الفحص.")
        self.start_button.setFixedWidth(160)
        verification.addWidget(self.start_button)
        disabled_note = QLabel("يتطلب التفعيل موفر Instagram موثوقًا ومتحققًا منه.")
        disabled_note.setWordWrap(True)
        verification.addWidget(disabled_note, 1)
        layout.addLayout(verification)

        results = QGroupBox("نتائج Instagram")
        results_layout = QVBoxLayout(results)
        results_layout.setContentsMargins(0, 0, 0, 0)
        self.counter_layout = QGridLayout()
        self.counter_layout.setSpacing(8)
        self.counters = {}
        for column, label in enumerate(("متاح", "مستخدم", "محجوز", "غير صالح", "غير محسوم")):
            card = QLabel(label + "\n0")
            card.setObjectName("igCounter")
            card.setFixedHeight(58)
            card.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.counters[label] = card
            self.counter_layout.addWidget(card, 0, column)
        results_layout.addLayout(self.counter_layout)
        self.result_table = make_table(["اسم المستخدم", "النتيجة", "التفاصيل", "الوقت"])
        self.result_table.setObjectName("igResultsTable")
        table_header = self.result_table.horizontalHeader()
        table_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table_header.setStretchLastSection(False)
        for column, width in ((0, 190), (1, 115), (3, 140)):
            self.result_table.setColumnWidth(column, width)
        table_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.result_table.setMinimumHeight(140)
        self.result_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.result_table.setAccessibleName("نتائج Instagram المستقلة")
        results_layout.addWidget(self.result_table, 1)
        self.results_empty = QLabel("لا توجد نتائج تحقق بعد.\nتظهر نتائج الفحص هنا عند تفعيل موفر موثوق.")
        self.results_empty.setObjectName("igHint")
        self.results_empty.setWordWrap(True)
        self.results_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.results_empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        empty_layout = QVBoxLayout(self.result_table.viewport())
        empty_layout.addStretch()
        empty_layout.addWidget(self.results_empty)
        empty_layout.addStretch()
        layout.addWidget(results, 1)
        self.analysis = OsintgramPanel(self.body, **(osintgram_options or {}))
        body_layout.addWidget(self.analysis, 1)
        self.analysis.hide()
        self.modes.currentChanged.connect(self._mode_changed)
        self.setStyleSheet("""
            QGroupBox { padding: 10px; margin-top: 12px; border-color: #dcd8e4; }
            QGroupBox::title { color: #654879; }
            QLabel#igTitle { font-size: 22px; font-weight: bold; color: #684581; }
            QLabel#igSubtitle, QLabel#igHint { font-size: 14px; color: #59677a; }
            QLabel#igStatus { background: #fff3d7; color: #71521d;
                border: 1px solid #ead8ac; border-radius: 6px; padding: 6px 10px; font-size: 14px; }
            QLabel#igFileEmpty { background: #f8f7fb; color: #59677a;
                border: 1px solid #e6e1ed; border-radius: 5px; padding: 8px; font-size: 14px; }
            QLabel#igCounter { background: #faf9fc; color: #344258;
                border: 1px solid #e2dce9; border-radius: 6px; padding: 4px; font-size: 15px; }
            QPushButton#igPreview { background: #f4eff9; color: #654879; border-color: #cbbbd9; }
            QPushButton#igPreview:hover { background: #eae0f4; }
            QTableWidget#igResultsTable QHeaderView::section { padding: 6px 8px; }
        """)
        self.setWidget(content)
        self._input_columns = self._counter_columns = 0
        self._reflow()
        self._update_files()
        self.manual.textChanged.connect(self.input_changed)
        self.manual.returnPressed.connect(self.preview)
        self.files.currentRowChanged.connect(self._update_files)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_input_columns"):
            self._reflow()

    def _mode_changed(self, index):
        self.names_page.setVisible(index == 0)
        self.analysis.setVisible(index == 1)
        self.verticalScrollBar().setValue(0)
        if index == 1:
            self.analysis.activate()

    def _reflow(self):
        width = min(self.viewport().width() - 24, 1240)
        columns = 2 if width >= 980 else 1
        if columns != self._input_columns:
            self._input_columns = columns
            for index, group in enumerate((self.manual_group, self.txt_group)):
                self.input_layout.removeWidget(group)
                self.input_layout.addWidget(group, index // columns, index % columns)
            self.input_layout.setColumnStretch(0, 1)
            self.input_layout.setColumnStretch(1, 1 if columns == 2 else 0)
        counter_columns = 5 if width >= 740 else 3
        if counter_columns != self._counter_columns:
            self._counter_columns = counter_columns
            for index, card in enumerate(self.counters.values()):
                self.counter_layout.removeWidget(card)
                self.counter_layout.addWidget(card, index // counter_columns, index % counter_columns)
            for index in range(5):
                self.counter_layout.setColumnStretch(index, 1 if index < counter_columns else 0)

    def _update_files(self):
        count = self.files.count()
        self.file_count.setText(f"الملفات المحددة: {count}" if count else "لم تُحدد ملفات بعد.")
        self.files.setVisible(count > 0)
        row_height = max(24, self.files.sizeHintForRow(0))
        self.files.setFixedHeight(min(3, max(1, count)) * row_height + 24)
        self.files_empty.setVisible(count == 0)
        self.remove_button.setEnabled(self.files.currentRow() >= 0)

    def input_changed(self):
        name = normalize_username(self.manual.text())
        error = syntax_error(name)
        self.feedback.setText(
            "أدخل الاسم ثم اضغط «معاينة الأسماء»، أو Enter."
            if not self.manual.text().strip()
            else error or "الصيغة الأولية مقبولة محليًا؛ لم تُفحص الإتاحة."
        )
        if self.last_preview is not None:
            self.preview_summary.setText("تغير الإدخال؛ اضغط معاينة الأسماء لتحديث العينة.")
            self.preview_text.clear()
            self.preview_text.hide()
            self.last_preview = None

    def add_files(self):
        filenames, _ = QFileDialog.getOpenFileNames(
            self, "اختر ملفات أسماء Instagram", "", "Text files (*.txt)"
        )
        existing = {self.files.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.files.count())}
        for filename in filenames:
            if filename not in existing:
                item = QListWidgetItem(Path(filename).name)
                item.setData(Qt.ItemDataRole.UserRole, filename)
                item.setToolTip(filename)
                self.files.addItem(item)
                existing.add(filename)
        if filenames:
            self._update_files()
            self.input_changed()

    def remove_file(self):
        row = self.files.currentRow()
        if row >= 0:
            self.files.takeItem(row)
            self._update_files()
            self.input_changed()

    def preview(self):
        files = tuple(self.files.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.files.count()))
        result = local_preview(self.manual.text(), files)
        self.last_preview = result
        counts = result["counts"]
        if not self.manual.text().strip() and not files:
            summary = "أدخل اسمًا أو اختر ملف TXT أولًا."
        else:
            summary = (
                f"مطابق للصيغة محليًا: {counts['valid']} • غير مطابق: {counts['invalid']} • "
                f"مكرر: {counts['duplicate']} • فارغ: {counts['blank']}"
            )
            if result["limited"]:
                summary += "\nمعاينة جزئية؛ الحد 200 سطر و64 KiB و20 ملفًا، وليست حصرًا لكل الأسماء."
            summary += "\nلم تُفحص الإتاحة؛ لم تُنشأ أي مهمة."
        if result["warnings"]:
            summary += "\n" + "\n".join(result["warnings"])
        self.preview_summary.setText(summary)
        self.preview_text.setPlainText(
            "\n".join(
                f"\u2066{name}\u2069 — {error or 'مطابق للصيغة محليًا'} ({source})"
                for name, error, source in result["examples"]
            )
        )
        self.preview_text.setVisible(bool(result["examples"]))
