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
    QGridLayout,
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

from .roblox_activity import RobloxActivity
from .roblox_bulk import write_new
from .roblox_prefilter import MAX_CONFIGURED_BATCH, VERIFIED_OPERATIONAL_BATCH, Prefilter
from .roblox_prefilter_estimates import calculate, duration_text
from .roblox_prefilter_review import inspect_operation, record_review
from .ui_cards import Card, CardGrid, Disclosure, icon

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
        self.shared_restriction = False
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
        self.restriction_notice = QLabel()
        self.restriction_notice.setObjectName("notice")
        self.restriction_notice.setWordWrap(True)
        self.restriction_notice.hide()
        layout.addWidget(self.restriction_notice)
        root_layout = layout
        self.steps_area = QScrollArea()
        self.steps_area.setWidgetResizable(True)
        self.steps_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.steps_area.setMinimumHeight(90)
        steps_body = QWidget()
        layout = QVBoxLayout(steps_body)
        layout.setContentsMargins(0, 0, 0, 0)
        explanation_box = Disclosure("كيف نقرأ دليل الحسابات؟")
        explanation_box.body.addWidget(explanation)
        layout.addWidget(explanation_box)
        self.steps_area.setWidget(steps_body)
        root_layout.addWidget(self.steps_area, 1)
        self.steps = {}
        cards = []
        for key, title in (
            ("import", "1 · اختيار الملف واستعادته"),
            ("config", "2 · إعداد الدفعات والتوقيت"),
            ("prepare", "3 · تحضير محلي دون اتصال"),
            ("pilot", "4 · تجربة محدودة بتفويض صريح"),
            ("review", "5 · مراجعة الدليل والاستعادة"),
            ("full", "6 · تفويض مستقل لبقية الملف"),
            ("export", "7 · تصدير الفئات ومرشحي الفردي"),
        ):
            card = Card(title, "file" if key == "import" else "layers")
            self.steps[key] = card.body
            cards.append(card)
        self.step_cards = CardGrid(cards, columns=(2, 1), minimum=340)
        layout.addWidget(self.step_cards)
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
        self.batch.setRange(1, MAX_CONFIGURED_BATCH)
        self.batch.setValue(20)
        self.pilot_size = QSpinBox()
        self.pilot_size.setRange(1, 200)
        self.pilot_size.setValue(100)
        form.addRow("الفاصل بعد الرد (ثانية)", self.delay)
        form.addRow("الأسماء في كل طلب Bulk", self.batch)
        form.addRow("حد التجربة الأولية: أسماء صالحة فريدة", self.pilot_size)
        self.steps["config"].addLayout(form)
        for control in (self.delay, self.batch, self.pilot_size):
            control.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            control.setMaximumWidth(155)
        self.warning = QLabel("أقل من 10 ثوانٍ تجريبي؛ لم يُثبت استقراره للمسح الطويل.")
        self.warning.setObjectName("simulation")
        self.warning.setWordWrap(True)
        self.delay.valueChanged.connect(lambda v: self.warning.setVisible(v < 10))
        self.warning.hide()
        self.steps["config"].addWidget(self.warning)
        self.batch_warning = QLabel(
            "أكبر من20 تجريبي للتحضير والتقدير فقط؛ الإرسال محظور حتى إثبات دعم Roblox."
        )
        self.batch_warning.setObjectName("simulation")
        self.batch_warning.setWordWrap(True)
        self.batch_warning.hide()
        self.steps["config"].addWidget(self.batch_warning)
        rates = QGridLayout()
        rates.setHorizontalSpacing(12)
        self.rate_values = {}
        for column, (key, unit) in enumerate(
            (("second", "اسم/ثانية"), ("minute", "اسم/دقيقة"), ("hour", "اسم/ساعة"))
        ):
            value = QLabel()
            value.setObjectName("heading")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            unit_label = QLabel(unit + " — نظريًا")
            unit_label.setObjectName("micro")
            unit_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            rates.addWidget(value, 0, column)
            rates.addWidget(unit_label, 1, column)
            rates.setColumnStretch(column, 1)
            self.rate_values[key] = value
        self.steps["config"].addLayout(rates)
        self.estimates = QLabel()
        self.estimate_note = QLabel()
        self.estimate_note.setObjectName("micro")
        for label in (self.estimates, self.estimate_note):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.steps["config"].addWidget(label)
        for control in (self.delay, self.batch, self.pilot_size):
            control.valueChanged.connect(self.refresh_estimates)
        self.source.textChanged.connect(self.refresh_estimates)
        self.buttons = {}
        for name, label, callback in (
            ("file", "اختيار TXT / CSV…", self.choose_file),
            ("open", "فتح عملية محفوظة…", self.open_run),
            ("prepare", "استيراد ومعاينة محلية", self.prepare),
            ("inspect", "مراجعة الحالة والدليل", self.inspect),
            ("restriction", "مراجعة القيد والتوافق — دون اتصال", self.review_restriction),
            ("acknowledge", "تسجيل مراجعة الدليل — لا يرفع القيد", self.acknowledge_restriction),
            ("export_review", "تصدير تقرير القيد والتوافق…", self.export_review),
            ("pilot", "تفويض / استئناف التجربة المحدودة", lambda: self.start("pilot")),
            ("full", "تفويض / استئناف الباقي", lambda: self.start("full")),
            ("stop", "إيقاف وحفظ التقدم", self.stop),
            ("recover", "استعادة محلية دون إرسال", self.recover),
            ("review", "مراجعة تسليم غير محسوم", self.review_uncertain),
            ("export", "تصدير الفئات ومرشحي الفردي…", self.export),
        ):
            button = QPushButton(label)
            button.setIcon(
                icon(
                    "stop" if name == "stop" else "file" if name in ("file", "export") else "layers", size=18
                )
            )
            button.clicked.connect(callback)
            step = {
                "file": "import",
                "open": "import",
                "prepare": "prepare",
                "pilot": "pilot",
                "inspect": "review",
                "restriction": "review",
                "acknowledge": "review",
                "export_review": "review",
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
        self.review_details = QLabel("افتح عملية محفوظة لمراجعة القيد وتوافقها، دون إرسال طلبات.")
        self.review_details.setWordWrap(True)
        self.review_details.setTextFormat(Qt.TextFormat.PlainText)
        self.steps["review"].addWidget(self.review_details)
        for label in (
            explanation,
            self.status,
            self.counts,
            self.warning,
            self.restriction_notice,
            self.batch_warning,
            self.review_details,
        ):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setMinimumWidth(0)
        self.progress_changed.connect(self.show_progress)
        self.finished.connect(self.done)
        self.controls()

    def refresh_estimates(self, *_):
        # Cached metadata only: no SQL, source-file reads, provider calls or manifest writes.
        try:
            model = calculate(
                self.delay.value(),
                self.batch.value(),
                self.pilot_size.value(),
                self.snapshot,
                provider_blocked=self.shared_restriction,
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            self.calculation = None
            for label in self.rate_values.values():
                label.setText("—")
            self.estimates.setText("التقدير غير متاح؛ راجع الإعدادات وبيانات التوقيت.")
            self.estimate_note.clear()
            return
        self.calculation = model
        self.batch_warning.setVisible(not model["batch_supported"])
        for key, label in self.rate_values.items():
            label.setText(f"{model['names_per_' + key]:,.2f}")
        remaining = model["remaining_requests"]
        remaining_text = f"{remaining:,}" if remaining is not None else "غير معلوم قبل الاستيراد"
        observed = model["observed_names_per_minute"]
        observed_text = f"{observed:,.2f} اسم/دقيقة" if observed is not None else "غير متاح بتوقيت موثوق"
        remaining_line = (
            f"طلبات الباقي: {remaining_text} • المدة: {duration_text(model['remaining_seconds'])}"
            if remaining is not None
            else "طلبات الباقي ومدته: تتحدد بعد اكتمال الاستيراد."
        )
        self.estimates.setText(
            f"التقدير النظري • دفعة {model['config']['batch_size']:,} • فاصل {model['config']['delay']:g}ث\n"
            + remaining_line
            + "\n"
            f"التجربة المتبقية: {model['pilot_requests']:,} طلبًا • {duration_text(model['pilot_seconds'])}\n"
            f"مرصود (يشمل المجهول): {observed_text}"
        )
        measured = model["latency_source"] == "measured_safe_responses"
        source = (
            f"زمن الرد: متوسط {model['latency_seconds']:.3f}ث من {model['latency_samples']:,} ردود آمنة."
            if measured
            else f"افتراض توضيحي لزمن الرد {model['latency_seconds']:g}ث؛ ليس قياسًا."
        )
        scope = "" if model["scope_complete"] else " عدد الملف غير مكتمل حتى ينتهي الاستيراد."
        blocked = (
            " التشغيل محظور؛ الأرقام افتراضية فقط."
            if model["restricted"] or model["review_required"] or not model["batch_supported"]
            else ""
        )
        details = (
            "يشمل الرد والفاصل والدفعات الجزئية؛ يستثني الاستيراد والتصدير والقرص "
            "ومراجعة المستخدم والقيود اللاحقة. إعدادات العملية المحفوظة ثابتة. التقدير ليس ضمانًا."
        )
        self.estimate_note.setText(source + scope + blocked)
        self.estimate_note.setToolTip(details)
        self.estimates.setToolTip(details)

    def controls(self):
        marker = RobloxActivity(self.owner.repo.store.path).block
        self.shared_restriction = marker.exists()
        if self.shared_restriction and self.snapshot is None:
            reason = "قيد Bulk محفوظ"
            try:
                with marker.open("rb") as stream:
                    raw = stream.read(256).decode("ascii")
                if raw and all(c in "abcdefghijklmnopqrstuvwxyz0123456789_:" for c in raw):
                    reason = raw
            except (OSError, UnicodeError):
                pass
            self.restriction_notice.setText(
                "توقف وقائي محفوظ: "
                + reason
                + ". التشغيل محظور. افتح العملية لمراجعة الدليل؛ لا يُرفع القيد تلقائيًا أو بتغيير السرعة."
            )
            self.restriction_notice.show()
        elif self.shared_restriction:
            self.restriction_notice.show()
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
                eligible &= not (self.shared_restriction or self.snapshot.get("provider_blocked", False))
                eligible &= self.snapshot["manifest"]["settings"]["batch_size"] <= VERIFIED_OPERATIONAL_BATCH
                eligible &= self.snapshot.get("timing_policy_current", False)
                self.buttons[phase].setEnabled(bool(eligible and not self.busy and not self.closing))
        self.refresh_estimates()

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
                ("ملف التقرير: " if result.is_file() else "مجلد التصدير: ")
                + str(result)
                + (
                    " — مراجعة محلية، دون إذن اتصال أو رفع قيد."
                    if result.is_file()
                    else " — افحص export-state.json لاكتماله؛ مرشحو الفردي في individual-candidates.txt"
                )
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
        if not data.get("timing_policy_current"):
            rate = None
        rate_note = f"{rate:.2f} اسم/دقيقة" if rate is not None else "غير محسوم"
        safe = sum(q["safe_responses"] for q in data["quality"].values())
        last = data.get("last_request") or {}
        stops = [p["stop_reason"] for p in phases.values() if p["state"] in ("Restricted", "ReviewRequired")]
        restricted = bool(data.get("provider_blocked")) or any(
            p["state"] == "Restricted" for p in phases.values()
        )
        review = data.get("restriction_review")
        if review:
            evidence = review["last_response_evidence"] or {}
            headers = evidence.get("headers", {})
            self.review_details.setText(
                f"القيد المحفوظ: {review['shared_restriction_reason'] or 'راجع حالة الأطوار'}\n"
                f"آخر رد: HTTP {evidence.get('status', '—')} • {evidence.get('response_at', '—')}\n"
                f"Retry-After: {headers.get('retry-after', 'غير موجود')} • "
                f"إعادة ضبط الحصة: {headers.get('x-ratelimit-reset', 'غير موجود')}\n"
                f"توافق بصمة المصدر: {'مطابقة' if review['policy_matches'] else 'غير مثبت؛ استئناف محظور'}\n"
                + review["explanation_ar"]
            )
            self.review_details.setToolTip(json.dumps(review, ensure_ascii=False, indent=2))
        else:
            self.review_details.setText("استخدم مراجعة القيد والتوافق لعرض الدليل والترويسات دون اتصال.")
        self.restriction_notice.setVisible(restricted or bool(stops))
        self.restriction_notice.setText(
            "توقف وقائي محفوظ: "
            + (" • ".join(stops) or "قيد Bulk من عملية أخرى")
            + ". لا إعادة محاولة أو تحرير تلقائي. راجع الدليل وصدّره؛ "
            "تغيير الفاصل أو فتح التطبيق لا يرفع القيد."
        )
        self.counts.setText(
            f"مقروءة: {c['imported']:,} • فريدة: {c['unique']:,} • مكررة: {c['duplicates']:,} • "
            f"غير صالحة: {c['invalid']:,}\n"
            f"عولجت: {c['processed']:,} • تطابق حساب مستبعد: {c['excluded']:,} • "
            f"مرشحو الفردي: {data['remaining_candidates']:,}\n"
            f"مجهولة: {c['unknown']:,} • غير آمنة: {c['unsafe']:,} • طلبات فاشلة: {c['failed_requests']:,} • "
            f"ردود آمنة: {safe:,} • ردود HTTP مكتملة: {c['completed_requests']:,}/{c['attempts']:,}\n"
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
                data.get("import_error")
                or (
                    f"توقف وقائي • {stops[-1] if stops else 'قيد محفوظ'} • {safe:,} ردًا آمنًا / "
                    f"{c['attempts']:,} طلبًا • أسماء الدفعة الأخيرة: {last.get('affected_names', 0):,}"
                    if restricted or stops
                    else "الاستيراد: " + STATE_LABELS.get(data["import_state"], data["import_state"])
                )
            )
            self.status.setToolTip("مجلد العملية: " + str(self.service.directory) if self.service else "")
        self.controls()

    def choose_file(self):
        if self.owner.busy or self.closing:
            return
        path, _ = QFileDialog.getOpenFileName(self, "ملف أسماء كبير — UTF-8", "", "Usernames (*.txt *.csv)")
        if path:
            self.service = self.snapshot = None
            self.restriction_notice.hide()
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
                self.snapshot = None
                value = self.service.manifest()
                self.source.setText(value["source"]["files"][0]["path"])
                self.delay.setValue(value["settings"]["delay"])
                self.batch.setValue(value["settings"]["batch_size"])
                self.pilot_size.setValue(value["settings"]["pilot_names"])
                self.submit(lambda: inspect_operation(self.service))
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

    def review_restriction(self):
        if self.service:
            self.submit(lambda: inspect_operation(self.service))

    def acknowledge_restriction(self):
        if self.service and self.confirm(
            "سيسجل إقرار محلي بمراجعة الدليل فقط. لا يرفع الحظر أو يجيز اتصالًا أو يغير النتائج. هل تؤكد؟"
        ):
            self.submit(lambda: record_review(self.service, self.service.manifest()["fingerprint"]))

    def export_review(self):
        if not self.service:
            return
        path, _ = QFileDialog.getSaveFileName(self, "تقرير جديد للقيد والتوافق", "", "JSON (*.json)")
        if path:

            def work():
                write_new(Path(path), inspect_operation(self.service))
                return Path(path)

            self.submit(work)

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
