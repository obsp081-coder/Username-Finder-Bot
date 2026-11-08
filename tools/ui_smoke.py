"""Capture updated Qt screens using a separate simulated database."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pagination_profile import seed
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from username_finder.gui import MainWindow


def main():
    directory = Path("runtime") / ("ui-usability-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    directory.mkdir(parents=True)
    job = seed(directory / "smoke.db", 200)
    app = QApplication([])
    app.setFont(QFont("Segoe UI", 10))
    ui = MainWindow(directory / "smoke.db")
    ui.timer.stop()
    ui.load_job(job)
    ui.show()
    for width, height in [(900, 650), (1180, 850)]:
        ui.resize(width, height)
        ui.search_tabs.setCurrentIndex(0)
        ui.advanced_toggle.setChecked(False)
        QTest.qWait(50)
        ui.grab().save(str(directory / f"basic-{width}.png"))
    ui.advanced_toggle.setChecked(True)
    for index, name in enumerate(["generation", "filters", "performance", "schedule"]):
        ui.advanced_tabs.setCurrentIndex(index)
        QTest.qWait(40)
        ui.search_tabs.widget(0).ensureWidgetVisible(ui.advanced_tabs)
        QTest.qWait(40)
        ui.grab().save(str(directory / f"advanced-{name}.png"))
    for index, name in [(1, "execution"), (2, "results"), (3, "history")]:
        ui.search_tabs.setCurrentIndex(index)
        QTest.qWait(40)
        ui.grab().save(str(directory / f"{name}.png"))
    print("SCREENSHOTS=" + str(directory))
    ui.close()
    app.processEvents()


if __name__ == "__main__":
    main()
