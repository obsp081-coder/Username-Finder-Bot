"""Compact shared filters and cancellable, versioned read-only result queries."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

from PySide6.QtCore import QObject, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .result_search import SYMBOL_RULES, ResultFilter, ResultSearch, SearchCancelled
from .ui_helpers import style_combo_popup


def choice(items):
    box = QComboBox()
    for label, value in items:
        box.addItem(label, value)
    style_combo_popup(box)
    return box


class AdvancedResultFilters(QWidget):
    changed = Signal()

    def __init__(self, scope, search, classification, changes=None):
        super().__init__()
        self.scope, self.search, self.classification, self.changes = scope, search, classification, changes
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(5)
        basic = QHBoxLayout()
        self.text_mode = choice(
            [("يحتوي", "contains"), ("يبدأ بـ", "starts"), ("ينتهي بـ", "ends"), ("مطابق تمامًا", "exact")]
        )
        basic.addWidget(QLabel("مطابقة النص"))
        basic.addWidget(self.text_mode)
        self.toggle = QPushButton("تصفية متقدمة")
        self.toggle.setCheckable(True)
        basic.addWidget(self.toggle)
        self.reset_button = QPushButton("إعادة التصفية")
        basic.addWidget(self.reset_button)
        self.cancel_button = QPushButton("إلغاء البحث")
        basic.addWidget(self.cancel_button)
        root.addLayout(basic)
        self.advanced = QWidget()
        self.advanced.setMaximumWidth(780)
        grid = QGridLayout(self.advanced)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        self.length_mode = choice([("أي طول", "any"), ("طول محدد", "exact"), ("نطاق شامل", "range")])
        self.minimum, self.maximum = QSpinBox(), QSpinBox()
        self.minimum.setAccessibleName("الطول الأدنى أو المحدد")
        self.maximum.setAccessibleName("الطول الأعلى")
        self.maximum.setPrefix("إلى ")
        self.composition = choice(
            [
                ("أي تركيب", "any"),
                ("حروف فقط", "letters"),
                ("أرقام فقط", "digits"),
                ("حروف وأرقام معًا؛ دون رموز", "mixed_plain"),
                ("حروف أو أرقام فقط", "alphanumeric"),
                ("حرف واحد على الأقل", "letter"),
                ("رقم واحد على الأقل", "digit"),
                ("حروف وأرقام معًا؛ الرموز مسموحة", "mixed"),
                ("يحتوي رموزًا", "symbols"),
                ("دون رموز", "plain"),
            ]
        )
        self.repetition = choice(
            [
                ("أي تكرار", "any"),
                ("لا محارف مكررة", "none"),
                ("محرف مكرر في أي موضع", "anywhere"),
                ("تكرار متتالٍ", "consecutive"),
                ("حروف مكررة", "letters"),
                ("أرقام مكررة", "digits"),
            ]
        )
        counts = [("دون شرط", "any"), ("على الأقل", "minimum"), ("بالضبط", "exact")]
        self.digit_mode, self.letter_mode = choice(counts), choice(counts)
        self.digits, self.letters = QSpinBox(), QSpinBox()
        for number in (self.minimum, self.maximum, self.digits, self.letters):
            number.setRange(0, 1000000)
            number.setMaximumWidth(135)
        self.symbol_mode = choice(
            [
                ("دون قيد رموز", "any"),
                ("يحتوي أي رمز", "present"),
                ("دون أي رمز", "absent"),
                ("يحتوي الرمز المحدد", "contains"),
                ("لا يحتوي الرمز المحدد", "excludes"),
            ]
        )
        self.symbol = QComboBox()
        self.symbol.setEditable(True)
        self.symbol.setMaximumWidth(100)
        self.symbol.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.symbol.lineEdit().setMaxLength(1)
        self.symbol.setToolTip("يمكن إدخال رمز آخر للبحث في البيانات غير الصالحة؛ لا يعني أنه مدعوم للمنصة.")
        style_combo_popup(self.symbol)
        for row, label, controls in (
            (0, "الطول", (self.length_mode, self.minimum, self.maximum)),
            (1, "التركيب / التكرار", (self.composition, self.repetition)),
            (2, "عدد الأرقام", (self.digit_mode, self.digits)),
            (3, "عدد الحروف", (self.letter_mode, self.letters)),
            (4, "الرموز", (self.symbol_mode, self.symbol)),
        ):
            grid.addWidget(QLabel(label), row, 0)
            for column, control in enumerate(controls, 1):
                grid.addWidget(control, row, column)
        self.rules = QLabel()
        self.rules.setWordWrap(True)
        grid.addWidget(self.rules, 5, 0, 1, 4)
        self.advanced_scroll = QScrollArea()
        self.advanced_scroll.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignTop
        )
        self.advanced_scroll.setWidgetResizable(True)
        self.advanced_scroll.setMaximumHeight(190)
        self.advanced_scroll.setWidget(self.advanced)
        root.addWidget(self.advanced_scroll)
        self.advanced_scroll.hide()
        self.toggle.toggled.connect(self.advanced_scroll.setVisible)
        self.active = QLabel()
        self.active.setWordWrap(True)
        self.active.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.active)
        self.controls = (
            self.text_mode,
            self.length_mode,
            self.minimum,
            self.maximum,
            self.composition,
            self.repetition,
            self.digit_mode,
            self.digits,
            self.letter_mode,
            self.letters,
            self.symbol_mode,
            self.symbol,
        )
        for control in self.controls:
            signal = control.valueChanged if isinstance(control, QSpinBox) else control.currentTextChanged
            signal.connect(self.update_filters)
        self.reset_button.clicked.connect(self.reset)
        self.set_scope(scope)

    def set_scope(self, scope):
        self.scope = scope
        previous = self.symbol.currentText() or "_"
        blocker = QSignalBlocker(self.symbol)
        symbols = "".join(
            dict.fromkeys(
                "".join(
                    rule["symbols"] for name, rule in SYMBOL_RULES.items() if scope == "All" or name == scope
                )
            )
        )
        self.symbol.clear()
        self.symbol.addItems(list(symbols))
        self.symbol.setCurrentText(previous)
        del blocker
        notes = " ".join(
            rule["note"] for name, rule in SYMBOL_RULES.items() if scope == "All" or name == scope
        )
        self.rules.setText(
            notes + " البحث يشمل الرموز غير المدعومة في السجل أيضًا. الحروف A–Z والأرقام 0–9؛ "
            "بقية المحارف رموز لأغراض التصفية. المطابقة والتكرار يتجاهلان حالة الحروف."
        )
        self.update_filters()

    def filters(self):
        return ResultFilter(
            text=self.search.text(),
            text_mode=self.text_mode.currentData(),
            classification=self.classification.currentData(),
            length_mode=self.length_mode.currentData(),
            minimum=self.minimum.value(),
            maximum=self.maximum.value(),
            composition=self.composition.currentData(),
            repetition=self.repetition.currentData(),
            digit_mode=self.digit_mode.currentData(),
            digits=self.digits.value(),
            letter_mode=self.letter_mode.currentData(),
            letters=self.letters.value(),
            symbol_mode=self.symbol_mode.currentData(),
            symbol=self.symbol.currentText(),
            changed=bool(self.changes and self.changes.isChecked()),
        ).validate()

    def update_filters(self):
        self.minimum.setEnabled(self.length_mode.currentData() != "any")
        self.maximum.setEnabled(self.length_mode.currentData() == "range")
        self.digits.setEnabled(self.digit_mode.currentData() != "any")
        self.letters.setEnabled(self.letter_mode.currentData() != "any")
        self.symbol.setEnabled(self.symbol_mode.currentData() in ("contains", "excludes"))
        try:
            filters = self.filters()
            defaults = ResultFilter()
            count = sum(
                (
                    bool(filters.text),
                    bool(filters.classification),
                    filters.changed,
                    filters.length_mode != defaults.length_mode,
                    filters.composition != defaults.composition,
                    filters.repetition != defaults.repetition,
                    filters.digit_mode != "any",
                    filters.letter_mode != "any",
                    filters.symbol_mode != "any",
                )
            )
            self.active.setText(f"شروط فعّالة: {count} • جميع الشروط معًا (AND) • أحدث نتيجة لكل اسم.")
        except ValueError as exc:
            self.active.setText(str(exc))
        self.changed.emit()

    def reset(self):
        controls = (
            *self.controls,
            self.search,
            self.classification,
            *((self.changes,) if self.changes else ()),
        )
        blockers = [QSignalBlocker(control) for control in controls]
        for control in self.controls:
            if isinstance(control, QSpinBox):
                control.setValue(0)
            elif control is self.symbol:
                control.setCurrentText("_")
            else:
                control.setCurrentIndex(0)
        self.search.clear()
        self.classification.setCurrentIndex(0)
        if self.changes:
            self.changes.setChecked(False)
        del blockers
        self.update_filters()

    def is_default(self):
        f = self.filters()
        return replace(f, minimum=0, maximum=0, digits=0, letters=0, symbol="_") == ResultFilter()


class ResultQueryController(QObject):
    ready = Signal(object, int, int, object)
    cleared = Signal(str)
    completed = Signal(int, object, str)

    def __init__(self, parent):
        super().__init__(parent)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="result-query")
        self.version = 0
        self.cancel_event = Event()
        self.closed = False
        self.future = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.launch)
        self.completed.connect(self.deliver)

    def cancel(self, message="أُلغي البحث؛ غيّر التصفية أو الصفحة لإعادته."):
        self.version += 1
        self.cancel_event.set()
        self.timer.stop()
        if self.future:
            self.future.cancel()
        if message is not None:
            self.cleared.emit(message)

    def request(self, path, scope, filters, offset):
        if self.closed:
            return
        if (
            getattr(self, "pending", ())[:4] == (path, scope, filters, offset)
            and not self.cancel_event.is_set()
            and (self.timer.isActive() or (self.future and not self.future.done()))
        ):
            return
        self.cancel("جارٍ البحث في جميع النتائج المحفوظة…")
        self.cancel_event = Event()
        self.pending = (path, scope, filters, offset, self.cancel_event, self.version)
        self.timer.start()

    def launch(self):
        path, scope, filters, offset, cancel, version = self.pending

        def work():
            try:
                search = ResultSearch(path)
                result = (*search.page(scope, filters, offset=offset, cancel=cancel), search.last_counts)
                error = ""
            except SearchCancelled:
                return
            except Exception as exc:
                result, error = None, str(exc)
            try:
                self.completed.emit(version, result, error)
            except RuntimeError:
                pass  # The owning window may already have been destroyed.

        self.future = self.executor.submit(work)

    def deliver(self, version, result, error):
        if self.closed or version != self.version:
            return
        if error:
            self.cleared.emit("تعذر البحث: " + error)
        else:
            self.ready.emit(*result)

    def shutdown(self):
        if not self.closed:
            self.closed = True
            self.cancel("")
            self.executor.shutdown(wait=False, cancel_futures=True)
