"""Lightweight presentation primitives; no persistence, provider, or polling work."""

from functools import lru_cache

from PySide6.QtCore import QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

PATHS = {
    "home": '<path d="m3 11 9-8 9 8v10h-6v-7H9v7H3z"/>',
    "layers": '<path d="m2 7 10-5 10 5-10 5zm0 5 10 5 10-5M2 17l10 5 10-5"/>',
    "search": '<circle cx="10" cy="10" r="7"/><path d="m15 15 7 7"/>',
    "file": '<path d="M5 2h9l5 5v15H5zM14 2v6h5M8 12h8m-8 4h8"/>',
    "folder": '<path d="M2 6V4h7l3 3h10v14H2z"/>',
    "import": '<path d="M3 16v5h18v-5M12 2v13m-5-5 5 5 5-5"/>',
    "generate": '<path d="m13 2 2.5 6.5L22 11l-6.5 2.5L13 20l-2.5-6.5L4 11l6.5-2.5zM3 1v5M.5 3.5h5"/>',
    "filter": '<path d="M2 3h20l-8 9v9l-4-2v-7z"/>',
    "check": '<circle cx="12" cy="12" r="10"/><path d="m7 12 3 3 7-7"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 10v7m0-11v1"/>',
    "play": '<path d="m7 3 14 9-14 9z"/>',
    "stop": '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 3"/>',
    "users": (
        '<circle cx="10" cy="7" r="4"/>'
        '<path d="M2 22v-4a8 8 0 0 1 16 0v4M17 3a4 4 0 0 1 0 8m3 3a7 7 0 0 1 2 8"/>'
    ),
    "roblox": (
        '<path fill="currentColor" fill-rule="evenodd" stroke="none"'
        ' d="m5 1 18 5-5 18L0 19zM9 9l-1.5 5L13 15.5l1.5-5z"/>'
    ),
    "instagram": (
        '<rect x="2" y="2" width="20" height="20" rx="6"/>'
        '<circle cx="12" cy="12" r="5"/><circle cx="18" cy="6" r=".8"/>'
    ),
    "x": '<path d="m3 3 18 18M21 3 3 21M3 3h5l13 18h-5z"/>',
    "snapchat": '<path d="M7 10V8a5 5 0 0 1 10 0v2l3 2-3 2 1 3 3 2-5 1-2 2h-4l-2-2-5-1 3-2 1-3-3-2z"/>',
}


@lru_cache(maxsize=96)
def icon(name, color="#2463f6", size=24):
    result = QIcon()
    for mode, ink in (
        (QIcon.Mode.Normal, color),
        (QIcon.Mode.Selected, "#ffffff"),
        (QIcon.Mode.Disabled, "#97a5bd"),
    ):
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
            f'fill="none" stroke="{ink}" color="{ink}" stroke-width="1.7" '
            f'stroke-linecap="round" stroke-linejoin="round">{PATHS[name]}</svg>'
        )
        pixmap = QPixmap(size * 2, size * 2)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        QSvgRenderer(svg.encode()).render(painter, QRectF(0, 0, size * 2, size * 2))
        painter.end()
        pixmap.setDevicePixelRatio(2)
        result.addPixmap(pixmap, mode)
    return result


def caption(text, name="muted"):
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(name != "statusPill")
    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute)
    label.setMinimumWidth(0)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class Card(QFrame):
    def __init__(self, title, symbol="layers", *, extra=None, shadow=False):
        super().__init__()
        self.setObjectName("surfaceCard")
        if shadow:
            effect = QGraphicsDropShadowEffect(self)
            effect.setBlurRadius(10)
            effect.setOffset(0, 2)
            effect.setColor(QColor(41, 78, 155, 18))
            self.setGraphicsEffect(effect)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(15, 13, 15, 13)
        self.body.setSpacing(11)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop)
        header = QHBoxLayout()
        image = QLabel()
        image.setPixmap(icon(symbol, size=21).pixmap(21, 21))
        header.addWidget(image)
        self.title = caption(title, "cardTitle")
        header.addWidget(self.title, 1)
        if extra:
            header.addWidget(extra)
        self.body.addLayout(header)


class PageHeading(QWidget):
    def __init__(self, title, subtitle, symbol):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        image = QLabel()
        image.setPixmap(icon(symbol, size=34).pixmap(34, 34))
        row.addWidget(image)
        text = QVBoxLayout()
        text.setSpacing(3)
        text.addWidget(caption(title, "pageTitle"))
        text.addWidget(caption(subtitle, "pageSubtitle"))
        row.addLayout(text, 1)


class ScrollBody(QScrollArea):
    """Let long forms scroll while their parent keeps stop/status controls visible."""

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

    def sizeHint(self):
        return QSize(0, 240)

    def minimumSizeHint(self):
        return QSize(0, 80)


class MetricCard(QFrame):
    def __init__(self, title, note, symbol):
        super().__init__()
        self.setObjectName("statCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 10, 13, 10)
        layout.setSpacing(3)
        header = QHBoxLayout()
        header.addWidget(caption(title, "metricTitle"), 1)
        image = QLabel()
        image.setPixmap(icon(symbol, size=23).pixmap(23, 23))
        header.addWidget(image)
        layout.addLayout(header)
        self.value = caption("0", "metricValue")
        layout.addWidget(self.value)
        layout.addWidget(caption(note, "micro"))


class ActionCard(QPushButton):
    def __init__(self, title, note, symbol, callback):
        super().__init__()
        self.setObjectName("actionCard")
        self.setAccessibleName(title)
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(4)
        image = QLabel()
        image.setPixmap(icon(symbol, size=26).pixmap(26, 26))
        layout.addWidget(image, alignment=Qt.AlignmentFlag.AlignCenter)
        for text, name in ((title, "actionTitle"), (note, "micro")):
            label = caption(text, name)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)
        for label in self.findChildren(QLabel):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.clicked.connect(callback)

    def sizeHint(self):
        return QSize(180, 112)

    def minimumSizeHint(self):
        return QSize(0, 112)


class CardGrid(QWidget):
    """Reflow the same widgets without resetting their inputs or connections."""

    def __init__(self, widgets, *, columns=(4, 2, 1), minimum=160):
        super().__init__()
        self.setObjectName("cardGrid")
        self.widgets, self.columns, self.minimum = widgets, columns, minimum
        self.current_columns = 0
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(12)
        self.reflow()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()

    def reflow(self):
        count = next((c for c in self.columns if self.width() >= c * self.minimum + (c - 1) * 12), 1)
        if count == self.current_columns:
            return
        for column in range(max(self.columns)):
            self.grid.setColumnStretch(column, int(column < count))
        for index, widget in enumerate(self.widgets):
            self.grid.removeWidget(widget)
            self.grid.addWidget(widget, index // count, index % count, Qt.AlignmentFlag.AlignTop)
        self.current_columns = count

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())


class ModeStrip(QTabBar):
    """Presentation of an existing combo, including its enabled-state safety gate."""

    def __init__(self, combo, labels):
        super().__init__()
        self.combo = combo
        self.setObjectName("modeStrip")
        self.setExpanding(True)
        self.setDrawBase(False)
        for title, symbol in labels:
            self.addTab(icon(symbol, size=20), title)
        self.setCurrentIndex(combo.currentIndex())
        self.setEnabled(combo.isEnabled())
        combo.installEventFilter(self)
        combo.currentIndexChanged.connect(self.setCurrentIndex)
        self.currentChanged.connect(combo.setCurrentIndex)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.EnabledChange:
            self.setEnabled(self.combo.isEnabled())
        return super().eventFilter(watched, event)


class Disclosure(QWidget):
    """Collapse presentation only; child values and enabled states remain untouched."""

    def __init__(self, title):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        self.toggle = QPushButton(title + "  ▾")
        self.toggle.setCheckable(True)
        root.addWidget(self.toggle)
        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(8, 6, 8, 6)
        self.body.setSpacing(8)
        root.addWidget(self.content)
        self.content.hide()
        self.toggle.toggled.connect(self.content.setVisible)

    def setExpanded(self, expanded):
        self.toggle.setChecked(expanded)
