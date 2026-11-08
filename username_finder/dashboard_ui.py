"""Bounded, read-only overview using data already displayed by existing panels."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QGridLayout, QGroupBox, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget


class DashboardPanel(QScrollArea):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(12)
        title = QLabel("مساحة العمل")
        title.setObjectName("title")
        layout.addWidget(title)
        subtitle = QLabel("عملياتك وأدواتك في مكان واحد • النتائج الأولية لا تضمن التسجيل")
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        self.activity = QLabel()
        self.activity.setObjectName("metric")
        self.activity.setWordWrap(True)
        layout.addWidget(self.activity)
        shortcuts = QGroupBox("ابدأ من هنا")
        grid = QGridLayout(shortcuts)
        actions = (
            ("Roblox · توليد أو استيراد", lambda: self.open_platform(window.roblox, 0)),
            ("النتائج المحفوظة", window.job_manager.search_results),
            ("صانع ملفات اليوزرات", lambda: window.tabs.setCurrentWidget(window.file_generator)),
            ("العمليات والإيقاف والاستعادة", lambda: window.tabs.setCurrentWidget(window.job_manager)),
        )
        for index, (label, callback) in enumerate(actions):
            button = QPushButton(label)
            button.clicked.connect(callback)
            grid.addWidget(button, index // 2, index % 2)
        layout.addWidget(shortcuts)
        self.recent = QLabel()
        self.recent.setWordWrap(True)
        self.recent.setTextFormat(Qt.TextFormat.PlainText)
        recent = QGroupBox("العمليات والنتائج المعروضة مؤخرًا")
        recent_layout = QVBoxLayout(recent)
        recent_layout.addWidget(self.recent)
        note = QLabel("ملخص محدود من الصفحات المحمّلة؛ افتح إدارة العمليات أو النتائج للسجل الكامل.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        recent_layout.addWidget(note)
        layout.addWidget(recent)
        readiness = QGroupBox("المنصات وحدود التحقق")
        ready_layout = QVBoxLayout(readiness)
        for text in (
            "Roblox: تحقق تسجيل فردي أولي • Bulk دليل حسابات منفصل، والتصفية لا تثبت الإتاحة.",
            "X: تجريبي؛ الإتاحة غير مثبتة. تبقى قيود الموفر والاستعادة اليدوية نافذة.",
            "Instagram: تحقق الإتاحة معطّل؛ تحليل Osintgram يتطلب إعداده المستقل.",
            "سناب: البحث محاكاة والفحص الحقيقي معطّل؛ سناب سكور أداة مستقلة بتفويض صريح.",
        ):
            label = QLabel(text)
            label.setWordWrap(True)
            ready_layout.addWidget(label)
        layout.addWidget(readiness)
        refresh = QPushButton("تحديث الملخص المعروض")
        refresh.clicked.connect(self.refresh)
        layout.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addStretch()
        self.setWidget(body)
        self.update_timer = QTimer(self)
        self.update_timer.setSingleShot(True)
        self.update_timer.setInterval(150)
        self.update_timer.timeout.connect(self.refresh)
        for panel in (window.roblox, window.x_panel):
            panel.progress_changed.connect(self.queue_refresh)
            panel.idle.connect(self.queue_refresh)

    def open_platform(self, panel, page):
        panel.workspace.setCurrentIndex(page)
        self.window.tabs.setCurrentWidget(panel)

    def queue_refresh(self):
        if self.isVisible() and not self.update_timer.isActive():
            self.update_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        # No database queries, network access, queue scans, or repeating polling timer.
        window = self.window
        lines = []
        for name, panel in (("Roblox", window.roblox), ("X", window.x_panel)):
            state = "جارٍ الإيقاف" if panel.stopping and panel.busy else "نشط" if panel.busy else "غير نشط"
            detail = panel.activity_status.text() if panel.current_run else "لا عملية فردية محددة"
            if name == "Roblox" and panel.prefilter.busy:
                detail = "تصفية الملفات · " + panel.prefilter.status.text()
            elif name == "Roblox" and panel.bulk.busy:
                detail = "تجربة Bulk · " + panel.bulk.status.text()
            lines.append(f"{name}: {state} · {detail}")
            if not panel.busy and not panel.start_button.isEnabled():
                lines.append(f"{name}: البدء غير متاح حاليًا؛ راجع العملية وقيودها في صفحة المنصة.")
        if not window.x_panel.restriction.isHidden():
            lines.append(window.x_panel.restriction.text())
        if window.is_running():
            lines.append("سناب: محاكاة قيد التشغيل")
        self.activity.setText("\n".join(lines))
        rows = window.job_manager.rows[:4]
        recent = [
            f"{r['platform']} · {r['id'][:8]} · {r['display_state']} · "
            + (f"{r['done']}/{r['total']}" if r["total"] is not None else "التحضير غير مكتمل")
            for r in rows
        ]
        for platform, table in (("Roblox", window.roblox.result_table), ("X", window.x_panel.table)):
            for row in range(min(2, table.rowCount())):
                name, result = table.item(row, 0), table.item(row, 1)
                if name and result:
                    recent.append(f"{platform} · {name.text()} · {result.text()}")
        self.recent.setText("\n".join(recent) or "لا توجد عمليات أو نتائج محمّلة بعد.")
