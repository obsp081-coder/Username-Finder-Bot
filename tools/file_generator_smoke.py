"""Render only owned Windows test windows with an isolated DB; no availability requests."""

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
    from osintgram_fixture import MemoryCredentials, MemorySettings
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from username_finder.gui import MainWindow
    from username_finder.osintgram_backend import BackendManager

    folder = ROOT / "runtime" / ("file-generator-render-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    folder.mkdir()
    app = QApplication([])
    app.setFont(QFont("Segoe UI", 10))
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    manager = BackendManager(settings=MemorySettings(), credentials=MemoryCredentials())
    window = MainWindow(folder / "isolated.db", osintgram_options={"manager": manager})
    window.timer.stop()
    pages = [window.search_tabs.widget(i) for i in range(4)]
    before = window.editor_signature()
    panel = window.file_generator
    window.tabs.setCurrentWidget(panel)
    report = {"platform": app.platformName(), "live_checks": 0, "sizes": []}
    try:
        window.show()
        for width, height in ((900, 650), (1180, 850)):
            window.resize(width, height)
            for index, platform in ((0, "Instagram"), (1, "Valorant")):
                panel.categories.setCurrentIndex(index)
                panel.platforms[index].setCurrentText(platform)
                panel.advanced_toggle.setChecked(False)
                before_rules = panel.primary.rule(), panel.secondary.rule()
                panel.verticalScrollBar().setValue(0)
                QTest.qWait(80)
                window.grab().save(str(folder / f"{platform}-{width}.png"))
                report["sizes"].append(
                    {
                        "width": window.width(),
                        "height": window.height(),
                        "dpr": window.devicePixelRatioF(),
                        "profile": panel.profile.key,
                        "horizontal_overflow": panel.horizontalScrollBar().maximum(),
                    }
                )
                assert panel.horizontalScrollBar().maximum() == 0
                panel.verticalScrollBar().setValue(panel.verticalScrollBar().maximum())
                QTest.qWait(40)
                window.grab().save(str(folder / f"{platform}-output-{width}.png"))
                panel.advanced_toggle.setChecked(True)
                QTest.qWait(50)
                panel.verticalScrollBar().setValue(panel.verticalScrollBar().maximum())
                QTest.qWait(50)
                window.grab().save(str(folder / f"{platform}-advanced-{width}.png"))
                assert panel.horizontalScrollBar().maximum() == 0
                panel.advanced_toggle.setChecked(False)
                assert before_rules == (panel.primary.rule(), panel.secondary.rule())
        report.update(
            settings_unchanged=before == window.editor_signature(),
            search_pages_unchanged=pages == [window.search_tabs.widget(i) for i in range(4)],
            jobs=len(window.store.jobs()),
            roblox_results=window.roblox.repo.results()[1],
            social_count=panel.platforms[0].count(),
            gaming_count=panel.platforms[1].count(),
            advanced_toggle_preserved_values=True,
        )
        assert report["settings_unchanged"] and report["search_pages_unchanged"]
        assert report["jobs"] == report["roblox_results"] == 0
    finally:
        window.close()
        for _ in range(100):
            QTest.qWait(10)
            if window.closed:
                break
        report["window_closed"] = window.closed
        (folder / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(folder)


if __name__ == "__main__":
    main()
