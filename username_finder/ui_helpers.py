from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QScrollArea,
    QTabBar,
    QWidget,
)

REASONS = {
    "explicitly_available": "إشارة قبول صريحة",
    "explicitly_taken": "إشارة استخدام صريحة",
    "explicitly_reserved": "إشارة حجز صريحة",
    "explicitly_invalid": "رفض صيغة الاسم",
    "syntax_invalid": "مخالفة قواعد اسم المستخدم",
    "simulated_ambiguous": "نتيجة افتراضية غير محسومة",
    "network_error": "تعذر الحصول على جواب",
    "account_exists": "رُصد حساب مطابق",
}


def local_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        formatted = (
            datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Riyadh")).strftime("%Y/%m/%d  %H:%M")
        )
        return "\u2066" + formatted + "\u2069"
    except (ValueError, TypeError):
        return "وقت غير معروف"


class WheelGuard(QObject):
    """Scroll the nearest movable ancestor, without synthesizing/redispatching wheel events."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.Wheel:
            return False
        if not isinstance(watched, QWidget):
            return False
        # Includes owned dialogs, but leaves other application windows/pop-up list views alone.
        owner = watched.window()
        if owner is not self.window:
            if not isinstance(owner, QDialog):
                return False
            ancestor = owner.parentWidget()
            while ancestor is not None and ancestor is not self.window:
                ancestor = ancestor.parentWidget()
            if ancestor is None:
                return False
        control = watched
        while control is not None and not isinstance(
            control, (QComboBox, QAbstractSpinBox, QTabBar, QAbstractScrollArea)
        ):
            control = control.parentWidget()
        viewport = watched.parentWidget()
        is_page_viewport = isinstance(viewport, QScrollArea) and watched is viewport.viewport()
        if not isinstance(control, (QComboBox, QAbstractSpinBox, QTabBar)) and not is_page_viewport:
            return False
        horizontal = (event.angleDelta().y() == 0 and event.pixelDelta().y() == 0) or bool(
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        )
        pixel = event.pixelDelta().x() if horizontal else event.pixelDelta().y()
        angle = event.angleDelta().x() if horizontal else event.angleDelta().y()
        if horizontal and not angle:
            angle = event.angleDelta().y()
        parent = watched.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                bar = parent.horizontalScrollBar() if horizontal else parent.verticalScrollBar()
                delta = pixel or angle / 120 * QApplication.styleHints().wheelScrollLines() * bar.singleStep()
                if delta and (
                    (delta > 0 and bar.value() > bar.minimum()) or (delta < 0 and bar.value() < bar.maximum())
                ):
                    movement = round(delta) or (1 if delta > 0 else -1)
                    bar.setValue(bar.value() - movement)
                    break
            parent = parent.parentWidget()
        event.accept()
        return True


def style_combo_popup(combo: QComboBox):
    """Override native dark popup brushes while retaining Qt's compatible list control."""
    view = combo.view()
    palette = view.palette()
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        disabled = group == QPalette.ColorGroup.Disabled
        for role in (QPalette.ColorRole.Base, QPalette.ColorRole.Window):
            palette.setColor(group, role, QColor("#ffffff"))
        for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText):
            palette.setColor(group, role, QColor("#66758a" if disabled else "#203047"))
        palette.setColor(group, QPalette.ColorRole.Highlight, QColor("#e6ebf2" if disabled else "#2463a6"))
        palette.setColor(
            group, QPalette.ColorRole.HighlightedText, QColor("#66758a" if disabled else "#ffffff")
        )
    view.setPalette(palette)
    view.setLayoutDirection(Qt.LayoutDirection.RightToLeft)


def status_text(status: str | None, mode: str) -> str:
    names = {
        "Available": "متاح",
        "Taken": "مستخدم",
        "Reserved": "محجوز",
        "Invalid": "غير صالح",
        "Unknown": "غير محسوم",
    }
    if not status:
        return "—"
    value = names.get(status, "غير معروف")
    return value + " · محاكاة" if mode == "simulated" else value
