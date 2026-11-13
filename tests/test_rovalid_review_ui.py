"""Review UI is absent; retained backend guards and historical evidence remain authoritative."""

import importlib.util

import pytest
from PySide6.QtWidgets import QPushButton
from test_rovalid_ui import desktop as desktop

from username_finder import rovalid, rovalid_review
from username_finder.roblox_activity import RobloxActivity


def test_review_controls_module_and_shared_monitoring_are_absent(desktop):
    panel = desktop.roblox
    for name in (
        "restriction",
        "restriction_button",
        "review_button",
        "review_restriction",
        "checking_allowed",
    ):
        assert not hasattr(panel, name)
    assert importlib.util.find_spec("username_finder.rovalid_review_ui") is None
    labels = {button.text() for button in panel.findChildren(QPushButton)}
    assert "تحقق من القيد — محلي فقط" not in labels
    assert "مراجعة القيد وتسجيل القرار" not in labels
    assert not hasattr(desktop.dashboard, "restrictions")
    assert "أولية" in desktop.dashboard.capability_notice.text()


def test_no_review_ui_does_not_authorize_fresh_or_historically_blocked_activity(desktop, monkeypatch):
    monkeypatch.setattr(rovalid_review, "journal", rovalid_review._journal)
    panel = desktop.roblox
    panel.current_run = "a" * 32
    panel.record = dict(state="Prepared", settings=panel.options())
    before = desktop.store.db.total_changes
    monkeypatch.setattr(panel, "launch", lambda *a: pytest.fail("No unauthorized dispatch"))
    panel.controls()
    assert not panel.start_button.isEnabled()
    panel.start()
    assert "القيد التاريخي" in panel.activity_status.text()
    guard = RobloxActivity(desktop.store.path)
    guard.block.write_bytes(b"http_429")
    panel.controls()
    panel.start()
    panel.resume()
    assert guard.block.read_bytes() == b"http_429"
    assert not rovalid.permission(desktop.store.path)["allowed"]
    assert desktop.store.db.total_changes == before


def test_backend_scoped_authorization_still_gates_each_mode_and_route(desktop, monkeypatch):
    monkeypatch.setattr(rovalid_review, "journal", rovalid_review._journal)
    panel = desktop.roblox
    panel.checking_mode.setCurrentIndex(1)
    panel.current_run = "a" * 32
    panel.record = dict(state="Prepared", settings=panel.options())
    status = panel.current_permission()
    rovalid_review.record_review(
        desktop.store.path,
        mode="validator",
        connection="direct",
        decision="authorize",
        reviewer="Offline test",
        evidence="Temporary fixture",
        fingerprint=status["fingerprint"],
    )
    panel.controls()
    assert panel.start_button.isEnabled()
    panel.proxy_mode.setCurrentIndex(2)
    assert not panel.start_button.isEnabled()
    panel.proxy_mode.setCurrentIndex(0)
    assert panel.start_button.isEnabled()
    RobloxActivity(desktop.store.path).pending.write_bytes(b"Pending:unknown\n")
    panel.controls()
    assert not panel.start_button.isEnabled()


@pytest.mark.parametrize("size", [(900, 650), (1180, 850)])
def test_clean_run_layout_offscreen(desktop, qtbot, size):
    desktop.resize(*size)
    desktop.tabs.setCurrentWidget(desktop.roblox)
    desktop.roblox.workspace.setCurrentIndex(1)
    qtbot.wait(20)
    assert (desktop.width(), desktop.height()) == size
    assert desktop.roblox.start_button.isVisible() and desktop.roblox.stop_button.isVisible()
