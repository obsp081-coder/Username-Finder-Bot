"""Isolated, explicitly supervised Snapchat Web desktop steps using Qt timers."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .snapscore_desktop import DesktopError, Target, WindowsDesktop

STEPS = ("فتح الكاميرا في المحادثة", "التقاط اللقطة", "إرسال اللقطة إلى المحادثة المفتوحة")


@dataclass(frozen=True)
class Plan:
    target: Target
    points: tuple[tuple[int, int], ...]
    limit: int
    delay: int

    def __post_init__(self):
        if type(self.limit) is not int or not 1 <= self.limit <= 3:
            raise ValueError("حد الجلسة من نقرة واحدة إلى ثلاث نقرات فقط، دون تكرار آلي.")
        if type(self.delay) is not int or not 3 <= self.delay <= 30:
            raise ValueError("التأخير من 3 إلى 30 ثانية لكل نقرة.")
        if (
            len(self.points) != 3
            or len(set(self.points)) != 3
            or any(not self.target.contains(point) for point in self.points)
        ):
            raise ValueError("عاير ثلاثة مواضع مختلفة داخل نافذة Snapchat Web نفسها.")


class SnapscoreSession(QObject):
    changed = Signal()

    def __init__(self, desktop, parent=None, *, clock=time.monotonic):
        super().__init__(parent)
        self.desktop, self.clock = desktop, clock
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        self.state = "idle"
        self.message = "متوقف — لم تُنفذ أي نقرة."
        self.attempts = self.errors = self.token = 0
        self.started = self.due = self.expires = self.elapsed = 0.0
        self.plan = None

    @property
    def active(self):
        return self.state in ("countdown", "awaiting")

    def start(self, plan, *, confirmed=False):
        if confirmed is not True or self.active or not isinstance(plan, Plan):
            return False
        self.plan = plan
        self.attempts = self.errors = 0
        self.elapsed = 0.0
        self.started = self.clock()
        self.expires = self.started + 300
        self.token += 1
        try:
            self.desktop.arm_stop(self.stop)
        except Exception as exc:
            self._error(exc)
            return False
        self._countdown()
        return True

    def _countdown(self):
        self.state = "countdown"
        self.due = self.clock() + self.plan.delay
        self.timer.start()
        self.tick()

    def advance(self, *, confirmed=False):
        if confirmed is not True or self.state != "awaiting" or self.attempts >= self.plan.limit:
            return False
        self.token += 1
        self._countdown()
        return True

    def tick(self):
        if not self.active:
            return
        now = self.clock()
        self.elapsed = max(0.0, now - self.started)
        if now >= self.expires or (self.state == "awaiting" and now >= self.due):
            self.stop("انتهت مهلة الجلسة أو انتظار التأكيد؛ أُوقفت دون نقرات إضافية.")
            return
        if self.state == "countdown":
            step = self.attempts
            remaining = max(0, math.ceil(self.due - now))
            self.message = f"جارٍ التشغيل — {STEPS[step]} بعد {remaining} ثانية. أظهر نافذة الهدف. F9 للإيقاف."
            if now >= self.due:
                try:
                    self.desktop.validate(self.plan.target, self.plan.points[step])
                    self.attempts += 1  # Attempted desktop input, never verified sends or score changes.
                    self.desktop.click(self.plan.target, self.plan.points[step])
                except Exception as exc:
                    self._error(exc)
                    return
                if self.attempts == self.plan.limit:
                    self._finish(
                        "complete",
                        "اكتمل حد الجلسة. النقرات محاولات فقط؛ لم يُتحقق من الإرسال أو زيادة السكور.",
                    )
                    return
                self.state = "awaiting"
                self.due = now + 60
                self.token += 1
                self.message = "بانتظارك — راجع Snapchat ثم أكّد الخطوة التالية. لا توجد متابعة تلقائية."
        self.changed.emit()

    def _error(self, exc):
        self.errors += 1
        message = str(exc) if isinstance(exc, DesktopError) else "تعذر التفاعل مع سطح المكتب؛ أُوقفت الجلسة."
        self._finish("error", message)

    def _finish(self, state, message):
        self.timer.stop()
        self.state, self.message = state, message
        self.elapsed = max(0.0, self.clock() - self.started)
        self.token += 1
        try:
            self.desktop.disarm_stop()
        except Exception:
            self.state = "error"
            self.errors += 1
            self.message = "تعذر تحرير مفتاح الإيقاف؛ أُوقف التنفيذ. أغلق التطبيق لتحرير موارده."
        self.changed.emit()

    def stop(self, message="أُوقفت الجلسة؛ لن تُنفذ نقرات إضافية."):
        if self.active:
            self._finish("stopped", message)


def label(text):
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def number(low, high, value):
    widget = QSpinBox()
    widget.setRange(low, high)
    widget.setValue(value)
    widget.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    return widget


class SnapscorePanel(QScrollArea):
    def __init__(self, parent=None, *, desktop=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.desktop = desktop if desktop is not None else WindowsDesktop()
        self.session = SnapscoreSession(self.desktop, self)
        self.session.changed.connect(self.refresh)
        self.target = self.reviewed = None
        self.calibrated = set()
        self.calibrating = None
        self.capture_due = 0.0
        self.closing = False
        self.calibration_timer = QTimer(self)
        self.calibration_timer.setInterval(50)
        self.calibration_timer.timeout.connect(self._capture_tick)
        body = QWidget()
        layout = QVBoxLayout(body)
        title = label("سناب سكور — خطوات مكتبية مراقبة")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(
            label(
                "1. افتح web.snapchat.com بنفسك وسجّل الدخول في المتصفح؛ لا تُدخل بيانات الدخول هنا.\n"
                "2. افتح محادثة واحدة مع شخص موافق، وجهّز الكاميرا يدويًا. "
                "لا تختَر مجموعات أو اختصارات جماعية.\n"
                "3. لكل موضع اضغط «معايرة»، ثم خلال 5 ثوانٍ أظهر المتصفح وضع المؤشر وسط الزر دون النقر. "
                "يمكنك الانتقال يدويًا بين شاشات الكاميرا لمعايرة الأزرار.\n"
                "4. راجع المواضع، وأعد Snapchat إلى بداية الخطوات. "
                "كل نقرة تتطلب تأكيدًا منفصلًا ثم مهلة للعودة للمتصفح. "
                "F9 يوقف الجلسة من أي نافذة."
            )
        )
        warning = label(
            "لا تُقاس زيادة Snapscore هنا. عدّاد المحاولات لا يثبت إرسال سناب أو استلامه. "
            "لا يُقرأ عنوان الموقع أو المستلم آليًا؛ راجعهما بنفسك قبل كل تأكيد. "
            "عنوان نافذة المتصفح وحده ليس إثباتًا لهوية الموقع. "
            "أعد المعايرة بعد تغيير التكبير أو تخطيط الصفحة."
        )
        warning.setObjectName("simulation")
        layout.addWidget(warning)
        group = QGroupBox("المعايرة — إحداثيات فعلية على الشاشة، دون نقر")
        form = QFormLayout(group)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.positions, self.capture_buttons = [], []
        for index, name in enumerate(STEPS):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            x, y = number(-100000, 100000, 0), number(-100000, 100000, 0)
            x.setAccessibleName(name + " — X")
            y.setAccessibleName(name + " — Y")
            capture = QPushButton("معايرة (5 ثوانٍ)")
            capture.clicked.connect(lambda _=False, i=index: self.calibrate(i))
            for name_label, spin in (("X", x), ("Y", y)):
                row_layout.addWidget(label(name_label))
                row_layout.addWidget(spin, 1)
                spin.valueChanged.connect(self.invalidate)
            row_layout.addWidget(capture)
            form.addRow(name, row)
            self.positions.append((x, y))
            self.capture_buttons.append(capture)
        self.target_label = label("لم تُعاير نافذة الهدف. تُحفظ المواضع في الذاكرة لهذه النافذة فقط.")
        form.addRow(self.target_label)
        layout.addWidget(group)
        options = QFormLayout()
        self.limit = number(1, 3, 3)
        self.delay = number(3, 30, 5)
        options.addRow("حد النقرات للجلسة (دون تكرار)", self.limit)
        options.addRow("التأخير قبل كل نقرة — ثوانٍ", self.delay)
        self.limit.valueChanged.connect(self.invalidate)
        self.delay.valueChanged.connect(self.invalidate)
        layout.addLayout(options)
        self.plan_text = label("المعاينة تعرض خطة المواضع فقط؛ لا تحرك المؤشر ولا تنفذ نقرات.")
        layout.addWidget(self.plan_text)
        controls = QHBoxLayout()
        self.preview = QPushButton("معاينة المواضع دون نقر")
        self.start = QPushButton("بدء")
        self.start.setObjectName("primary")
        self.next = QPushButton("تأكيد الخطوة التالية")
        self.stop = QPushButton("إيقاف — F9")
        self.stop.setStyleSheet("QPushButton { border: 2px solid #b34040; color: #8a2525; }")
        self.preview.clicked.connect(self.preview_plan)
        self.start.clicked.connect(self.start_session)
        self.next.clicked.connect(self.next_step)
        self.stop.clicked.connect(self.stop_work)
        for control in (self.preview, self.start, self.next, self.stop):
            controls.addWidget(control)
        layout.addLayout(controls)
        self.status = label("")
        self.status.setAccessibleName("حالة أداة سناب سكور")
        layout.addWidget(self.status)
        self.counters = label("")
        layout.addWidget(self.counters)
        layout.addStretch()
        self.setWidget(body)
        self.refresh()

    def invalidate(self, *_):
        self.reviewed = None
        self.stop_work()
        self.plan_text.setText("تغيّرت الإعدادات؛ راجع المعاينة مجددًا قبل البدء.")
        self.refresh()

    def plan(self):
        if self.target is None or len(self.calibrated) != 3:
            raise ValueError("عاير المواضع الثلاثة في نافذة Snapchat Web أولًا.")
        points = tuple((x.value(), y.value()) for x, y in self.positions)
        return Plan(self.target, points, self.limit.value(), self.delay.value())

    def calibrate(self, index):
        if self.closing or self.session.active or self.calibrating is not None:
            return
        self.reviewed = None
        self.calibrating = index
        self.capture_due = time.monotonic() + 5
        self.calibration_timer.start()
        self._capture_tick()

    def _capture_tick(self):
        if self.calibrating is None:
            return
        remaining = math.ceil(self.capture_due - time.monotonic())
        if remaining > 0:
            self.status.setText(
                f"معايرة {STEPS[self.calibrating]} بعد {remaining} ثانية — ضع المؤشر دون النقر."
            )
            self.refresh(update_status=False)
            return
        index = self.calibrating
        self.calibrating = None
        self.calibration_timer.stop()
        try:
            target, point = self.desktop.capture()
            if any(not -100000 <= coordinate <= 100000 for coordinate in point):
                raise DesktopError("point")
            if self.target is not None and self.target != target:
                self.calibrated.clear()
            self.target = target
            x, y = self.positions[index]
            x.setValue(point[0])
            y.setValue(point[1])
            self.calibrated.add(index)
            self.target_label.setText(
                f"الهدف: \u2066{target.browser}\u2069 — DPI: {target.dpi} — "
                f"المواضع المعايرة: {len(self.calibrated)}/3. لا يُتحقق من الموقع أو المستلم تلقائيًا."
            )
            self.session.message = "حُفظ الموضع دون تنفيذ نقرة. أكمل المعايرة ثم اعرض الخطة."
        except Exception as exc:
            self.session.errors += 1
            self.session.message = (
                str(exc) if isinstance(exc, DesktopError) else "تعذرت المعايرة؛ لم تُنفذ نقرة."
            )
        self.refresh()

    def preview_plan(self):
        if self.session.active or self.calibrating is not None or self.closing:
            return
        try:
            plan = self.plan()
        except ValueError as exc:
            self.session.message = str(exc)
            self.refresh()
            return
        self.reviewed = plan
        self.plan_text.setText(
            "معاينة فقط — لا توجد نقرات:\n"
            + "\n".join(
                f"{i + 1}. {STEPS[i]}: \u2066X={point[0]}, Y={point[1]}\u2069"
                for i, point in enumerate(plan.points[: plan.limit])
            )
        )
        self.session.message = (
            "المواضع جاهزة للمراجعة. البدء يتطلب تأكيد النقرة الأولى؛ لا توجد متابعة تلقائية."
        )
        self.refresh()

    def confirm(self, step):
        return (
            QMessageBox.question(
                self,
                "تأكيد نقرة فعلية في Snapchat Web",
                f"الخطوة: {STEPS[step]}\n"
                "راجعت بنفسي web.snapchat.com والمحادثة الواحدة والمحتوى والموضع؟\n"
                f"ستُحاول الأداة نقرة واحدة بعد {self.delay.value()} ثانية. "
                "أظهر نافذة Snapchat المعايرة، ولا تغيّر تخطيطها. F9 للإيقاف.\n"
                "لن تُؤكد الأداة نجاح الإرسال أو زيادة السكور.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def start_session(self):
        if self.closing or self.session.active or self.calibrating is not None:
            return
        try:
            plan = self.plan()
        except ValueError:
            return
        if self.reviewed != plan:
            return
        token = self.session.token
        if self.confirm(0) and not self.closing and token == self.session.token and self.reviewed == plan:
            self.session.start(plan, confirmed=True)
            QTimer.singleShot(0, self, lambda: self.ensureWidgetVisible(self.status, 0, 15))

    def next_step(self):
        if self.closing or self.session.state != "awaiting":
            return
        token = self.session.token
        if self.confirm(self.session.attempts) and not self.closing and token == self.session.token:
            self.session.advance(confirmed=True)
            QTimer.singleShot(0, self, lambda: self.ensureWidgetVisible(self.status, 0, 15))

    def stop_work(self):
        if self.calibrating is not None:
            self.calibrating = None
            self.calibration_timer.stop()
            self.session.message = "أُلغيت المعايرة دون نقر."
        self.session.stop()
        self.refresh()

    def refresh(self, *, update_status=True):
        busy = self.session.active or self.calibrating is not None
        usable = not busy and not self.closing
        for x, y in self.positions:
            x.setEnabled(usable)
            y.setEnabled(usable)
        for button in self.capture_buttons:
            button.setEnabled(usable and self.desktop.available)
        self.limit.setEnabled(usable)
        self.delay.setEnabled(usable)
        self.preview.setEnabled(usable)
        self.start.setEnabled(usable and self.reviewed is not None and self.desktop.available)
        self.next.setEnabled(self.session.state == "awaiting" and not self.closing)
        self.stop.setEnabled(busy and not self.closing)
        if update_status:
            self.status.setText(self.session.message)
        self.status.setStyleSheet(
            "background: #fff0d4; color: #674600; padding: 10px; border: 2px solid #d29b32;"
            if busy
            else "background: #e8eef5; color: #203047; padding: 10px;"
        )
        self.counters.setText(
            f"النقرات المحاوَلة: {self.session.attempts}/{self.limit.value()} | "
            f"الزمن: {self.session.elapsed:.1f} ثانية | الأخطاء: {self.session.errors}\n"
            "الإرسال وزيادة Snapscore: غير متحقق منهما."
        )

    def hideEvent(self, event):
        self.stop_work()
        super().hideEvent(event)

    def shutdown(self):
        self.closing = True
        self.stop_work()
