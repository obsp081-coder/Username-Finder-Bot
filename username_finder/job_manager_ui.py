"""Read-only, paged job summaries with actions delegated to the existing platform panels."""

import sqlite3

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from . import x
from .overnight import STATES as OVERNIGHT_STATES
from .overnight import OvernightStore
from .platform_workspace import ReadableColumns
from .roblox_ui import STATES as ROBLOX_STATES
from .ui_cards import Card, caption, icon
from .windows_awake import AwakeRequest
from .x_ui import STATES as X_STATES


class JobManagerPanel(QWidget):
    view_changed = Signal()
    page_size = 20

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.panels = {"Roblox": window.roblox, "X": window.x_panel}
        self.legacy_night = OvernightStore(window.store, "Roblox")
        self.rows = []
        self.offset = 0
        self.awake = AwakeRequest()
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(8)
        layout = root
        header = QHBoxLayout()
        header.addWidget(caption("إدارة العمليات", "pageTitle"), 1)
        self.summary = caption("", "statusPill")
        header.addWidget(self.summary)
        layout.addLayout(header)
        layout.addWidget(
            caption("تابع التقدم المحفوظ، وافتح العملية أو أوقفها بأمان. عمل واحد لكل منصة.", "pageSubtitle")
        )
        toolbar = QHBoxLayout()
        self.search_results_button = QPushButton("سجل النتائج المحفوظة")
        self.search_results_button.setIcon(icon("file", size=19))
        self.search_results_button.clicked.connect(self.search_results)
        toolbar.addWidget(self.search_results_button)
        if not hasattr(window.roblox, "overnight"):
            rovalid_history = QPushButton("سجل RoValid المستقل")
            rovalid_history.clicked.connect(self.open_rovalid_history)
            toolbar.addWidget(rovalid_history)
        self.preflight_button = QPushButton("جاهزية التشغيل الليلي")
        self.preflight_button.setIcon(icon("check", size=19))
        self.preflight_button.clicked.connect(self.preflight)
        toolbar.addWidget(self.preflight_button)
        self.power_button = QPushButton("الطاقة")
        self.power_menu = QMenu(self)
        self.power_button.setMenu(self.power_menu)
        toolbar.addWidget(self.power_button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        table_card = Card("العمليات المحفوظة", "layers", shadow=True)
        self.table = window.make_table(
            ["المنصة", "العملية", "الحالة", "المكتمل / الإجمالي", "تجاوز سابق", "الفاصل"]
        )
        self.table.setMinimumHeight(100)
        table_card.body.setAlignment(Qt.AlignmentFlag(0))
        self.table.itemSelectionChanged.connect(self.show_selected)
        self.columns = ReadableColumns(self.table, window.fill_table, fill_width=True)
        table_card.body.addWidget(self.table, 1)
        self.empty = caption("لا توجد عمليات بعد.\nافتح Roblox أو X لتحضير عملية جديدة.", "emptyTitle")
        self.empty.setMinimumHeight(100)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        table_card.body.addWidget(self.empty)
        layout.addWidget(table_card, 1)
        self.details = caption("حدد عملية لعرض نتائجها واتصالها وأي قيد محفوظ.", "selectionDetails")
        self.detail_area = QScrollArea()
        self.detail_area.setWidgetResizable(True)
        self.detail_area.setFixedHeight(50)
        self.detail_area.setWidget(self.details)
        power_content = QWidget()
        power_layout = QVBoxLayout(power_content)
        power_content.setMaximumWidth(420)
        power_action = QWidgetAction(self.power_menu)
        power_action.setDefaultWidget(power_content)
        self.power_menu.addAction(power_action)
        self.keep_awake = QCheckBox("منع السكون التلقائي أثناء التشغيل الليلي")
        self.keep_awake.setToolTip(
            "اختياري لهذه الجلسة؛ لا يمنع إطفاء الشاشة أو السكون اليدوي أو إغلاق الغطاء."
        )
        self.keep_awake.toggled.connect(self.update_power)
        power_layout.addWidget(self.keep_awake)
        self.sleep_status = caption("")
        power_layout.addWidget(self.sleep_status)
        root.addWidget(self.detail_area)
        actions = QGridLayout()
        self.open_button = QPushButton("فتح المنصة")
        self.start_button = QPushButton("بدء / استئناف")
        self.start_button.setToolTip("بدء أو استئناف العملية المحددة فقط")
        self.start_button.setObjectName("primary")
        self.stop_button = QPushButton("إيقاف المحددة")
        self.stop_button.setObjectName("danger")
        self.overnight_button = QPushButton("تشغيل ليلي")
        self.overnight_button.setToolTip("إعداد التشغيل الليلي للعملية المحددة")
        for index, (button, callback) in enumerate(
            (
                (self.open_button, self.open_selected),
                (self.start_button, self.start_selected),
                (self.stop_button, self.stop_selected),
                (self.overnight_button, self.overnight_selected),
            )
        ):
            button.clicked.connect(callback)
            actions.addWidget(button, 0, index)
        root.addLayout(actions)
        pages = QHBoxLayout()
        self.previous_button, self.next_button = QPushButton("السابق"), QPushButton("التالي")
        self.previous_button.clicked.connect(lambda: self.page(-1))
        self.next_button.clicked.connect(lambda: self.page(1))
        self.page_label = caption("")
        pages.addWidget(self.previous_button)
        pages.addWidget(self.page_label, 1)
        pages.addWidget(self.next_button)
        root.addLayout(pages)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh_if_visible)
        self.timer.timeout.connect(self.update_power)
        self.timer.start()
        self.update_timer = QTimer(self)
        self.update_timer.setSingleShot(True)
        self.update_timer.setInterval(100)
        self.update_timer.timeout.connect(self.refresh_if_visible)
        for panel in self.panels.values():
            panel.progress_changed.connect(self.request_refresh)
            panel.idle.connect(self.request_refresh)
            panel.progress_changed.connect(self.update_power)
            panel.idle.connect(self.update_power)
        QApplication.instance().aboutToQuit.connect(self.shutdown)
        self.refresh()
        self.update_power()

    def open_rovalid_history(self):
        self.window.tabs.setCurrentWidget(self.window.roblox)
        self.window.roblox.workspace.setCurrentIndex(3)

    def preflight(self):
        from .overnight_preflight_ui import OvernightPreflightDialog

        OvernightPreflightDialog(self).exec()

    def update_power(self):
        if self.awake.closed:
            return
        required = False
        if self.keep_awake.isChecked() and not self.window.closing and not self.window.closed:
            try:
                for panel in self.panels.values():
                    if (
                        not hasattr(panel, "overnight")
                        or not panel.busy
                        or panel.stopping
                        or not panel.runner
                        or panel.runner.cancel.is_set()
                    ):
                        continue
                    data = panel.overnight.repo.get(panel.current_run) or {}
                    if (
                        data.get("enabled")
                        and data.get("auto_resume")
                        and data.get("state") in ("Running", "Connectivity", "Delay")
                        and not panel.repo.control().get("blocked")
                    ):
                        required = True
                        break
            except (ValueError, TypeError, OSError, sqlite3.Error):
                # An unreadable job cannot justify keeping the machine awake.
                required = False
        self.awake.update(required)
        if self.awake.error:
            text = self.awake.error
        elif not self.awake.api:
            text = "منع السكون غير مدعوم؛ راجع إعدادات الجهاز يدويًا."
        elif self.awake.active:
            text = "منع السكون نشط للعمل الليلي؛ يمكن للشاشة أن تنطفئ."
        elif self.keep_awake.isChecked():
            text = "منع السكون مهيأ لهذه الجلسة؛ ينشط مع عمل ليلي مؤهل فقط."
        else:
            text = "منع السكون غير مفعّل؛ لن يستمر الفحص أثناء سكون الجهاز أو إيقافه."
        self.sleep_status.setText(text)

    def shutdown(self):
        self.timer.stop()
        self.update_timer.stop()
        self.awake.close()

    def search_results(self):
        from .saved_results_ui import SavedResultsDialog

        dialog = SavedResultsDialog(self.window)
        dialog.exec()

    def overnight_selected(self):
        row = self.selected()
        if row and (row["platform"] != "Roblox" or hasattr(self.window.roblox, "overnight")):
            self.open_selected()
            controls = self.panels[row["platform"]].overnight
            controls.enabled.setFocus()

    def snapshot(self):
        store = self.window.store
        with store.lock:
            total = sum(
                store.db.execute(f"SELECT COUNT(*) FROM {prefix}_runs").fetchone()[0]
                for prefix in ("roblox", "x")
            )
            records = store.db.execute(
                "SELECT 'Roblox' AS platform,id,created_at,state FROM roblox_runs UNION ALL "
                "SELECT 'X' AS platform,id,created_at,state FROM x_runs ORDER BY created_at DESC,id "
                "LIMIT ? OFFSET ?",
                (self.page_size, self.offset),
            ).fetchall()
            classifications = {}
            for platform, prefix in (("Roblox", "roblox"), ("X", "x")):
                identities = [r["id"] for r in records if r["platform"] == platform]
                if not identities:
                    continue
                placeholders = ",".join("?" for _ in identities)
                classification_sql = x.CLASSIFICATION_SQL if platform == "X" else "c.classification"
                for identity, classification, count in store.db.execute(
                    f"SELECT run_id,({classification_sql}),COUNT(*) FROM {prefix}_checks c "
                    f"WHERE run_id IN ({placeholders}) GROUP BY run_id,({classification_sql})",
                    identities,
                ):
                    classifications.setdefault((platform, identity), {})[classification] = count
            rows = []
            for record in records:
                platform, identity = record["platform"], record["id"]
                panel = self.panels[platform]
                local_preparation = record["state"] in (
                    "Preparing",
                    "PreparationPaused",
                    "PreparationError",
                )
                run = (
                    panel.repo.preparation_progress(identity)
                    if local_preparation
                    else panel.repo.progress(identity)
                )
                counts = classifications.get((platform, identity), {})
                state = (ROBLOX_STATES if platform == "Roblox" else X_STATES).get(run["state"], run["state"])
                blocked = panel.repo.control().get("blocked")
                if panel.busy and panel.current_run == identity and panel.stopping:
                    state = "جارٍ الإيقاف"
                elif run["state"] == "Ready" and not run["total"]:
                    state = "لا أسماء مؤهلة"
                elif blocked and run["state"] in ("Ready", "Paused"):
                    state = "ممنوعة بقيد المنصة"
                night_repo = panel.overnight.repo if hasattr(panel, "overnight") else self.legacy_night
                night = night_repo.get(identity)
                if night and night["enabled"]:
                    if panel.busy and panel.current_run == identity and not panel.stopping:
                        state = OVERNIGHT_STATES.get(night["state"], state)
                    elif run["state"] == "Paused" and night["state"] in ("Stopped", "Interrupted", "Review"):
                        state = OVERNIGHT_STATES[night["state"]]
                route = panel.repo.connection_for_run(identity) if platform == "X" else {"mode": "direct"}
                done = run["completed"] if platform == "X" else run["counts"].get("Done", 0)
                rows.append(
                    dict(
                        run,
                        platform=platform,
                        display_state=state,
                        done=done,
                        classifications=counts,
                        route=route,
                        restriction=str(blocked) if blocked else "",
                        overnight=night_repo.label(identity),
                    )
                )
            return rows, total

    def refresh_if_visible(self):
        if self.isVisible() and not self.window.busy and not self.window.closing and not self.window.closed:
            self.refresh()

    def request_refresh(self):
        if self.isVisible() and not self.update_timer.isActive():
            self.update_timer.start()

    def refresh(self):
        selected = self.selected()
        key = (selected["platform"], selected["id"]) if selected else None
        self.rows, total = self.snapshot()
        self.columns.fill(
            [
                [
                    "Roblox · أرشيف"
                    if r["platform"] == "Roblox" and not hasattr(self.window.roblox, "overnight")
                    else r["platform"],
                    r["id"][:8],
                    r["display_state"],
                    f"{r['done']}/{r['total']}" if r["total"] is not None else "التحضير غير مكتمل",
                    r["skipped"],
                    f"{r['interval_seconds']:g} ثانية",
                ]
                for r in self.rows
            ],
        )
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.empty.setVisible(not self.rows)
        self.table.setVisible(bool(self.rows))
        for index, row in enumerate(self.rows):
            self.table.item(index, 1).setToolTip(row["id"])
            if row["state"] == "Running" and row["total"] > 0:
                progress = QProgressBar()
                progress.setMinimumWidth(0)
                progress.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                progress.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
                progress.setRange(0, 1000)
                progress.setValue(min(1000, row["done"] * 1000 // row["total"]))
                progress.setFormat(f"{row['done']} / {row['total']}")
                progress.setToolTip("تقدم محفوظ فعليًا؛ يتضمن النتائج غير المحسومة.")
                self.table.setCellWidget(index, 3, progress)
            elif self.table.cellWidget(index, 3):
                self.table.removeCellWidget(index, 3)
            if (row["platform"], row["id"]) == key:
                self.table.setCurrentCell(index, 0)
        self.page_label.setText(
            f"{self.offset + 1 if total else 0}–{min(total, self.offset + len(self.rows))} من {total}"
        )
        self.previous_button.setEnabled(self.offset > 0)
        self.next_button.setEnabled(self.offset + len(self.rows) < total)
        self.summary.setText(
            " • ".join(
                f"{name}: "
                + (
                    "جارٍ الإيقاف"
                    if panel.stopping and panel.busy
                    else "نشطة"
                    if panel.busy
                    else "متوقفة بقيد"
                    if panel.repo.control().get("blocked")
                    else "خاملة"
                )
                for name, panel in self.panels.items()
            )
        )
        self.show_selected()
        self.view_changed.emit()

    def selected(self):
        index = self.table.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def show_selected(self):
        row = self.selected()
        self.open_button.setEnabled(bool(row))
        self.overnight_button.setEnabled(bool(row))
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        if not row:
            self.details.setText("حدد عملية لعرض نتائجها واتصالها وأي قيد محفوظ.")
            return
        panel = self.panels[row["platform"]]
        counts = row["classifications"]
        self.details.setText(
            "نتائج العملية: "
            + " • ".join(
                f"{name}: {counts.get(key, 0)}"
                for key, name in (
                    ("Available", "متاح أوليًا"),
                    ("Taken", "مستخدم"),
                    ("Reserved", "محجوز/مرفوض"),
                    ("Invalid", "غير صالح"),
                    ("Unknown", "غير محسوم"),
                )
            )
            + "\nالاتصال: "
            + ("مباشر" if row["route"]["mode"] == "direct" else row["route"]["proxy_url"])
            + " • آخر خطأ/قيد: "
            + (row["error"] or row["restriction"] or "لا يوجد")
            + "\n"
            + row["overnight"]
        )
        if row["platform"] == "Roblox" and not hasattr(panel, "overnight"):
            self.overnight_button.setEnabled(False)
            self.details.setText(
                self.details.text() + "\nأرشيف Roblox القديم للقراءة فقط؛ لا يُشغّل عبر RoValid."
            )
            return
        eligible = row["state"] in ("Ready", "Paused", "PreparationPaused", "PreparationError")
        if row["platform"] == "Roblox":
            eligible = eligible or row["state"] in ("Restricted", "Error")
        else:
            eligible = eligible and panel.recovery.can_start(row["id"])
        pending = row.get("pending", row.get("counts", {}).get("Pending", 0))
        eligible = eligible and (pending > 0 or row["state"].startswith("Preparation"))
        self.start_button.setEnabled(
            bool(eligible and not panel.busy and not panel.closing and not row["restriction"])
        )
        self.stop_button.setEnabled(panel.busy and panel.current_run == row["id"])

    def start_selected(self):
        row = self.selected()
        if not row:
            return
        panel = self.panels[row["platform"]]
        if row["platform"] == "Roblox" and not hasattr(panel, "overnight"):
            return
        if panel.busy or panel.closing:
            return
        panel.current_run = row["id"]
        panel.refresh()
        if panel.resume_button.isEnabled():
            panel.resume()
        self.refresh()

    def stop_selected(self):
        row = self.selected()
        if row:
            panel = self.panels[row["platform"]]
            if row["platform"] == "Roblox" and not hasattr(panel, "overnight"):
                return
            if panel.busy and panel.current_run == row["id"]:
                panel.stop()
        self.refresh()

    def open_selected(self):
        row = self.selected()
        if row:
            panel = self.panels[row["platform"]]
            if row["platform"] == "Roblox" and not hasattr(panel, "overnight"):
                self.window.tabs.setCurrentWidget(panel)
                panel.workspace.setCurrentIndex(3)
                return
            if not panel.busy:
                panel.current_run = row["id"]
                panel.refresh()
            self.window.tabs.setCurrentWidget(panel)
            panel.workspace.setCurrentIndex(2)

    def page(self, direction):
        self.offset = max(0, self.offset + direction * self.page_size)
        self.refresh()
