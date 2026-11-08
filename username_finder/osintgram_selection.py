"""Readable Qt command checkboxes; inspection and execution remain separate."""

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeWidget


class CommandDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if not opt.features & QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator:
            return
        rect = opt.widget.style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemCheckIndicator, opt, opt.widget
        )
        if rect.isEmpty():
            return
        enabled = bool(opt.state & QStyle.StateFlag.State_Enabled) and bool(
            index.flags() & Qt.ItemFlag.ItemIsUserCheckable
        )
        checked = opt.checkState == Qt.CheckState.Checked
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#365d85" if enabled else "#9daaba"), 1.5))
        painter.setBrush(QColor("#2463a6" if checked and enabled else "#ffffff" if enabled else "#e6ebf2"))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
        if checked:
            painter.setPen(QPen(QColor("#ffffff" if enabled else "#66758a"), 2.2))
            path = QPainterPath()
            path.moveTo(rect.left() + rect.width() * 0.22, rect.top() + rect.height() * 0.51)
            path.lineTo(rect.left() + rect.width() * 0.43, rect.top() + rect.height() * 0.72)
            path.lineTo(rect.left() + rect.width() * 0.79, rect.top() + rect.height() * 0.28)
            painter.drawPath(path)
        if opt.state & QStyle.StateFlag.State_HasFocus:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#365d85"), 1, Qt.PenStyle.DashLine))
            painter.drawRect(opt.rect.adjusted(2, 2, -3, -3))
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
        ):
            return False  # The tree toggles the readable row once; Qt still handles Space.
        return super().editorEvent(event, model, option, index)


class CommandTree(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("osintCommands")
        self.setItemDelegate(CommandDelegate(self))
        self.setMouseTracking(True)
        self.setStyleSheet("""
            QTreeWidget#osintCommands { background: white; border: 1px solid #c3cedd;
                border-radius: 5px; outline: none; }
            QTreeWidget#osintCommands::item { padding: 5px 2px; }
            QTreeWidget#osintCommands::item:hover { background: #edf3fa; }
            QTreeWidget#osintCommands::item:selected { background: #dceafa; color: #163b67; }
            QTreeWidget#osintCommands::item:disabled { color: #66758a; }
            QTreeWidget#osintCommands::indicator { width: 20px; height: 20px; }
        """)
        self._pressed_item = None

    def mousePressEvent(self, event):
        self._pressed_item = self.itemAt(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        super().mouseReleaseEvent(event)
        if (
            self.isEnabled()
            and event.button() == Qt.MouseButton.LeftButton
            and item is not None
            and item is self._pressed_item
            and item.flags() & Qt.ItemFlag.ItemIsEnabled
            and item.flags() & Qt.ItemFlag.ItemIsUserCheckable
        ):
            item.setCheckState(
                0,
                Qt.CheckState.Unchecked
                if item.checkState(0) == Qt.CheckState.Checked
                else Qt.CheckState.Checked,
            )
        self._pressed_item = None
