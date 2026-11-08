"""Render the isolated Instagram panel and close only this test window."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--visible", action="store_true")
    args = parser.parse_args()
    if not args.visible:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog

    from username_finder.gui import MainWindow

    directory = Path("runtime") / (
        "instagram-render-" + datetime.now(ZoneInfo("Asia/Riyadh")).strftime("%Y%m%d-%H%M%S")
    )
    directory.mkdir()
    app = QApplication([])
    app.setFont(QFont("Segoe UI", 10))
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    ui = MainWindow(directory / "isolated.db")
    ui.timer.stop()
    ui.tabs.setCurrentWidget(ui.instagram)
    ui.show()
    report = {"platform": app.platformName(), "sizes": []}
    panel = ui.instagram
    sample = directory / "instagram-names.txt"
    sample.write_text("example_1\n123_example\nbad-name\n", encoding="utf-8")
    for label, size in [("900", (900, 650)), ("1180", (1180, 850)), ("maximized", None)]:
        if size:
            ui.resize(*size)
        else:
            ui.showMaximized()
        QTest.qWait(50)
        panel.manual.clear()
        panel.files.clear()
        panel._update_files()
        panel.verticalScrollBar().setValue(0)
        QTest.qWait(20)
        ui.grab().save(str(directory / f"instagram-empty-{label}.png"))
        panel.manual.setText("@Example.user_1")
        original_picker = QFileDialog.getOpenFileNames
        QFileDialog.getOpenFileNames = lambda *args: ([str(sample)], "Text files (*.txt)")
        try:
            panel.add_files()
        finally:
            QFileDialog.getOpenFileNames = original_picker
        panel.preview()
        QTest.qWait(30)
        assert panel.files.viewport().height() >= panel.files.sizeHintForRow(0)
        panel.verticalScrollBar().setValue(0)
        ui.grab().save(str(directory / f"instagram-top-{label}.png"))
        panel.ensureWidgetVisible(panel.result_table)
        QTest.qWait(20)
        ui.grab().save(str(directory / f"instagram-results-{label}.png"))
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.start_button.width() == 160
        report["sizes"].append(
            {
                "size": label,
                "width": ui.width(),
                "height": ui.height(),
                "horizontal_overflow": False,
                "input_columns": panel._input_columns,
                "body_width": panel.body.width(),
                "table_height": panel.result_table.height(),
                "vertical_range": panel.verticalScrollBar().maximum(),
            }
        )
    report.update(
        dpr=ui.devicePixelRatioF(),
        tabs=ui.tabs.count(),
        jobs=len(ui.store.jobs()),
        result_rows=ui.instagram.result_table.rowCount(),
        start_disabled=not ui.instagram.start_button.isEnabled(),
    )
    ui.close()
    app.processEvents()
    report["test_window_closed"] = ui.closed and not ui.isVisible()
    assert report["test_window_closed"]
    (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(directory)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
