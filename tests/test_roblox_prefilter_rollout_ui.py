import os

import pytest
from PySide6.QtCore import Qt
from test_roblox_prefilter import no_live as no_live
from test_roblox_ui import window as window


@pytest.mark.parametrize("size", [20, 50, 100, 200, 500, 1000])
def test_selectable_range_preview_and_explicit_dispatch_guard(window, qtbot, tmp_path, size, monkeypatch):
    ui = window.roblox.prefilter
    window.tabs.setCurrentWidget(window.roblox)
    window.roblox.checking_mode.setCurrentIndex(2)
    assert ui.batch.minimum() == 1 and ui.batch.maximum() == 1000 and ui.batch.value() == 20
    assert ui.layoutDirection() == Qt.LayoutDirection.RightToLeft
    ui.batch.setValue(size)
    assert ui.calculation["config"]["batch_size"] == size
    assert ui.batch_warning.isVisible() == (size > 20)
    path = tmp_path / "names.txt"
    path.write_text("FirstName\nSecondName\nfirstname\ninvalid-name", encoding="utf8")
    ui.source.setText(str(path))
    ui.prepare()
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    assert ui.snapshot["counts"]["duplicates"] == ui.snapshot["counts"]["invalid"] == 1
    before = ui.service.manifest_path.read_bytes()
    assert ui.buttons["pilot"].isEnabled() == (size <= 20)
    if size > 20:
        monkeypatch.setattr(ui, "confirm", lambda _: pytest.fail("Unsupported batch cannot be authorized"))
        ui.start("pilot")
        assert "20" in ui.status.text()
    ui.review_restriction()
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    assert "restriction_review" in ui.snapshot and not window.roblox.test_calls
    monkeypatch.setattr(ui, "confirm", lambda _: False)
    ui.acknowledge_restriction()
    assert not ui.snapshot["restriction_review"]["recent_reviews"]
    monkeypatch.setattr(ui, "confirm", lambda _: True)
    ui.acknowledge_restriction()
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    assert len(ui.snapshot["restriction_review"]["recent_reviews"]) == 1
    assert ui.service.manifest_path.read_bytes() == before
    assert not window.roblox.repo.runs() and not window.roblox.test_calls


@pytest.mark.parametrize("width,height", [(900, 650), (1180, 850)])
def test_large_batch_calculator_native_layout_and_saved_config(window, qtbot, tmp_path, width, height):
    ui = window.roblox.prefilter
    window.tabs.setCurrentWidget(window.roblox)
    window.roblox.checking_mode.setCurrentIndex(2)
    window.resize(width, height)
    ui.batch.setValue(1000)
    ui.delay.setValue(0.001)
    qtbot.wait(40)
    ui.steps_area.ensureWidgetVisible(ui.estimates)
    qtbot.wait(30)
    assert ui.batch_warning.isVisible() and "محظور" in ui.estimate_note.text()
    assert float(ui.rate_values["second"].text().replace(",", "")) > 0
    assert ui.steps_area.horizontalScrollBar().maximum() == 0
    assert window.roblox.horizontalScrollBar().maximum() == 0
    assert ui.calculation["config"]["batch_size"] == 1000 and ui.service is None
    if os.environ.get("UFB_SCREENSHOTS"):
        target = tmp_path / f"bulk-rollout-{width}.png"
        assert window.grab().save(str(target))
        print("ROLLOUT_SCREENSHOT=" + str(target))
