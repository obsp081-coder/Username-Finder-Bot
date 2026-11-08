"""Compact Qt workspaces with independently scrollable platform sections."""

from PySide6.QtCore import QObject, QSignalBlocker, QSize, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .ui_helpers import style_combo_popup


class ResultsPresentation(QWidget):
    """Change visible columns only; never reload, filter, or alter result evidence."""

    def __init__(self, table, simple_columns, platform):
        super().__init__()
        self.table = table
        self.simple_columns = frozenset(simple_columns)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        label = QLabel(platform + " · عرض النتائج")
        label.setObjectName("heading")
        layout.addWidget(label)
        layout.addStretch()
        self.mode = QComboBox()
        self.mode.addItems(["بسيط", "متقدم · الدليل والتوقيت"])
        self.mode.setAccessibleName("طريقة عرض النتائج")
        self.mode.setToolTip("العرض فقط؛ لا تتغير التصنيفات أو التصفية أو التصدير.")
        style_combo_popup(self.mode)
        layout.addWidget(self.mode)
        self.mode.currentIndexChanged.connect(self.apply)
        self.apply()

    def apply(self, *_):
        simple = self.mode.currentIndex() == 0
        was_simple = bool(self.table.property("simpleResultsView"))
        visible = [c for c in range(self.table.columnCount()) if not self.table.isColumnHidden(c)]
        widths = {c: self.table.columnWidth(c) for c in visible[:-1]} if was_simple else {}
        self.table.setProperty("simpleResultsView", simple)
        with QSignalBlocker(self.table.horizontalHeader()):
            self.table.horizontalHeader().setStretchLastSection(simple)
            for column in range(self.table.columnCount()):
                self.table.setColumnHidden(column, simple and column not in self.simple_columns)
                if was_simple and not simple:
                    columns = getattr(self.table, "readable_columns", None)
                    width = columns.user_widths.get(column) if columns else None
                    width = width or widths.get(column)
                    if width:
                        self.table.setColumnWidth(column, width)
                    else:
                        self.table.resizeColumnToContents(column)


def generation_fields(minimum, maximum, quantity):
    """Keep related fields together without changing their validation or signals."""
    group = QWidget()
    group.setMaximumWidth(580)
    grid = QGridLayout(group)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(12)
    for row, column, text, control in (
        (0, 0, "من طول", minimum),
        (0, 2, "إلى طول", maximum),
        (1, 0, "إجمالي الأسماء", quantity),
    ):
        label = QLabel(text)
        label.setBuddy(control)
        control.setMinimumWidth(90)
        control.setMaximumWidth(180 if control is quantity else 120)
        grid.addWidget(label, row, column)
        grid.addWidget(control, row, column + 1)
    return group


class ReadableColumns(QObject):
    """Size visible page data fully while retaining deliberate user column widths."""

    def __init__(self, table, fill_table):
        super().__init__(table)
        self.table, self.fill_table = table, fill_table
        table.readable_columns = self
        self.filling = False
        self.sized = False
        self.user_widths = {}
        table.horizontalHeader().sectionResized.connect(self.resized)

    def resized(self, index, old, new):
        if self.table.isColumnHidden(index):
            return
        if self.table.property("simpleResultsView"):
            visible = [c for c in range(self.table.columnCount()) if not self.table.isColumnHidden(c)]
            if visible and index == visible[-1]:
                return  # Stretching a presentation column is not a user resize.
        if not self.filling and self.sized:
            self.user_widths[index] = new

    def fill(self, rows):
        self.filling = True
        try:
            self.fill_table(self.table, rows)
            self.table.horizontalHeader().setStretchLastSection(
                bool(self.table.property("simpleResultsView"))
            )
            for column in range(self.table.columnCount()):
                if column in self.user_widths:
                    self.table.setColumnWidth(column, self.user_widths[column])
                else:
                    self.table.resizeColumnToContents(column)
                for row in range(self.table.rowCount()):
                    item = self.table.item(row, column)
                    if item and not item.toolTip():
                        item.setToolTip(item.text())
            # Initial empty-table layout changes are not deliberate column resizing.
            self.sized = self.sized or bool(rows)
        finally:
            self.filling = False


class PlatformWorkspace(QTabWidget):
    def sizeHint(self):
        return QSize(super().sizeHint().width(), 100)

    def minimumSizeHint(self):
        # The inner pages scroll; their content must not push the fixed actions below the viewport.
        return QSize(0, 100)

    def add_page(self, title, *, expand=False):
        area = QScrollArea()
        area.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        if not expand:
            layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        area.setWidget(content)
        self.addTab(area, title)
        return layout
