"""Reference-inspired dashboard built exclusively from bounded, already loaded views."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .platform_workspace import ReadableColumns, ResultsPresentation
from .ui_cards import ActionCard, Card, CardGrid, MetricCard, caption, icon


class OperationCard(Card):
    def __init__(self, window, platform, panel):
        super().__init__(platform, "roblox" if platform == "Roblox" else "x")
        self.window, self.panel = window, panel
        self.status = caption("", "bodyText")
        self.body.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.body.addWidget(self.progress)
        actions = QHBoxLayout()
        self.open_button = QPushButton("التفاصيل")
        self.open_button.setIcon(icon("info", size=17))
        self.open_button.clicked.connect(lambda: window.tabs.setCurrentWidget(panel))
        self.stop_button = QPushButton("إيقاف وحفظ")
        self.stop_button.setObjectName("danger")
        self.stop_button.setIcon(icon("stop", "#ce425d", 17))
        self.stop_button.clicked.connect(self.stop)
        actions.addWidget(self.open_button)
        actions.addStretch()
        actions.addWidget(self.stop_button)
        self.body.addLayout(actions)

    def stop(self):
        panel = self.panel
        if hasattr(panel, "prefilter") and panel.prefilter.busy:
            panel.prefilter.stop()
        elif hasattr(panel, "bulk") and panel.bulk.busy:
            panel.bulk.stop()
        else:
            panel.stop()

    def refresh(self):
        panel = self.panel
        self.setVisible(panel.busy)
        self.stop_button.setEnabled(panel.busy and not panel.closing and not panel.stopping)
        self.status.setText(panel.activity_status.text())
        self.progress.hide()
        if hasattr(panel, "prefilter") and panel.prefilter.busy:
            self.status.setText("تصفية ملفات Bulk · " + panel.prefilter.status.text())
            self.progress.setValue(panel.prefilter.progress.value())
            self.progress.show()
        elif hasattr(panel, "bulk") and panel.bulk.busy:
            self.status.setText("التجربة المجمّدة · " + panel.bulk.status.text())
        else:
            identity, data = getattr(panel, "view_progress", (None, None))
            if identity == panel.current_run and data and data.get("total"):
                done = data.get("completed", data.get("counts", {}).get("Done", 0))
                self.progress.setValue(min(1000, done * 1000 // data["total"]))
                self.progress.setFormat(f"{done:,} / {data['total']:,}")
                self.progress.show()


class DashboardPanel(QScrollArea):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12, 10, 12, 14)
        layout.setSpacing(13)
        heading = QHBoxLayout()
        title = caption("لوحة التحكم", "pageTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.activity = caption("", "statusPill")
        heading.addWidget(self.activity)
        layout.addLayout(heading)
        layout.addWidget(
            caption("نظرة على عملياتك ونتائجك المحمّلة، ووصول سريع إلى أدوات العمل.", "pageSubtitle")
        )
        self.metrics = [
            MetricCard("فاحصات نشطة", "Roblox وX الآن", "play"),
            MetricCard("عمليات معروضة", "صفحة مدير العمليات المحمّلة", "layers"),
            MetricCard("نتائج معروضة", "صفحتا نتائج Roblox وX", "users"),
            MetricCard("تحتاج مراجعة", "ضمن العمليات المعروضة فقط", "info"),
        ]
        layout.addWidget(CardGrid(self.metrics, minimum=150))
        actions_card = Card("ابدأ عملًا جديدًا", "generate", shadow=True)
        self.quick_actions = [
            ActionCard("استيراد ملف", "أسماء TXT / CSV", "import", lambda: self.open_roblox(imported=True)),
            ActionCard("فحص أسماء", "RoValid · تحقق أولي", "search", self.open_roblox),
            ActionCard("نمط RoValid السريع", "Bulk ثم Validator", "filter", self.open_prefilter),
            ActionCard(
                "توليد أسماء",
                "إنشاء ملف بقواعدك",
                "generate",
                lambda: window.tabs.setCurrentWidget(window.file_generator),
            ),
        ]
        actions_card.body.addWidget(CardGrid(self.quick_actions, minimum=145))
        layout.addWidget(actions_card)
        open_jobs = QPushButton("كل العمليات ←")
        open_jobs.setObjectName("textButton")
        open_jobs.clicked.connect(lambda: window.tabs.setCurrentWidget(window.job_manager))
        operations = Card("العمليات الجارية", "layers", extra=open_jobs)
        self.operation_cards = [
            OperationCard(window, name, panel)
            for name, panel in (("Roblox", window.roblox), ("X", window.x_panel))
        ]
        for card in self.operation_cards:
            operations.body.addWidget(card)
        self.active_empty = QWidget()
        self.active_empty.setObjectName("emptyState")
        self.active_empty.setMinimumHeight(165)
        empty_layout = QVBoxLayout(self.active_empty)
        empty_layout.setContentsMargins(12, 18, 12, 18)
        empty_layout.addStretch()
        empty_layout.addWidget(
            caption("لا توجد عمليات فحص جارية", "emptyTitle"), alignment=Qt.AlignmentFlag.AlignCenter
        )
        note = caption("حضّر أسماءك أولًا، ثم ابدأ من صفحة المنصة. لن يبدأ أي فحص تلقائيًا.")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(note)
        empty_layout.addStretch()
        operations.body.addWidget(self.active_empty)
        capabilities = Card("حالة المنصات", "layers")
        tiles = []
        for name, note, symbol, panel in (
            ("Roblox", "تحقق تسجيل أولي", "roblox", window.roblox),
            ("X", "تجريبي · غير موثوق", "x", window.x_panel),
            ("Instagram", "الإتاحة معطّلة", "instagram", window.instagram),
            ("Snapchat", "محاكاة فقط", "snapchat", window.snapchat_tabs),
        ):
            tile = ActionCard(name, note, symbol, lambda _=False, p=panel: window.tabs.setCurrentWidget(p))
            tile.setMinimumHeight(80)
            tiles.append(tile)
        capabilities.body.addWidget(CardGrid(tiles, minimum=120))
        layout.addWidget(CardGrid((operations, capabilities), columns=(2, 1), minimum=340))
        results_card = Card("آخر النتائج المعروضة", "file", shadow=True)
        self.results_table = window.make_table(
            ["الاسم", "المنصة", "النتيجة الأولية", "آخر فحص", "المصدر / السبب"]
        )
        self._last_results = None
        self.results_table.setFixedHeight(178)
        self.result_columns = ReadableColumns(self.results_table, window.fill_table, fill_width=True)
        self.results_presentation = ResultsPresentation(self.results_table, (0, 1, 2), "")
        results_card.body.addWidget(self.results_presentation)
        results_card.body.addWidget(self.results_table)
        self.results_empty = caption("لا توجد نتائج محمّلة بعد؛ افتح سجل النتائج المحفوظة لاستعراضه.")
        results_card.body.addWidget(self.results_empty)
        saved = QPushButton("فتح النتائج المحفوظة")
        saved.setObjectName("textButton")
        saved.clicked.connect(window.job_manager.search_results)
        results_card.body.addWidget(saved)
        latest = Card("ملخص آخر عملية معروضة", "clock")
        self.recent = caption("", "bodyText")
        latest.body.addWidget(self.recent)
        self.latest_fields = QWidget()
        self.latest_fields.setObjectName("cardGrid")
        fields = QGridLayout(self.latest_fields)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setVerticalSpacing(8)
        self.latest_values = []
        for index, title in enumerate(("الحالة", "المكتمل / الإجمالي", "الفاصل المحفوظ", "تجاوز سابق")):
            fields.addWidget(caption(title), index, 0)
            value = caption("", "cardTitle")
            self.latest_values.append(value)
            fields.addWidget(value, index, 1)
        latest.body.addWidget(self.latest_fields)
        latest.body.addWidget(caption("ملخص محدود من الصفحات المحمّلة؛ ليس إحصاءً شاملًا للسجل.", "micro"))
        layout.addWidget(CardGrid((results_card, latest), columns=(2, 1), minimum=360))
        self.restrictions = caption(
            "نتائج التحقق أولية ولا تضمن التسجيل. Bulk دليل حسابات مستقل عن إتاحة التسجيل.", "notice"
        )
        layout.addWidget(self.restrictions)
        layout.addStretch()
        self.setWidget(body)
        self.update_timer = QTimer(self)
        self.update_timer.setSingleShot(True)
        self.update_timer.setInterval(150)
        self.update_timer.timeout.connect(self.refresh)
        for panel in (window.roblox, window.x_panel):
            panel.progress_changed.connect(self.queue_refresh)
            panel.idle.connect(self.queue_refresh)
        if hasattr(window.roblox, "prefilter"):
            window.roblox.prefilter.progress_changed.connect(self.queue_refresh)
            window.roblox.bulk.changed.connect(self.queue_refresh)
        window.job_manager.view_changed.connect(self.queue_refresh)

    def open_platform(self, panel, page):
        panel.workspace.setCurrentIndex(page)
        self.window.tabs.setCurrentWidget(panel)

    def open_roblox(self, _=False, *, imported=False):
        panel = self.window.roblox
        if not panel.busy:
            panel.checking_mode.setCurrentIndex(0)
            source = panel.source if hasattr(panel, "source") else panel.modes
            source.setCurrentIndex(1 if imported else 0)
            panel.workspace.setCurrentIndex(0)
        self.window.tabs.setCurrentWidget(panel)

    def open_prefilter(self):
        panel = self.window.roblox
        if not panel.busy:
            panel.checking_mode.setCurrentIndex(0 if hasattr(panel, "source") else 2)
            panel.workspace.setCurrentIndex(0)
        self.window.tabs.setCurrentWidget(panel)

    def queue_refresh(self, *_):
        if self.isVisible() and not self.update_timer.isActive():
            self.update_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        # No queries, queue scans, or network access. All figures describe bounded loaded views.
        window = self.window
        panels = (window.roblox, window.x_panel)
        rows = window.job_manager.rows
        active = sum(bool(p.busy) for p in panels)
        values = (
            active,
            len(rows),
            window.roblox.result_table.rowCount() + window.x_panel.table.rowCount(),
            sum(r["state"] in ("Restricted", "Error", "PreparationError") for r in rows),
        )
        for metric, value in zip(self.metrics, values, strict=True):
            metric.value.setText(f"{value:,}")
        self.activity.setText(f"{active} فاحص نشط" if active else "غير نشط · لا فحص تلقائي")
        for card in self.operation_cards:
            card.refresh()
        self.active_empty.setVisible(not active)
        recent = rows[0] if rows else None
        self.recent.setText(
            f"{recent['platform']} · {recent['id'][:8]}"
            if recent
            else "لا توجد عمليات معروضة بعد.\nحضّر عملية من صفحة Roblox أو X؛ لن يبدأ التنفيذ هنا."
        )
        self.latest_fields.setVisible(bool(recent))
        if recent:
            values = (
                recent["display_state"],
                f"{recent['done']:,} / {recent['total']:,}"
                if recent["total"] is not None
                else "التحضير غير مكتمل",
                f"{recent['interval_seconds']:g} ثانية",
                f"{recent['skipped']:,}",
            )
            for field, value in zip(self.latest_values, values, strict=True):
                field.setText(value)
        data = []
        for name, table, timestamp, evidence in (("X", window.x_panel.table, 2, 5),):
            for row in range(min(2, table.rowCount())):
                items = [table.item(row, c) for c in (0, 1, timestamp, evidence)]
                if all(items):
                    data.append([items[0].text(), name, items[1].text(), items[2].text(), items[3].text()])
        table = window.roblox.result_table
        for row in range(min(2, table.rowCount())):
            data.append(
                [
                    table.item(row, 0).text(),
                    "Roblox · RoValid",
                    table.item(row, 1).text(),
                    "غير محفوظ لكل اسم",
                    table.item(row, 2).text(),
                ]
            )
        if data != self._last_results:
            self.result_columns.fill(data)
            self._last_results = data
        self.results_empty.setVisible(not data)
        self.results_table.setVisible(bool(data))
        warnings = ["نتائج التحقق أولية ولا تضمن التسجيل. Bulk دليل حسابات؛ السجل المفقود لا يعني Available."]
        for name, panel in zip(("Roblox", "X"), panels, strict=True):
            if not panel.busy and not panel.start_button.isEnabled():
                warnings.append(f"{name}: البدء غير متاح حاليًا؛ راجع العملية وقيودها.")
        if not window.x_panel.restriction.isHidden():
            warnings.append(window.x_panel.restriction.text())
        self.restrictions.setText("\n".join(warnings))
