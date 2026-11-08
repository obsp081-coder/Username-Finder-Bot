"""Render Roblox on native Windows with an isolated DB and fake HTTP only."""

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
    from osintgram_fixture import MemoryCredentials, MemorySettings
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from test_roblox import response

    from username_finder.gui import MainWindow
    from username_finder.osintgram_backend import BackendManager
    from username_finder.roblox_runner import RobloxRunner

    folder = ROOT / "runtime" / ("roblox-render-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    folder.mkdir()
    app = QApplication([])
    app.setFont(QFont("Segoe UI", 10))
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    manager = BackendManager(
        settings=MemorySettings(directory=str(folder / "unused")), credentials=MemoryCredentials()
    )
    window = MainWindow(folder / "isolated.db", osintgram_options={"manager": manager})
    window.timer.stop()
    panel = window.roblox
    panel.fetcher = lambda _: response()
    before = window.editor_signature()
    original_pages = [window.search_tabs.widget(i) for i in range(4)]
    report = {"platform": app.platformName(), "fake_http_only": True, "live_checks": 0, "sizes": []}
    try:
        names = [
            "visualAvailable",
            "visualChanged",
            "visualTaken",
            "visualReserved",
            "visualInvalid",
            "visualUnknown",
        ]
        codes = iter([0, 1, 1, 12, 2, 99])
        identity = panel.repo.create_run(names)
        RobloxRunner(panel.repo, fetcher=lambda _: response(next(codes)), wait=lambda _: False).run(identity)
        changed = panel.repo.create_run(["visualChanged"], kind="recheck_selected")
        RobloxRunner(panel.repo, fetcher=lambda _: response(), wait=lambda _: False).run(changed)
        panel.current_run = changed
        panel.refresh()
        window.tabs.setCurrentWidget(panel)
        window.show()
        for width, height in ((900, 650), (1180, 850)):
            window.resize(width, height)
            panel.modes.setCurrentIndex(0)
            panel.verticalScrollBar().setValue(0)
            QTest.qWait(80)
            window.grab().save(str(folder / f"roblox-generation-{width}.png"))
            panel.modes.setCurrentIndex(1)
            panel.manual.setPlainText("visualAvailable\nnewCandidate\nNEWCANDIDATE\ninvalid-name")
            panel.preview()
            for _ in range(100):
                QTest.qWait(10)
                if not panel.busy:
                    break
            assert not panel.busy
            QTest.qWait(40)
            window.grab().save(str(folder / f"roblox-input-{width}.png"))
            panel.verticalScrollBar().setValue(panel.verticalScrollBar().maximum())
            QTest.qWait(40)
            window.grab().save(str(folder / f"roblox-results-{width}.png"))
            report["sizes"].append(
                {
                    "width": window.width(),
                    "height": window.height(),
                    "dpr": window.devicePixelRatioF(),
                    "horizontal_overflow": panel.horizontalScrollBar().maximum(),
                    "vertical_scroll": panel.verticalScrollBar().maximum(),
                }
            )
            assert panel.horizontalScrollBar().maximum() == 0
        report.update(
            top_tabs=[window.tabs.tabText(i) for i in range(window.tabs.count())],
            settings_unchanged=before == window.editor_signature(),
            original_search_pages_unchanged=original_pages
            == [window.search_tabs.widget(i) for i in range(4)],
            snapchat_jobs=len(window.store.jobs()),
            mock_latest_counts=panel.repo.counts(),
            mock_history_count=len(panel.repo.history("visualChanged")),
            instagram_disabled=not window.instagram.start_button.isEnabled(),
            backend_state=manager.state,
        )
        assert report["settings_unchanged"] and report["snapchat_jobs"] == 0
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
