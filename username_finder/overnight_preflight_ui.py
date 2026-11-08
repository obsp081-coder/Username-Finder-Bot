"""Small local-only preflight dialog using the existing local-data executor."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout

from .overnight_preflight import JobIntent, Readiness, check_job


class OvernightPreflightDialog(QDialog):
    def __init__(self, manager):
        super().__init__(manager)
        self.manager = manager
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setWindowTitle("فحص جاهزية التشغيل الليلي")
        self.resize(740, 510)
        self.checks = {}
        self.results = []
        self.identities = {name: panel.current_run for name, panel in manager.panels.items()}
        selected = manager.selected()
        if selected:
            self.identities[selected["platform"]] = selected["id"]
        layout = QVBoxLayout(self)
        note = QLabel(
            "حدد العمليات المقصودة أدناه. فحص محلي لحظي دون شبكة؛ لا يبدأ أي عمل ولا يضمن اتصالًا مستقبليًا."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        for name, identity in self.identities.items():
            box = QCheckBox(f"{name} • {identity or 'لم تُحدد عملية'}")
            box.setChecked(bool(identity))
            box.toggled.connect(self.invalidate)
            layout.addWidget(box)
            self.checks[name] = box
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)
        self.summary = QLabel("اضغط إجراء الفحص؛ لن تُغيّر الموافقة أو الحالة المحفوظة.")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        actions = QHBoxLayout()
        self.check_button = QPushButton("إجراء الفحص المحلي")
        self.check_button.clicked.connect(self.check_now)
        actions.addWidget(self.check_button)
        close = QPushButton("إغلاق")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)

    def invalidate(self):
        self.results = []
        self.output.clear()
        self.summary.setText("تغيّر الاختيار؛ أعد إجراء الفحص المحلي.")

    def check_now(self):
        window = self.manager.window
        if window.busy or window.closing or window.closed or sum(not f.done() for f in window.futures) >= 2:
            self.summary.setText("انتظر اكتمال تحضير البيانات أو الإغلاق قبل الفحص.")
            return
        items = []
        for name, box in self.checks.items():
            if box.isChecked():
                panel = self.manager.panels[name]
                items.append(
                    (
                        panel.repo,
                        panel.overnight.repo,
                        JobIntent(name, self.identities[name], panel.busy, panel.stopping, panel.closing),
                    )
                )
        if not items:
            self.summary.setText("محظورة: حدد منصة وعملية مقصودة واحدة على الأقل.")
            self.output.clear()
            self.results = []
            return
        self.output.clear()
        self.summary.setText("جارٍ فحص الحالة والتخزين محليًا…")
        self.check_button.setEnabled(False)
        for box in self.checks.values():
            box.setEnabled(False)

        def work():
            results = []
            for repo, night, intent in items:
                try:
                    results.append(check_job(repo, night, intent))
                except Exception:
                    results.append(
                        Readiness(intent.platform, intent.identity, errors=["تعذر إتمام الفحص المحلي."])
                    )
            return results

        def ready(results):
            self.results = results
            self.check_button.setEnabled(True)
            for box in self.checks.values():
                box.setEnabled(True)
            sleep_note = self.manager.sleep_status.text()
            if (
                not self.manager.keep_awake.isChecked()
                or not self.manager.awake.api
                or self.manager.awake.error
            ):
                for result in results:
                    result.warnings.append("راجع سكون Windows؛ منع السكون غير مفعّل أو غير مؤكد.")
            self.output.setPlainText("\n\n".join(result.text() for result in results) + "\n\n" + sleep_note)
            count = sum(not result.errors for result in results)
            self.summary.setText(
                f"مسموح محليًا: {count}/{len(results)}. "
                "ابدأ كل عملية مسموحة صراحةً من مدير العمليات؛ لم يبدأ الفحص تلقائيًا."
            )

        window.submit(work, ready)
