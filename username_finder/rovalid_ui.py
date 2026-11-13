"""Arabic RoValid workspace with an isolated managed process and no legacy dispatch."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QProcess, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import rovalid
from .roblox_storage import RobloxStore
from .rovalid_filters_ui import ResultFilterPanel
from .rovalid_proxy_ui import PrivateProxyEdit
from .rovalid_review_ui import review_dialog
from .ui_cards import Card, PageHeading, caption


class RoValidPanel(QWidget):
    idle = Signal()
    progress_changed = Signal()

    def __init__(self, store, make_table, fill_table):
        super().__init__()
        self.store, self.fill_table = store, fill_table
        self.repo = RobloxStore(store)  # Read-only legacy summaries and safety; never run/recover old work.
        self.root = store.path.parent / "rovalid"
        self.current_run, self.record = None, {}
        self.closing = self.stopping = False
        self.view_progress = (None, None)
        self.result_offset, self.result_total = 0, 0
        self.proxy_pool = []
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(Path(__file__).resolve().parent.parent))
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.readyReadStandardError.connect(lambda: self.process.readAllStandardError())
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.buffer, self.outcome, self.action = b"", None, ""
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout = QVBoxLayout(self)
        layout.addWidget(
            PageHeading("Roblox · RoValid", "محرك مستقل • نتائج إتاحة أولية لا تضمن التسجيل", "roblox")
        )
        self.restriction = caption("", "notice")
        layout.addWidget(self.restriction)
        self.workspace = QTabWidget()
        layout.addWidget(self.workspace, 1)
        page = QWidget()
        body = QVBoxLayout(page)
        source = Card("مصدر الأسماء", "file")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        source.body.addLayout(form)
        self.source = QComboBox()
        self.source.addItem("توليد أسماء", "generate")
        self.source.addItem("استيراد ملف أسماء TXT", "file")
        form.addRow("المصدر", self.source)
        self.length = QComboBox()
        for value in (3, 4, 5, 6):
            self.length.addItem(str(value), value)
        form.addRow("طول الاسم", self.length)
        self.count = QSpinBox()
        self.count.setRange(1, 1_000_000)
        form.addRow("العدد المطلوب", self.count)
        self.underscore = QCheckBox("السماح بشرطة سفلية وفق مولّد RoValid")
        self.repeat = QCheckBox("تكرار التوليد حتى العثور على نتيجة — قد يستمر حتى الإيقاف")
        form.addRow(self.underscore)
        form.addRow(self.repeat)
        source.body.addWidget(
            caption(
                "التوليد يتبع RoValid الأصلي: قد يزيد خيار '_' الطول حرفًا، "
                "وقد يقل العدد الفعلي عند نفاد مساحة الأسماء؛ تعرض المعاينة العدد الناتج."
            )
        )
        self.input_path = QLineEdit()
        self.input_path.setReadOnly(True)
        self.input_path.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        picker = QPushButton("اختيار ملف الأسماء…")
        picker.clicked.connect(self.choose_input)
        form.addRow(picker, self.input_path)
        body.addWidget(source)
        connection = Card("الاتصال والإشعارات", "layers")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        connection.body.addLayout(form)
        self.proxy_mode = QComboBox()
        self.proxy_mode.addItem("مباشر — دون proxies", "direct")
        self.proxy_mode.addItem("استيراد proxies من TXT", "file")
        self.proxy_mode.addItem("لصق / إدخال proxies", "manual")
        self.proxy_mode.addItem("جلب مصادر RoValid وفحص الاتصال", "sources")
        form.addRow("الاتصال", self.proxy_mode)
        self.proxy_path = QLineEdit()
        self.proxy_path.setReadOnly(True)
        self.proxy_picker = QPushButton("استيراد ملف proxies…")
        self.proxy_picker.clicked.connect(self.choose_proxies)
        form.addRow(self.proxy_picker, self.proxy_path)
        self.proxy_entries = PrivateProxyEdit()
        form.addRow(self.proxy_entries)
        self.proxy_reveal = QCheckBox("إظهار الإدخالات مؤقتًا — قد تحتوي بيانات اعتماد")
        self.proxy_reveal.toggled.connect(self.proxy_entries.reveal)
        form.addRow(self.proxy_reveal)
        self.proxy_validate = QPushButton("تحقق محلي من الصيغة والتكرار — دون شبكة")
        self.proxy_validate.clicked.connect(self.validate_proxy_input)
        form.addRow(self.proxy_validate)
        self.proxy_fetch = QPushButton("جلب وفحص المصادر بطلب صريح")
        self.proxy_fetch.clicked.connect(self.fetch_proxies)
        form.addRow(self.proxy_fetch)
        self.proxy_notice = caption("")
        form.addRow(self.proxy_notice)
        self.remove_bad = QCheckBox("إزالة proxies الفاشلة من تجمع الجلسة وفق المحرك")
        form.addRow(self.remove_bad)
        self.proxy_mode.currentIndexChanged.connect(self.proxy_controls)
        self.webhook = QLineEdit()
        self.webhook.setEchoMode(QLineEdit.EchoMode.Password)
        self.webhook.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        form.addRow("Discord webhook (اختياري)", self.webhook)
        self.template = QLineEdit("<name> — <link> — <elapsed>s")
        form.addRow("قالب الرسالة", self.template)
        connection.body.addWidget(
            caption(
                "الأسرار في ذاكرة الجلسة فقط ولا تحفظ في الإعدادات أو السجل. "
                "لا scraping تلقائيًا أو تبديل هوية لتجاوز القيود. "
                "قالب المحرك يدعم <name> و<link> و<time> و<elapsed>."
            )
        )
        body.addWidget(connection)
        checking = Card("إعدادات المحرك", "clock")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        checking.body.addLayout(form)
        self.checking_mode = QComboBox()
        self.checking_mode.addItem("RoValid السريع — Bulk ثم Validator", "two_stage")
        self.checking_mode.addItem("Validator فقط", "validator")
        self.checking_mode.currentIndexChanged.connect(
            lambda: self.controls() if hasattr(self, "start_button") else None
        )
        form.addRow("النمط", self.checking_mode)
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 2000)
        form.addRow("العمال المتزامنون", self.concurrency)
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 300)
        form.addRow("مهلة الطلب (ثانية)", self.timeout)
        self.gap = QDoubleSpinBox()
        self.gap.setRange(0, 3600)
        self.gap.setDecimals(3)
        form.addRow("أقل فاصل بين بدايات الطلبات المباشرة (ثانية)", self.gap)
        self.diagnostics = QCheckBox("حفظ تشخيص المحرك المحلي")
        self.stop_on_429 = QCheckBox("إيقاف عند بلوغ حد 429 خلال النافذة")
        form.addRow(self.diagnostics)
        form.addRow(self.stop_on_429)
        self.threshold = QSpinBox()
        self.threshold.setRange(1, 10000)
        self.window_seconds = QDoubleSpinBox()
        self.window_seconds.setRange(0.1, 86400)
        form.addRow("حد استجابات 429", self.threshold)
        form.addRow("نافذة الحد (ثانية)", self.window_seconds)
        checking.body.addWidget(
            caption(
                "هذه إعدادات RoValid الأصلية، وليست ضمانًا لسعة Roblox. "
                "الفاصل يخص الاتصال المباشر فقط؛ المحرك يحتفظ بمحاولاته وسياسة تبريده الأصلية."
            )
        )
        body.addWidget(checking)
        self.prepare_button = QPushButton("معاينة وحفظ الإدخال محليًا")
        self.prepare_button.clicked.connect(self.prepare)
        body.addWidget(self.prepare_button)
        self.preview = caption("لم يُحضّر إدخال؛ لا طلبات شبكة.")
        body.addWidget(self.preview)
        body.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.settings_page = scroll
        self.workspace.addTab(scroll, "الإعدادات")
        run_page = QWidget()
        run_layout = QVBoxLayout(run_page)
        self.activity_status = caption("متوقف • لا تشغيل تلقائي", "notice")
        run_layout.addWidget(self.activity_status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        run_layout.addWidget(self.progress)
        self.statistics = caption("لا توجد جلسة نشطة", "bodyText")
        run_layout.addWidget(self.statistics)
        actions = QGridLayout()
        self.start_button = QPushButton("بدء الفحص")
        self.start_button.setObjectName("primary")
        self.resume_button = QPushButton("استئناف غير المحسوم بتأكيد")
        self.stop_button = QPushButton("إيقاف وحفظ")
        self.stop_button.setObjectName("danger")
        self.restriction_button = QPushButton("تحقق من القيد — محلي فقط")
        self.review_button = QPushButton("مراجعة القيد وتسجيل القرار")
        for index, (button, callback) in enumerate(
            (
                (self.start_button, self.start),
                (self.resume_button, self.resume),
                (self.stop_button, self.stop),
                (self.restriction_button, self.controls),
                (self.review_button, self.review_restriction),
            )
        ):
            button.clicked.connect(callback)
            actions.addWidget(button, index // 2, index % 2)
        run_layout.addLayout(actions)
        run_layout.addWidget(
            caption(
                "العدادات والسرعات تخص الجولة الحالية؛ لا تعني زيادة مؤكدة في أسماء قابلة للتسجيل. "
                "الإيقاف تعاوني: ينتظر المحرك الحفظ والتنظيف، وقد يتأخر أثناء التوليد أو webhook."
            )
        )
        run_layout.addStretch()
        self.workspace.addTab(run_page, "التشغيل")
        result_page = QWidget()
        result_layout = QVBoxLayout(result_page)
        result_layout.addWidget(
            caption(
                "يُحفظ تفصيليًا Available (أولي) وUnknown فقط. Taken وCensored وInvalid عدادات إجمالية؛ "
                "لا نختلق سجلات فردية أو نساوي Censored بملكية حساب. كل المرشحات تُجمع بشرط AND."
            )
        )
        self.result_filters = ResultFilterPanel()
        self.search, self.filter = self.result_filters.search, self.result_filters.classification
        self.result_filters.apply.clicked.connect(self.refresh_results)
        self.result_filters.reset.clicked.connect(self.refresh_results)
        self.search.returnPressed.connect(self.refresh_results)
        result_layout.addWidget(self.result_filters)
        filters = QHBoxLayout()
        export = QPushButton("تصدير CSV…")
        export.clicked.connect(self.export)
        self.previous_results = QPushButton("السابق")
        self.next_results = QPushButton("التالي")
        self.previous_results.clicked.connect(lambda: self.result_page(-1))
        self.next_results.clicked.connect(lambda: self.result_page(1))
        for control in (self.previous_results, self.next_results, export):
            filters.addWidget(control)
        result_layout.addLayout(filters)
        self.result_table = make_table(["الاسم", "التصنيف الأولي", "ملف المصدر"])
        result_layout.addWidget(self.result_table, 1)
        self.result_count = caption("")
        result_layout.addWidget(self.result_count)
        self.workspace.addTab(result_page, "النتائج")
        history_page = QWidget()
        history_layout = QVBoxLayout(history_page)
        history_layout.addWidget(
            caption(
                "سجل RoValid مستقل. عمليات SQLite القديمة محفوظة للقراءة "
                "عبر إدارة العمليات والبحث في النتائج؛ "
                "لا تُرحّل أو تُستأنف من الواجهة الجديدة. الإنهاء غير المتوقع يحتاج تدقيقًا، ولا يُستأنف تلقائيًا."
            )
        )
        self.history = make_table(["العملية", "التاريخ", "الحالة", "أسماء الإدخال"])
        self.history.itemSelectionChanged.connect(self.select_history)
        history_layout.addWidget(self.history, 1)
        button = QPushButton("تحديث السجل")
        button.clicked.connect(self.refresh_history)
        history_layout.addWidget(button)
        self.workspace.addTab(history_page, "السجل")
        try:
            saved = (
                rovalid.read_json(self.root / "settings.json")
                if (self.root / "settings.json").exists()
                else {}
            )
            self.apply_options(rovalid.settings(saved))
        except (OSError, ValueError):
            self.apply_options(rovalid.DEFAULTS)
            self.activity_status.setText("إعدادات RoValid غير سليمة؛ عُرضت الافتراضيات دون الكتابة فوق الملف.")
        self.refresh_history()
        self.controls()
        self.proxy_controls()

    @property
    def busy(self):
        return self.process.state() != QProcess.ProcessState.NotRunning

    def options(self):
        return rovalid.settings(
            dict(
                source=self.source.currentData(),
                length=self.length.currentData(),
                count=self.count.value(),
                underscore=self.underscore.isChecked(),
                repeat=self.repeat.isChecked(),
                mode=self.checking_mode.currentData(),
                concurrency=self.concurrency.value(),
                timeout=self.timeout.value(),
                gap=self.gap.value(),
                diagnostics=self.diagnostics.isChecked(),
                stop_on_429=self.stop_on_429.isChecked(),
                threshold=self.threshold.value(),
                window=self.window_seconds.value(),
                remove_bad=self.remove_bad.isChecked(),
            )
        )

    def apply_options(self, options):
        for control, key in ((self.source, "source"), (self.length, "length"), (self.checking_mode, "mode")):
            control.setCurrentIndex(control.findData(options[key]))
        for control, key in (
            (self.count, "count"),
            (self.concurrency, "concurrency"),
            (self.timeout, "timeout"),
            (self.gap, "gap"),
            (self.threshold, "threshold"),
            (self.window_seconds, "window"),
        ):
            control.setValue(options[key])
        for control, key in (
            (self.underscore, "underscore"),
            (self.repeat, "repeat"),
            (self.diagnostics, "diagnostics"),
            (self.stop_on_429, "stop_on_429"),
            (self.remove_bad, "remove_bad"),
        ):
            control.setChecked(options[key])

    def choose_input(self):
        path, _ = QFileDialog.getOpenFileName(self, "اختيار أسماء RoValid", "", "Text (*.txt);;All files (*)")
        if path:
            self.input_path.setText(path)
            self.source.setCurrentIndex(1)
            self.repeat.setChecked(False)

    def choose_proxies(self):
        path, _ = QFileDialog.getOpenFileName(self, "اختيار proxies — لا اختبار شبكة", "", "Text (*.txt)")
        if path:
            self.proxy_path.setText(path)
            self.proxy_mode.setCurrentIndex(1)

    def controls(self):
        active = self.busy or self.closing
        self.settings_page.setEnabled(not active)
        self.history.setEnabled(not active)
        self.result_filters.setEnabled(not active)
        self.previous_results.setEnabled(not active and self.result_offset > 0)
        self.next_results.setEnabled(not active and self.result_offset + 500 < self.result_total)
        self.prepare_button.setEnabled(not active)
        permission = self.current_permission() if not active else dict(allowed=False, reason="")
        self.checking_allowed = permission["allowed"]
        self.restriction.setText(permission["reason"] or "عملية محلية جارية؛ لا بدء ثانٍ.")
        effective = self.current_permission(saved=True) if not active else permission
        allowed = effective["allowed"] and not active
        if (
            not active
            and self.record
            and self.record.get("settings", {}).get("mode") != self.checking_mode.currentData()
        ):
            self.restriction.setText(
                permission["reason"] + "\nإذن العملية المحفوظة للاستئناف:\n" + effective["reason"]
            )
        self.review_button.setEnabled(not active)
        fetch_allowed = (
            rovalid.permission(self.store.path, mode="two_stage", connection="sources")["allowed"]
            if not active
            else False
        )
        self.proxy_fetch.setEnabled(fetch_allowed and self.proxy_mode.currentData() == "sources")
        self.start_button.setEnabled(allowed and self.record.get("state") == "Prepared")
        self.resume_button.setEnabled(allowed and self.record.get("state") in ("Stopped", "Completed"))
        self.stop_button.setEnabled(self.busy and not self.stopping)
        self.progress_changed.emit()

    def connection_scope(self):
        mode = self.proxy_mode.currentData()
        return "private" if mode in ("file", "manual") else mode

    def current_permission(self, *, saved=False):
        mode = (
            self.record.get("settings", {}).get("mode", self.checking_mode.currentData())
            if saved
            else self.checking_mode.currentData()
        )
        return rovalid.permission(self.store.path, mode=mode, connection=self.connection_scope())

    def review_restriction(self):
        if self.busy or self.closing:
            return
        mode = self.checking_mode.currentData()
        if self.connection_scope() == "sources" and mode == "validator":
            target, accepted = QInputDialog.getItem(
                self,
                "نطاق مراجعة المصادر",
                "قرار مستقل لكل نطاق:",
                ["فحص Validator عبر المصادر", "جلب المصادر وفحص اتصال Bulk"],
                0,
                False,
            )
            if not accepted:
                return
            mode = "two_stage" if target == "جلب المصادر وفحص اتصال Bulk" else "validator"
        review_dialog(self, self.store.path, mode, self.connection_scope())
        self.controls()

    def prepare(self):
        if self.busy or self.closing:
            return
        try:
            options = self.options()
            rovalid.atomic_json(self.root / "settings.json", options)
            self.current_run = uuid4().hex
            self.record = {}
            self.launch("prepare", dict(settings=options, input_path=self.input_path.text()))
        except (OSError, ValueError):
            self.activity_status.setText("تعذر حفظ الإعدادات أو قبولها؛ لم يبدأ فحص.")

    def secret_options(self):
        proxies = []
        mode = self.proxy_mode.currentData()
        if mode == "file":
            if self.proxy_path.text():
                path = Path(self.proxy_path.text())
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError("ملف proxies أكبر من حد الواجهة 1 MiB.")
                proxies.extend(path.read_text(encoding="utf-8-sig").splitlines())
        elif mode == "manual":
            proxies = self.proxy_entries.toPlainText().replace(";", "\n").splitlines()
        elif mode == "sources":
            proxies = self.proxy_pool
        if mode != "direct" and not any(value.strip() for value in proxies):
            raise ValueError("لا توجد proxies لهذا المصدر؛ أدخل قائمة أو اختر الاتصال المباشر.")
        entries, counts = rovalid.proxy_entries(proxies)
        self.proxy_notice.setText(
            f"مدخلات: {counts['source_count']} • مكرر: {counts['duplicates']} • "
            f"مقبول صياغيًا: {counts['accepted']}؛ لم يُفحص الاتصال أو Roblox."
        )
        return rovalid.secrets_payload(entries, self.webhook.text().strip(), self.template.text()) | dict(
            scraped=mode == "sources"
        )

    def proxy_controls(self):
        mode = self.proxy_mode.currentData()
        self.proxy_path.setVisible(mode == "file")
        self.proxy_picker.setVisible(mode == "file")
        self.proxy_entries.setVisible(mode == "manual")
        self.proxy_reveal.setVisible(mode == "manual")
        if mode != "manual":
            self.proxy_reveal.setChecked(False)
        self.proxy_validate.setVisible(mode in ("file", "manual"))
        self.remove_bad.setVisible(mode != "direct")
        self.proxy_fetch.setVisible(mode == "sources")
        status = (
            rovalid.permission(self.store.path, mode="two_stage", connection="sources")
            if mode == "sources"
            else None
        )
        self.proxy_fetch.setEnabled(bool(status and status["allowed"]) and not self.busy)
        if mode == "sources":
            self.proxy_notice.setText(
                "الفحص الأصلي يتصل بـRoblox ويقبل 200 أو429 كدليل وصول فقط، وليس تحقق إتاحة.\n"
                + (status["reason"] if not status["allowed"] else "يلزم إجراء صريح؛ لا جلب تلقائي.")
            )
        else:
            self.proxy_notice.setText("لا إرسال تلقائي؛ فحص الصيغة لا يثبت اتصالًا أو صحة نتائج Roblox.")
        if hasattr(self, "start_button"):
            self.controls()

    def validate_proxy_input(self):
        try:
            self.secret_options()
        except (ValueError, OSError, UnicodeError):
            self.proxy_notice.setText("تعذر قبول الملف أو الإدخال؛ راجع الصيغة. لم تُعرض أسرار ولم يُرسل طلب.")

    def fetch_proxies(self):
        if self.busy or self.closing:
            return
        status = rovalid.permission(self.store.path, mode="two_stage", connection="sources")
        if not status["allowed"]:
            self.proxy_notice.setText(status["reason"])
            return
        if (
            QMessageBox.question(
                self,
                "تأكيد جلب وفحص proxies",
                "سيجلب المصدر قوائم عامة ويفحص الوصول إلى Roblox؛ ليس إثباتًا لصحة الفحص.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.launch("proxy_sources", {})

    def start(self):
        self._start(False)

    def resume(self):
        self._start(True)

    def _start(self, resume):
        if self.busy or self.closing or not self.current_run:
            return
        permission = self.current_permission(saved=True)
        if not permission["allowed"]:
            self.restriction.setText(permission["reason"])
            return
        if self.record.get("state") not in (("Stopped", "Completed") if resume else ("Prepared",)):
            return
        try:
            if not resume and self.options() != self.record["settings"]:
                raise ValueError("تغيرت الإعدادات؛ أعد المعاينة والحفظ أولًا.")
            packet = self.secret_options() | dict(resume=resume)
            message = (
                "ستُستخدم إعدادات العملية المحفوظة. سيُعاد فقط غير المحسوم بطلبك الصريح؛ قد يكون قد حُوول سابقًا."
                if resume
                else "سيُشغّل محرك RoValid بطلبات حقيقية وإعداداته المحفوظة."
            )
            if QMessageBox.question(self, "تأكيد تشغيل Roblox", message) != QMessageBox.StandardButton.Yes:
                return
            self.launch("run", packet)
            self.workspace.setCurrentIndex(1)
        except (ValueError, OSError) as exc:
            self.activity_status.setText(
                str(exc) if isinstance(exc, ValueError) else "تعذر قراءة ملف proxies."
            )

    def launch(self, action, packet):
        if self.busy or self.closing:
            return
        self.action, self.outcome, self.buffer = action, None, b""
        self.stopping = False
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        # QProcess on Windows starts without a console; all output is private pipes.
        self.process.setProgram(str(executable))
        self.process.setArguments(
            [
                "-B",
                "-m",
                "username_finder.rovalid_child",
                action,
                str(self.root.resolve()),
                self.current_run or uuid4().hex,
                str(self.store.path.resolve()),
            ]
        )
        self.process.start()
        self.process.write((json.dumps(packet, ensure_ascii=True) + "\n").encode("utf-8"))
        self.activity_status.setText("جارٍ التحضير المحلي…" if action == "prepare" else "جارٍ تنفيذ العملية…")
        self.controls()

    def _read_output(self):
        self.buffer += bytes(self.process.readAllStandardOutput())
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                event = json.loads(line)
                kind = event["event"]
                if kind == "progress":
                    self.show_progress(event["data"])
                elif kind in ("prepared", "finished"):
                    self.record, self.outcome = event["data"], kind
                    if kind == "prepared":
                        self.preview.setText(
                            f"صالح: {self.record['valid']:,} • غير صالح: {self.record['invalid']:,} • "
                            f"مكرر: {self.record['duplicates']:,} — لم يُرسل طلب."
                        )
                elif kind == "view":
                    self.fill_table(
                        self.result_table, [[r[0], rovalid.LABELS[r[1]], r[2]] for r in event["rows"]]
                    )
                    self.result_total = event["total"]
                    self.result_offset = event.get("offset", 0)
                    begin = self.result_offset + 1 if event["rows"] else 0
                    end = self.result_offset + len(event["rows"]) if event["rows"] else 0
                    self.result_count.setText(f"{begin:,}–{end:,} من {self.result_total:,} مطابق")
                    self.outcome = kind
                elif kind == "proxy_pool":
                    self.proxy_pool = event["proxies"]
                    self.proxy_notice.setText(
                        f"مصدر: {event['source_count']} • مكرر: {event['duplicates']} • "
                        f"فُحص: {event['screened']} • مقبول وصولًا: {event['accepted']} • "
                        f"فشل وصول: {event['failures']}؛ ليس تحقق أسماء Roblox."
                    )
                    self.outcome = kind
                elif kind == "exported":
                    self.outcome = kind
                    self.export_count = event.get("count", 0)
                elif kind == "cancelled":
                    self.outcome = kind
                elif kind == "error":
                    self.outcome = "error"
                    self.activity_status.setText(event["reason"] + " (" + event["code"] + ")")
            except (ValueError, TypeError, KeyError):
                self.outcome = "error"
                self.activity_status.setText("بيانات عملية غير سليمة؛ لا نجاح مفترض.")
                self.stop()
        if len(self.buffer) > 1024 * 1024:
            self.outcome = "error"
            self.stop()

    def show_progress(self, data):
        total, done = data.get("input_total", 0), data.get("resolved", 0)
        saved = self.record.get("settings", {})
        requested = (
            saved.get("count", total)
            if saved.get("source") == "generate"
            else self.record.get("valid", total)
        )
        self.view_progress = (self.current_run, dict(total=total, completed=done))
        self.progress.setValue(min(1000, int(done * 1000 / total)) if total else 0)
        self.progress.setFormat(f"{done:,} / {total:,}")
        self.statistics.setText(
            f"المطلوب أصلًا: {requested:,} • إدخال الجولة {data.get('round_no', 1)}: {total:,}\n"
            f"محسوم: {done:,} • "
            f"غير محسوم حتى الآن: {max(0, total - done):,}\n"
            f"Available أولي: {data.get('works', 0)} • Taken: {data.get('taken', 0)} • "
            f"Censored: {data.get('censored', 0)} • Invalid: {data.get('invalid', 0)}\n"
            f"محاولات HTTP/ث: {data.get('rps', 0):.2f} • أسماء محسومة/ث: {data.get('checks_rps', 0):.2f}\n"
            f"HTTP الكلي: {data.get('requests', 0)} • 429: {data.get('ratelimited', 0)} • "
            f"proxies متاحة: {data.get('proxy_alive', 0)} • الزمن: {data.get('elapsed', 0):.1f} ثانية"
        )
        self.progress_changed.emit()

    def _process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.outcome = "error"
            self.activity_status.setText("تعذر بدء عملية RoValid؛ تحقق من Python والاعتمادات.")
            self.controls()
            self.idle.emit()

    def _finished(self, code, status):
        self._read_output()
        if (
            code != 0
            or status != QProcess.ExitStatus.NormalExit
            or not self.outcome
            or self.outcome == "error"
        ):
            detail = self.activity_status.text() if self.outcome == "error" else ""
            self.activity_status.setText(
                "فشل أو إنهاء غير متوقع؛ راجع الأدلة. لا استئناف تلقائي أو نجاح مفترض. " + detail
            )
        elif self.outcome == "cancelled":
            self.activity_status.setText("أُلغيت العملية المحلية؛ لم يُنشر تصدير جزئي.")
            if self.action == "view":
                self.result_offset, self.result_total = 0, 0
                self.result_count.setText("أُلغي البحث؛ أعد تطبيق المرشحات لعرض النتائج.")
        else:
            self.activity_status.setText(
                "اكتملت المعاينة المحلية."
                if self.action == "prepare"
                else f"حالة العملية: {self.record.get('state', '')} • "
                f"السبب: {self.record.get('reason', 'local_view')}"
            )
            if self.action == "export":
                self.activity_status.setText(f"حُفظ التصدير كاملًا: {self.export_count:,} سجل مطابق.")
        self.stopping = False
        self.refresh_history()
        self.show_saved_statistics()
        self.controls()
        self.idle.emit()

    def stop(self):
        if self.busy and not self.stopping:
            self.stopping = True
            if self.action in ("run", "view", "export"):
                self.process.write(b"stop\n")
            self.activity_status.setText("جارٍ الإيقاف التعاوني والحفظ؛ لا إغلاق قسري.")
            self.stop_button.setEnabled(False)

    def shutdown(self):
        self.closing = True
        self.stop()
        self.controls()

    def refresh_history(self):
        self.history.blockSignals(True)
        self.history_rows = rovalid.histories(self.root)
        if self.busy and self.action == "run":
            for row in self.history_rows:
                if row["id"] == self.current_run:
                    row["state"] = "Running"
        current = next((row for row in self.history_rows if row["id"] == self.current_run), None)
        if current and not self.busy:
            self.record = current
        self.fill_table(
            self.history,
            [[r["id"][:8], r["created_at"], r["state"], r.get("valid", 0)] for r in self.history_rows],
        )
        self.history.blockSignals(False)

    def select_history(self):
        index = self.history.currentRow()
        if not self.busy and 0 <= index < len(self.history_rows):
            self.record = self.history_rows[index]
            self.current_run = self.record["id"]
            self.result_offset, self.result_total = 0, 0
            self.fill_table(self.result_table, [])
            self.apply_options(rovalid.settings(self.record["settings"]))
            self.activity_status.setText(f"{self.record['state']} • {self.record.get('reason', '')}")
            self.show_saved_statistics()
            self.controls()

    def show_saved_statistics(self):
        try:
            data = self.record.get("summary")
            if data:
                for key in ("works", "taken", "censored", "invalid", "requests", "total_names"):
                    if type(data[key]) is not int or data[key] < 0:
                        raise ValueError("Invalid saved counter")
                if not math.isfinite(data["elapsed"]) or data["elapsed"] < 0:
                    raise ValueError("Invalid saved elapsed time")
                elapsed = max(data["elapsed"], 1e-9)
                resolved = sum(data[key] for key in ("works", "taken", "censored", "invalid"))
                self.show_progress(
                    data
                    | dict(
                        input_total=data["total_names"],
                        resolved=resolved,
                        rps=data["requests"] / elapsed,
                        checks_rps=resolved / elapsed,
                    )
                )
            elif self.record.get("progress"):
                self.show_progress(self.record["progress"])
            else:
                self.progress.setValue(0)
                self.statistics.setText("لا توجد إحصاءات فحص محفوظة لهذه العملية.")
                self.view_progress = (self.current_run, None)
        except (ValueError, TypeError, KeyError, OverflowError):
            self.record = self.record | dict(state="ReviewRequired")
            self.statistics.setText("إحصاءات غير سليمة؛ يلزم تدقيق السجل الأصلي. لا تشغيل.")
            self.view_progress = (self.current_run, None)

    def refresh_results(self, _=False, *, offset=0):
        if self.current_run and not self.busy and not self.closing:
            values = self.result_filters.values()
            if values != getattr(self, "applied_filters", None):
                offset = 0
            self.applied_filters = values
            self.fill_table(self.result_table, [])
            self.result_count.setText("جارٍ البحث في كامل السجل…")
            self.launch("view", dict(filters=values, offset=offset))

    def result_page(self, direction):
        self.refresh_results(offset=max(0, self.result_offset + direction * 500))

    def export(self):
        if not self.current_run or self.busy or self.closing:
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, "تصدير نتائج RoValid", "rovalid-results.csv", "CSV (*.csv)"
        )
        if destination:
            self.launch(
                "export",
                dict(
                    destination=destination,
                    filters=self.result_filters.values(),
                ),
            )
