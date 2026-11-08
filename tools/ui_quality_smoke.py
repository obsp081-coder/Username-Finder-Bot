"""Short isolated Qt diagnosis/rendering check; never opens the user's database."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--label", default="before")
    args = parser.parse_args()
    if not args.visible:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QFont, QPalette, QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea

    from username_finder.gui import MainWindow

    directory = Path("runtime") / (
        "ui-quality-" + args.label + "-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    )
    directory.mkdir(parents=True)
    app = QApplication([])
    app.setFont(QFont("Segoe UI", 10))
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    app.setQuitOnLastWindowClosed(False)
    ui = MainWindow(directory / "isolated.db")
    ui.timer.stop()
    ui.unsaved_choice = lambda: QMessageBox.StandardButton.Discard
    errors = []
    QMessageBox.warning = lambda *values: errors.append(str(values[-1]))
    report = {"platform": app.platformName(), "style": app.style().objectName(), "errors": errors}

    def finish_preview():
        deadline = time.monotonic() + 3
        while not ui.last_preview_diagnostics and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        assert ui.last_preview_diagnostics.get("counts", {}).get("eligible") == 3

    ui.resize(1180, 850)
    ui.show()
    ui.advanced_toggle.setChecked(True)
    ui.advanced_tabs.setCurrentIndex(2)
    QTest.qWait(40)
    outer = ui.search_tabs.widget(0)
    outer.verticalScrollBar().setValue(0)
    inner = ui.advanced_tabs.currentWidget()
    ui.ttl.setFocus()
    control = ui.workers
    areas = []
    parent = control.parentWidget()
    while parent:
        if isinstance(parent, QScrollArea):
            bar = parent.verticalScrollBar()
            areas.append({"maximum": bar.maximum(), "value": bar.value()})
        parent = parent.parentWidget()
    before = ui.editor_signature()
    position = control.rect().center()
    event = QWheelEvent(
        QPointF(position),
        QPointF(control.mapToGlobal(position)),
        QPoint(),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(control, event)
    report["wheel"] = {
        "areas_before": areas,
        "inner_after": inner.verticalScrollBar().value(),
        "outer_after": outer.verticalScrollBar().value(),
        "values_unchanged": before == ui.editor_signature(),
    }
    ui.advanced_toggle.setChecked(False)
    sample = directory / "known-good.txt"
    sample.write_text("aaa\naab\naac\n", encoding="utf-8")
    ui.source.setCurrentIndex(1)
    ui.files.addItem(str(sample.resolve()))
    ui.min_length.setValue(3)
    ui.max_length.setValue(3)
    ui.preview()
    finish_preview()
    report["preview_fresh"] = {"message": ui.preview_label.text(), "status": ui.statusBar().currentMessage()}
    ui.show_error("ValueError")
    ui.preview()
    finish_preview()
    report["preview_after_error"] = {
        "message": ui.preview_label.text(),
        "status": ui.statusBar().currentMessage(),
    }
    report["preview_diagnostics"] = ui.last_preview_diagnostics
    report["jobs_created"] = len(ui.store.jobs())
    ui.grab().save(str(directory / "txt-preview.png"))
    ui.source.setCurrentIndex(0)
    ui.preview_label.setText("ابدأ بضبط المصدر والطول والعدد، ثم عاين البحث قبل إنشائه.")
    ui.resize(900, 650)
    QTest.qWait(30)
    ui.grab().save(str(directory / "settings-medium.png"))
    ui.resize(1180, 850)
    QTest.qWait(30)
    ui.grab().save(str(directory / "settings-large.png"))
    ui.advanced_toggle.setChecked(True)
    ui.advanced_tabs.setCurrentIndex(2)
    QTest.qWait(30)
    ui.search_tabs.widget(0).verticalScrollBar().setValue(
        ui.search_tabs.widget(0).verticalScrollBar().maximum()
    )
    QTest.qWait(30)
    ui.grab().save(str(directory / "settings-advanced.png"))
    ui.advanced_toggle.setChecked(False)
    ui.search_tabs.widget(0).verticalScrollBar().setValue(0)
    ui.preset_selector.addItem("قالب تجريبي", "test-only")
    for name in ("source", "mode", "preset_selector"):
        widget = getattr(ui, name)
        widget.showPopup()
        QTest.qWait(40)
        view = widget.view()
        palette = view.palette()
        report[name] = {
            role.name: palette.color(QPalette.ColorGroup.Active, role).name()
            for role in [
                QPalette.ColorRole.Base,
                QPalette.ColorRole.Text,
                QPalette.ColorRole.Highlight,
                QPalette.ColorRole.HighlightedText,
            ]
        }
        view.window().grab().save(str(directory / f"popup-{name}.png"))
        widget.hidePopup()
    ui.showMaximized()
    QTest.qWait(50)
    ui.grab().save(str(directory / "settings-maximized.png"))
    report["screen_dpr"] = ui.devicePixelRatioF()
    ui.close()
    deadline = time.monotonic() + 3
    while not ui.closed and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    report["test_window_closed"] = ui.closed and not ui.isVisible()
    assert report["test_window_closed"]
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("REPORT=" + str(directory / "report.json"))
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
