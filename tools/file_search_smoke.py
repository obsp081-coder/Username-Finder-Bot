"""Native Windows rendering with synthetic local files and an isolated database."""

from __future__ import annotations

import argparse
import hashlib
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
    parser.add_argument("--instagram-id", action="store_true")
    args = parser.parse_args()
    if not args.visible:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from osintgram_fixture import PROFILE, FakeBackend, MemoryCredentials, MemorySettings
    from PySide6.QtCore import QPoint, Qt, QUrl
    from PySide6.QtGui import QFont
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from username_finder.gui import MainWindow
    from username_finder.osintgram_backend import BackendManager

    prefix = "file-search-id-render-" if args.instagram_id else "file-search-render-"
    folder = ROOT / "runtime" / (prefix + datetime.now().strftime("%Y%m%d-%H%M%S"))
    folder.mkdir()
    path = folder / "synthetic.jsonl"
    line = '{"u":"example_user","id":"1234567890123456789","n":"حساب تجريبي🙂","e":"example@example.invalid"}'
    if args.instagram_id:
        line = line.replace("1234567890123456789", str(PROFILE["id"]))
    path.write_bytes(((line + "\r\n") * 5000).encode())
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    app = QApplication([])
    app.setFont(QFont("Segoe UI", 10))
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    manager = BackendManager(
        settings=MemorySettings(directory=str(folder / "unused-external")), credentials=MemoryCredentials()
    )
    window = MainWindow(folder / "isolated.db", osintgram_options={"manager": manager})
    window.timer.stop()
    panel = window.file_search
    backend = FakeBackend() if args.instagram_id else None
    if backend:
        manager._url = window.instagram.analysis.client._url = panel.resolver.client._url = lambda route: (
            QUrl(backend.url + route)
        )
    window.tabs.setCurrentWidget(panel)
    window.show()
    report = {
        "platform": app.platformName(),
        "synthetic_only": True,
        "mocked_resolution": args.instagram_id,
        "sizes": [],
    }

    def wait_for(predicate):
        for _ in range(500):
            if predicate():
                return
            QTest.qWait(10)
        raise AssertionError("Synthetic operation timed out")

    try:
        panel.set_files([path])
        panel.query.setText("example")
        if args.instagram_id:
            panel.mode.setCurrentIndex(1)
            panel.username.setText("@Fixture_User")
        panel.limit.setCurrentText("1,000")
        panel.start_search()
        wait_for(lambda: not panel.busy)
        assert panel.model.rowCount() == 1000 and panel.last_summary.status == "limit"
        for name, size in [("900", (900, 650)), ("1180", (1180, 850)), ("maximized", None)]:
            if size:
                window.resize(*size)
            else:
                window.showMaximized()
            QTest.qWait(60)
            panel.verticalScrollBar().setValue(0)
            window.grab().save(str(folder / f"search-controls-{name}.png"))
            panel.ensureWidgetVisible(panel.table)
            QTest.qWait(25)
            window.grab().save(str(folder / f"search-results-{name}.png"))
            panel.table.setCurrentIndex(panel.model.index(0, 0))
            wait_for(lambda: panel.page is not None)
            panel.ensureWidgetVisible(panel.copy_line)
            QTest.qWait(25)
            window.grab().save(str(folder / f"search-line-{name}.png"))
            assert panel.horizontalScrollBar().maximum() == 0
            assert panel.viewer.toPlainText() == line
            handle = panel.splitter.handle(1)
            panel.ensureWidgetVisible(handle)
            QTest.qWait(20)
            original_sizes = panel.splitter.sizes()
            center = handle.rect().center()
            shift = QPoint(0, -35 if original_sizes[0] > 170 else 35)
            QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=center)
            QTest.mouseMove(handle, center + shift, delay=20)
            QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=center + shift)
            QTest.qWait(20)
            assert original_sizes != panel.splitter.sizes()
            window.grab().save(str(folder / f"search-divider-{name}.png"))
            report["sizes"].append(
                {
                    "label": name,
                    "width": window.width(),
                    "height": window.height(),
                    "page_horizontal_overflow": panel.horizontalScrollBar().maximum(),
                    "vertical_scroll_maximum": panel.verticalScrollBar().maximum(),
                    "splitter_dragged": original_sizes != panel.splitter.sizes(),
                    "splitter_sizes": panel.splitter.sizes(),
                }
            )
        report.update(
            tabs=window.tabs.count(),
            dpr=window.devicePixelRatioF(),
            matches=panel.model.rowCount(),
            source_unchanged=before == hashlib.sha256(path.read_bytes()).hexdigest(),
            jobs=len(window.store.jobs()),
            availability_disabled=not window.instagram.start_button.isEnabled(),
            instagram_backend_state=manager.state,
            resolved_id=panel.resolved_id,
            only_fixture_requests=not backend
            or all(call[1] in ("/api/about", "/api/tools", "/api/run") for call in backend.calls),
        )
    finally:
        window.close()
        wait_for(lambda: window.closed)
        if backend:
            backend.close()
        report["window_closed"] = window.closed
        (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(folder)


if __name__ == "__main__":
    main()
