"""Arabic large-file prefilter, with an explicit pilot and bounded previews."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .roblox_prefilter import Prefilter

STATE_LABELS = {
    "Ready": "جاهزة",
    "Running": "قيد التشغيل",
    "Paused": "متوقفة قابلة للاستئناف",
    "Completed": "مكتملة",
    "Restricted": "متوقفة وقائيًا",
    "ReviewRequired": "تحتاج مراجعة",
}


class PrefilterPanel(QWidget):
    progress_changed = Signal(object)
    finished = Signal(object, str)
    idle = Signal()

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.busy = self.closing = False
        self.service = None
        self.snapshot = None
        self.last_update = 0.0
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="roblox-prefilter")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "تصفية ملفات كبيرة على القرص، دون تحميلها كاملة أو تغيير سجل Roblox. "
            "نستبعد تطابق الحساب الحالي من أولوية الفردي فقط؛ لا يعني Taken التسجيل. "
            "الاسم السابق أو السجل المفقود أو المجهول لا يعني Available.\n"
            "ابدأ بعينة محدودة من الملف، وراجع تقريرها قبل تفويض الباقي. "
            "لا يبدأ الفحص الفردي تلقائيًا؛ صدّر ملف مرشحيه واستورده في الوضع الفردي."
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("simulation")
        heading = QLabel("تصفية حسابات فقط · لا تثبت إتاحة التسجيل")
        heading.setWordWrap(True)
        heading.setObjectName("heading")
        layout.addWidget(heading)
        root_layout = layout
        self.steps_area = QScrollArea()
        self.steps_area.setWidgetResizable(True)
        self.steps_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.steps_area.setMinimumHeight(90)
        steps_body = QWidget()
        layout = QVBoxLayout(steps_body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(explanation)
        self.steps_area.setWidget(steps_body)
        root_layout.addWidget(self.steps_area, 1)
        self.steps = {}
        for key, title in (
            ("import", "1 · اختيار الملف واستعادته"),
            ("config", "2 · إعداد الدفعات والتوقيت"),
            ("prepare", "3 · تحضير محلي دون اتصال"),
            ("pilot", "4 · تجربة محدودة بتفويض صريح"),
            ("review", "5 · مراجعة الدليل والاستعادة"),
            ("full", "6 · تفويض مستقل لبقية الملف"),
            ("export", "7 · تصدير الفئات ومرشحي الفردي"),
        ):
            card = QGroupBox(title)
            self.steps[key] = QVBoxLayout(card)
            layout.addWidget(card)
        self.source = QLineEdit()
        self.source.setReadOnly(True)
        self.source.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.steps["import"].addWidget(self.source)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.delay = QDoubleSpinBox()
        self.delay.setDecimals(3)
        self.delay.setRange(0.001, 86400)
        self.delay.setValue(10)
        self.batch = QSpinBox()
        self.batch.setRange(1, 20)
        self.batch.setValue(20)
        self.pilot_size = QSpinBox()
        self.pilot_size.setRange(1, 200)
        self.pilot_size.setValue(100)
        form.addRow("الفاصل بعد الرد (ثانية)", self.delay)
        form.addRow("الأسماء في كل طلب Bulk", self.batch)
        form.addRow("حد التجربة الأولية: أسماء صالحة فريدة", self.pilot_size)
        self.steps["config"].addLayout(form)
        self.warning = QLabel("أقل من 10 ثوانٍ تجريبي؛ لم يُثبت استقراره للمسح الطويل.")
        self.warning.setObjectName("simulation")
        self.warning.setWordWrap(True)
        self.delay.valueChanged.connect(lambda v: self.warning.setVisible(v < 10))
        self.warning.hide()
        self.steps["config"].addWidget(self.warning)
        self.buttons = {}
        for name, label, callback in (
            ("file", "اختيار TXT / CSV…", self.choose_file),
            ("open", "فتح عملية محفوظة…", self.open_run),
            ("prepare", "استيراد ومعاينة محلية", self.prepare),
            ("inspect", "مراجعة الحالة والدليل", self.inspect),
            ("pilot", "تفويض / استئناف التجربة المحدودة", lambda: self.start("pilot")),
            ("full", "تفويض / استئناف الباقي", lambda: self.start("full")),
            ("stop", "إيقاف وحفظ التقدم", self.stop),
            ("recover", "استعادة محلية دون إرسال", self.recover),
            ("review", "مراجعة تسليم غير محسوم", self.review_uncertain),
            ("export", "تصدير الفئات ومرشحي الفردي…", self.export),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            step = {
                "file": "import",
                "open": "import",
                "prepare": "prepare",
                "pilot": "pilot",
                "inspect": "review",
                "recover": "review",
                "review": "review",
                "full": "full",
                "export": "export",
            }.get(name)
            if step:
                self.steps[step].addWidget(button)
            else:
                root_layout.addWidget(button)
            self.buttons[name] = button
        layout = root_layout
        self.status = QLabel("اختر ملفًا؛ لا طلبات تلقائية.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        layout.addWidget(self.progress)
        self.counts = QLabel()
        self.counts.setWordWrap(True)
        self.counts.setTextFormat(Qt.TextFormat.PlainText)
        self.steps["review"].addWidget(self.counts)
        for label in (explanation, self.status, self.counts, self.warning):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setMinimumWidth(0)
        self.progress_changed.connect(self.show_progress)
        self.finished.connect(self.done)
        self.controls()

    def controls(self):
        for key, button in self.buttons.items():
            button.setEnabled(
                not self.busy and not self.closing and (key in ("file", "open") or self.service is not None)
            )
        self.buttons["prepare"].setEnabled(not self.busy and not self.closing and bool(self.source.text()))
        self.buttons["stop"].setEnabled(self.busy)
        for widget in (self.delay, self.batch, self.pilot_size):
            widget.setEnabled(not self.busy and self.service is None and not self.closing)
        self.owner.checking_mode.setEnabled(not self.owner.busy and not self.closing)
        if self.snapshot:
            for phase in ("pilot", "full"):
                eligible = self.snapshot["import_state"] == "Ready"
                if phase == "full":
                    eligible &= self.snapshot["phases"].get("pilot", {}).get("state") == "Completed"
                state = self.snapshot["phases"].get(phase, {}).get("state", "Ready")
                eligible &= state in ("Ready", "Paused") and self.snapshot["unprocessed"] > 0
                self.buttons[phase].setEnabled(bool(eligible and not self.busy and not self.closing))

    def new_service(self, directory):
        return Prefilter(
            directory, self.owner.repo.store.path, control=self.owner.repo.control, updated=self.updated
        )

    def updated(self):
        now = time.monotonic()
        if now - self.last_update >= 0.2:
            self.last_update = now
            self.progress_changed.emit(self.service.summary())

    def submit(self, function):
        if self.busy or self.owner.busy or self.closing:
            return
        self.busy = True
        self.service.cancel.clear()
        self.controls()
        self.status.setText("جارٍ العمل؛ الإيقاف متاح، والنتائج تُحفظ تدريجيًا.")
        future = self.executor.submit(function)

        def finished(future):
            try:
                value, error = future.result(), ""
            except BaseException as exc:
                value, error = None, str(exc)
            self.finished.emit(value, error)

        future.add_done_callback(finished)

    def done(self, result, error):
        self.busy = False
        if isinstance(result, dict):
            self.show_progress(result)
        if error:
            self.status.setText(error)
        elif isinstance(result, Path):
            self.status.setText(
                "مجلد التصدير: "
                + str(result)
                + " — افحص export-state.json لاكتماله؛ مرشحو الفردي في individual-candidates.txt"
            )
        if self.closing:
            self.executor.shutdown(wait=False)
        self.controls()
        self.idle.emit()

    def show_progress(self, data):
        self.snapshot = data
        c, phases = data["counts"], data["phases"]
        self.progress.setValue(int(1000 * c["processed"] / c["valid"]) if c["valid"] else 0)
        elapsed = sum(p["elapsed_seconds"] for p in phases.values())
        time_note = "حد أدنى " if any(p.get("elapsed_incomplete") for p in phases.values()) else ""
        rate = data["quality"]["full" if "full" in phases else "pilot"]["names_per_minute"]
        rate_note = f"{rate:.2f} اسم/دقيقة" if rate is not None else "غير محسوم"
        self.counts.setText(
            f"مقروءة: {c['imported']:,} • فريدة: {c['unique']:,} • مكررة: {c['duplicates']:,} • "
            f"غير صالحة: {c['invalid']:,}\n"
            f"عولجت: {c['processed']:,} • تطابق حساب مستبعد: {c['excluded']:,} • "
            f"مرشحو الفردي: {data['remaining_candidates']:,}\n"
            f"مجهولة: {c['unknown']:,} • غير آمنة: {c['unsafe']:,} • طلبات فاشلة: {c['failed_requests']:,} • "
            f"ردود مكتملة: {c['completed_requests']:,}/{c['attempts']:,}\n"
            f"زمن حلقات الفحص: {time_note}{elapsed:.1f}ث • المعدل المرصود: {rate_note}\n"
            f"ميزانية الخطة: {data['planned_requests']:,} طلبًا • "
            f"حد الانتظار الأدنى: {data['estimated_pacing_seconds'] / 3600:.2f} ساعة، دون وقت الردود\n"
            + " • ".join(
                f"{'التجربة' if name == 'pilot' else 'الباقي'}: {STATE_LABELS[p['state']]} "
                f"({p['processed']}/{p['target']}) — {p['stop_reason']}"
                for name, p in phases.items()
            )
        )
        if not self.busy:
            self.status.setText(
                data.get("import_error", "الاستيراد: " + data["import_state"])
                + " • المجلد: "
                + str(self.service.directory)
            )
        self.controls()

    def choose_file(self):
        if self.owner.busy or self.closing:
            return
        path, _ = QFileDialog.getOpenFileName(self, "ملف أسماء كبير — UTF-8", "", "Usernames (*.txt *.csv)")
        if path:
            self.service = self.snapshot = None
            self.counts.clear()
            self.progress.setValue(0)
            self.status.setText("اختير الملف؛ اضبط إعدادات عملية جديدة ثم عاين محليًا.")
            self.source.setText(path)
            self.controls()

    def prepare(self):
        if self.owner.busy or self.closing:
            return
        if self.service is None:
            self.service = self.new_service(
                self.owner.repo.store.path.parent / "roblox-prefilter" / uuid4().hex
            )
        path, delay, batch, pilot = (
            self.source.text(),
            self.delay.value(),
            self.batch.value(),
            self.pilot_size.value(),
        )
        self.submit(lambda: self.service.prepare(path, delay=delay, batch_size=batch, pilot_names=pilot))

    def open_run(self):
        if self.owner.busy or self.closing:
            return
        path = QFileDialog.getExistingDirectory(
            self, "مجلد عملية التصفية المحفوظة", str(self.owner.repo.store.path.parent / "roblox-prefilter")
        )
        if path:
            try:
                self.service = self.new_service(Path(path))
                value = self.service.manifest()
                self.source.setText(value["source"]["files"][0]["path"])
                self.delay.setValue(value["settings"]["delay"])
                self.batch.setValue(value["settings"]["batch_size"])
                self.pilot_size.setValue(value["settings"]["pilot_names"])
                self.submit(self.service.recover)
            except Exception as exc:
                self.status.setText(str(exc))

    def confirm(self, text):
        return (
            QMessageBox.question(
                self,
                "تفويض تصفية Roblox",
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def start(self, phase):
        if self.owner.busy or self.closing or self.service is None:
            return
        try:
            token = self.service.review(phase)
            data = self.service.summary()
            saved = data["phases"].get(phase)
            count = saved["target"] - saved["processed"] if saved else data["unprocessed"]
            config = data["manifest"]["settings"]
            if self.confirm(
                f"راجع المعاينة وتقرير التجربة قبل الباقي. تفويض {phase} فقط: حتى {count:,} اسم، "
                f"{config['batch_size']} لكل طلب، بفاصل {config['delay']:g} ثانية بعد الرد.\n"
                "الإعدادات محفوظة لهذه العملية. أوقف أي فاحص Roblox خارجي. "
                "لا إتاحة تسجيل من Bulk ولا تشغيل فردي تلقائي. هل تفوّض هذا الطور فقط؟"
            ):
                self.submit(lambda: self.service.run(phase, token))
        except Exception as exc:
            self.status.setText(str(exc))

    def recover(self):
        if self.service:
            self.submit(self.service.recover)

    def review_uncertain(self):
        if self.service and self.confirm(
            "راجع الدليل أولًا. ستظل أسماء الطلب غير المحسوم Unknown ولن تُرسل مجددًا. "
            "هذا إقرار بالمراجعة فقط ولا يمسح حظر الموفر. هل تؤكد؟"
        ):

            def work():
                self.service.review_uncertain(self.service.manifest()["fingerprint"])
                return self.service.summary()

            self.submit(work)

    def inspect(self):
        if not self.snapshot:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("ملخص محدود ومعاينة أول 30 اسمًا — ليس كامل النتائج")
        dialog.resize(780, 530)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        text.setPlainText(json.dumps(self.snapshot, ensure_ascii=False, indent=2))
        layout.addWidget(text)
        close = QPushButton("إغلاق")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def export(self):
        path = QFileDialog.getExistingDirectory(self, "اختر مجلدًا للتصدير — سيُنشأ مجلد جديد داخله")
        if path:
            destination = Path(path)
            if (destination / "export-state.json").exists():
                if not self.confirm("هذا تصدير محفوظ؛ هل تستكمل ملفاته غير المكتملة لنفس لقطة النتائج؟"):
                    return
            else:
                destination = destination / ("roblox-prefilter-" + uuid4().hex)
            self.submit(lambda: self.service.export(destination))

    def stop(self):
        if self.service:
            self.service.cancel.set()
            self.status.setText("طُلب الإيقاف؛ نحفظ الرد الجاري عند انتهاء مهلة I/O.")

    def shutdown(self):
        self.closing = True
        self.stop()
        self.controls()
        if not self.busy:
            self.executor.shutdown(wait=False)
