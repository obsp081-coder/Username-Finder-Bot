"""Render Base Mode against synthetic local HTTP only, using an isolated database."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--visible", action="store_true")
    args = parser.parse_args()
    if not args.visible:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from osintgram_fixture import FakeBackend, MemoryCredentials, MemorySettings
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtGui import QFont
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from username_finder.gui import MainWindow
    from username_finder.osintgram_backend import BackendManager

    directory = ROOT / "runtime" / ("osintgram-render-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    directory.mkdir()
    app = QApplication([])
    app.setFont(QFont("Segoe UI", 10))
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    backend = FakeBackend()
    backend.tools = backend.tools + [
        {"name": "get_account_about", "params": [], "needs_target": True},
        {"name": "get_suggested_profiles", "params": [], "needs_target": True},
    ]
    backend.identity_backend = "instagrapi"
    backend.results["get_user_info"].update(
        biography="",
        follower_count=1234567,
        following_count=0,
        media_count=None,
        is_business=False,
        account_type=2,
        public_email="",
        public_phone_number=None,
        profile_pic_url_hd="",
        category="",
        external_url="",
    )
    manager = BackendManager(settings=MemorySettings(), credentials=MemoryCredentials())
    ui = MainWindow(directory / "isolated.db", osintgram_options={"manager": manager})
    ui.timer.stop()
    ui.tabs.setCurrentWidget(ui.instagram)
    ui.show()
    instagram = ui.instagram
    panel = instagram.analysis
    panel.client._url = lambda path: QUrl(backend.url + path)
    panel.manager._url = lambda path: QUrl(backend.url + path)
    report = {"platform": app.platformName(), "fixture_only": True, "sizes": []}

    def wait_for(predicate):
        for _ in range(400):
            if predicate():
                return
            QTest.qWait(10)
        raise AssertionError("Timed out waiting for synthetic backend")

    def analysis_top():
        instagram.verticalScrollBar().setValue(0)
        QTest.qWait(25)

    try:
        instagram.modes.setCurrentIndex(1)
        wait_for(lambda: panel.state == "connected")
        panel.target.setText("fixture_user")
        first = None
        for i in range(panel.commands.topLevelItemCount()):
            group = panel.commands.topLevelItem(i)
            for j in range(group.childCount()):
                item = group.child(j)
                name = item.data(0, Qt.ItemDataRole.UserRole)
                if name in ("get_user_info", "get_followers", "future_complex_tool"):
                    item.setCheckState(0, Qt.CheckState.Checked)
                if name == "get_followers":
                    first = item
        panel.commands.setCurrentItem(first)
        panel.start_analysis()
        wait_for(lambda: not panel.running and not panel.client.busy)
        assert panel.outcome == "complete"
        for name, size in [("900", (900, 650)), ("1180", (1180, 850)), ("maximized", None)]:
            if size:
                ui.resize(*size)
            else:
                ui.showMaximized()
            QTest.qWait(60)
            panel.json_toggle.setChecked(False)
            panel.pages.setCurrentIndex(0)
            analysis_top()
            ui.grab().save(str(directory / f"osintgram-setup-{name}.png"))
            center = panel.setup.save.mapTo(instagram.widget(), panel.setup.save.rect().center())
            instagram.ensureVisible(center.x(), center.y(), 0, 20)
            QTest.qWait(25)
            ui.grab().save(str(directory / f"osintgram-setup-actions-{name}.png"))
            panel.pages.setCurrentIndex(1)
            analysis_top()
            panel.commands.scrollToTop()
            panel.commands.setFocus()
            QTest.qWait(25)
            ui.grab().save(str(directory / f"osintgram-controls-{name}.png"))
            center = panel.commands.mapTo(instagram.widget(), panel.commands.rect().center())
            instagram.ensureVisible(center.x(), center.y(), 0, 20)
            QTest.qWait(25)
            ui.grab().save(str(directory / f"osintgram-checkboxes-{name}.png"))
            panel.commands.setEnabled(False)
            QTest.qWait(25)
            ui.grab().save(str(directory / f"osintgram-disabled-{name}.png"))
            panel.commands.setEnabled(True)
            instagram.ensureWidgetVisible(panel.parameter_scroll)
            QTest.qWait(25)
            ui.grab().save(str(directory / f"osintgram-parameters-{name}.png"))
            for index, result_name in enumerate(("profile", "list", "complex")):
                panel.pages.setCurrentIndex(2)
                panel.result_picker.setCurrentIndex(index)
                panel.json_toggle.setChecked(result_name == "complex")
                QTest.qWait(25)
                if result_name == "profile":
                    analysis_top()
                    ui.grab().save(str(directory / f"osintgram-summary-{name}.png"))
                instagram.ensureWidgetVisible(panel.table if result_name != "complex" else panel.json_view)
                if result_name == "complex":
                    # QTextEdit's focus rectangle follows its cursor, not its full editor.
                    instagram.verticalScrollBar().setValue(instagram.verticalScrollBar().maximum())
                QTest.qWait(30)
                ui.grab().save(str(directory / f"osintgram-{result_name}-{name}.png"))
            assert instagram.horizontalScrollBar().maximum() == 0
            report["sizes"].append(
                {
                    "label": name,
                    "width": ui.width(),
                    "height": ui.height(),
                    "columns": panel._columns,
                    "horizontal_overflow": 0,
                    "vertical_scroll_maximum": instagram.verticalScrollBar().maximum(),
                }
            )
            for tab, title in ((0, "settings"), (2, "results"), (3, "history")):
                ui.tabs.setCurrentWidget(ui.snapchat_tabs)
                ui.search_tabs.setCurrentIndex(tab)
                QTest.qWait(25)
                ui.grab().save(str(directory / f"application-{title}-{name}.png"))
            ui.tabs.setCurrentWidget(ui.instagram)
        report.update(
            dpr=ui.devicePixelRatioF(),
            tabs=ui.tabs.count(),
            jobs=len(ui.store.jobs()),
            results=len(panel.result_items),
            availability_rows=instagram.result_table.rowCount(),
            availability_disabled=not instagram.start_button.isEnabled(),
            outcome=panel.outcome,
            requests=[{"method": method, "path": path} for method, path, body in backend.calls],
        )
    finally:
        ui.close()
        wait_for(lambda: ui.closed)
        backend.close()
        report["window_closed"] = ui.closed
        (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(directory)


if __name__ == "__main__":
    main()
