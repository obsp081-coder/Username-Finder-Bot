"""Multiline secret editing with concealed painting by default; no credential persistence."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QPlainTextEdit


class PrivateProxyEdit(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.revealed = False
        self.setMaximumHeight(85)
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.setAcceptDrops(False)
        self.setAccessibleName("إدخال proxies سري متعدد الأسطر")
        self.textChanged.connect(self.viewport().update)

    def reveal(self, checked):
        self.revealed = checked
        self.viewport().update()

    def paintEvent(self, event):
        if self.revealed:
            super().paintEvent(event)
            return
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), self.palette().base())
        painter.setPen(self.palette().text().color())
        painter.drawText(
            self.viewport().rect().adjusted(8, 4, -8, -4),
            Qt.AlignmentFlag.AlignTop,
            "•••••• — محتوى مخفي؛ يمكن اللصق والتحرير. فعّل الإظهار للمراجعة.",
        )

    def contextMenuEvent(self, event):
        # Avoid accidental clipboard disclosure through a context-menu copy action.
        event.ignore()
