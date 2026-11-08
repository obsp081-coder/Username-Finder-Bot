"""Native Qt rendering with an isolated database and a mock desktop only."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

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
    from snapscore_fixture import FakeDesktop, advance_clock, calibrate

    from username_finder.gui import MainWindow
    from username_finder.osintgram_backend import BackendManager
    from username_finder.snapscore_desktop import WindowsDesktop

    folder = ROOT / "runtime" / ("snapscore-render-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    folder.mkdir()
    app = QApplication([])
    app.setFont(QFont("Segoe UI", 10))
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    desktop = FakeDesktop()
    manager = BackendManager(
        settings=MemorySettings(directory=str(folder / "unused")), credentials=MemoryCredentials()
    )
    with patch("username_finder.snapscore_ui.WindowsDesktop", lambda: desktop):
        window = MainWindow(folder / "isolated.db", osintgram_options={"manager": manager})
    window.timer.stop()
    window.show()
    panel = window.snapscore
    original = window.editor_signature()
    pages = [window.search_tabs.widget(i) for i in range(4)]
    report = dict(platform=app.platformName(), mocked_desktop=True, real_input_sent=False, sizes=[])
    try:
        native = WindowsDesktop()
        native._check_platform()  # Read-only DPI-context check; no calibration, hotkey or SendInput.
        report["windows_input_structure_bytes"] = ctypes.sizeof(native.Input)
        report["per_monitor_dpi_context_verified"] = True
        for width, height in ((900, 650), (1180, 850)):
            window.resize(width, height)
            window.tabs.setCurrentWidget(window.snapchat_tabs)
            window.snapchat_tabs.setCurrentWidget(window.search_tabs)
            for index, name in enumerate(("settings", "run", "results", "history")):
                window.search_tabs.setCurrentIndex(index)
                QTest.qWait(30)
                window.grab().save(str(folder / f"search-{name}-{width}.png"))
            window.snapchat_tabs.setCurrentWidget(panel)
            calibrate(panel)
            panel.preview.click()
            panel.verticalScrollBar().setValue(0)
            QTest.qWait(30)
            window.grab().save(str(folder / f"snapscore-setup-{width}.png"))
            assert panel.horizontalScrollBar().maximum() == 0
            assert len(desktop.clicks) == 0 if width == 900 else len(desktop.clicks) == 1
            panel.ensureWidgetVisible(panel.stop)
            QTest.qWait(30)
            window.grab().save(str(folder / f"snapscore-preview-{width}.png"))
            panel.confirm = lambda _: True  # Mock consent for mock input; never invokes WindowsDesktop.
            panel.start.click()
            QTest.qWait(20)
            window.grab().save(str(folder / f"snapscore-running-{width}.png"))
            advance_clock(panel.session)
            assert panel.session.state == "awaiting" and panel.session.attempts == 1
            panel.ensureWidgetVisible(panel.counters)
            QTest.qWait(20)
            window.grab().save(str(folder / f"snapscore-paused-{width}.png"))
            panel.stop.click()
            assert not panel.session.active and desktop.callback is None
            for page, name in ((window.instagram, "instagram"), (window.file_search, "files")):
                window.tabs.setCurrentWidget(page)
                QTest.qWait(30)
                window.grab().save(str(folder / f"top-{name}-{width}.png"))
            report["sizes"].append(
                dict(
                    width=window.width(),
                    height=window.height(),
                    dpr=window.devicePixelRatioF(),
                    horizontal_overflow=0,
                    snapscore_vertical_scroll_maximum=panel.verticalScrollBar().maximum(),
                    mock_attempts=panel.session.attempts,
                )
            )
        report.update(
            top_tabs=[window.tabs.tabText(i) for i in range(window.tabs.count())],
            snapchat_tabs=[window.snapchat_tabs.tabText(i) for i in range(2)],
            search_pages_unchanged=pages == [window.search_tabs.widget(i) for i in range(4)],
            settings_unchanged=original == window.editor_signature(),
            jobs=len(window.store.jobs()),
            instagram_disabled=not window.instagram.start_button.isEnabled(),
            backend_state=manager.state,
            mock_clicks=len(desktop.clicks),
        )
        assert report["jobs"] == 0 and report["settings_unchanged"]
    finally:
        window.close()
        for _ in range(100):
            if window.closed:
                break
            QTest.qWait(10)
        report["window_closed"] = window.closed
        report["timers_stopped"] = (
            not panel.session.timer.isActive() and not panel.calibration_timer.isActive()
        )
        report["stop_released"] = desktop.callback is None
        (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(folder)


if __name__ == "__main__":
    main()
