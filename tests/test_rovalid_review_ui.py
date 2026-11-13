"""Offscreen review/status checks with no network dispatch."""

import pytest
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QInputDialog, QLineEdit, QMessageBox
from test_rovalid_ui import desktop as desktop

from username_finder import rovalid, rovalid_review
from username_finder.roblox_activity import RobloxActivity
from username_finder.rovalid_review_ui import review_dialog


def test_status_review_and_modes_never_dispatch(desktop, monkeypatch):
    monkeypatch.setattr(rovalid_review, "journal", rovalid_review._journal)
    panel = desktop.roblox
    monkeypatch.setattr(panel, "launch", lambda *a: (_ for _ in ()).throw(AssertionError("No dispatch")))
    panel.record = dict(state="Prepared", settings=panel.options())
    panel.controls()
    assert not panel.start_button.isEnabled() and panel.review_button.isEnabled()
    panel.checking_mode.setCurrentIndex(1)
    status = panel.current_permission()
    rovalid_review.record_review(
        desktop.store.path,
        mode="validator",
        connection="direct",
        decision="authorize",
        reviewer="Offline reviewer",
        evidence="Fixture only",
        fingerprint=status["fingerprint"],
    )
    panel.record["settings"]["mode"] = "validator"
    panel.controls()
    assert panel.start_button.isEnabled() and "Authorized" in panel.restriction.text()
    panel.proxy_mode.setCurrentIndex(2)
    assert not panel.start_button.isEnabled()
    panel.proxy_mode.setCurrentIndex(0)
    assert panel.start_button.isEnabled()
    guard = RobloxActivity(desktop.store.path)
    guard.block.write_bytes(b"rovalid_review_required")
    panel.controls()
    assert not panel.start_button.isEnabled()
    assert "BlockedNewRestriction" in panel.restriction.text()
    assert guard.block.read_bytes() == b"rovalid_review_required"


def test_explicit_dialog_records_review_then_requires_separate_authorization(desktop, monkeypatch):
    monkeypatch.setattr(rovalid_review, "journal", rovalid_review._journal)
    calls = []

    def act(dialog):
        fields = dialog.findChildren(QLineEdit)
        fields[0].setText("Offline reviewer")
        fields[1].setText("Synthetic temporary evidence")
        choices = dialog.findChild(QComboBox)
        choices.setCurrentIndex(choices.findData(calls[-1]))
        dialog.findChild(QDialogButtonBox).accepted.emit()
        return dialog.result()

    monkeypatch.setattr(QDialog, "exec", act)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    calls.append("review")
    review_dialog(desktop.roblox, desktop.store.path, "validator", "direct")
    assert not rovalid.permission(desktop.store.path, mode="validator")["allowed"]
    calls.append("authorize")
    review_dialog(desktop.roblox, desktop.store.path, "validator", "direct")
    assert rovalid.permission(desktop.store.path, mode="validator")["allowed"]
    assert [r["decision"] for r in rovalid_review.journal(desktop.store.path)] == ["review", "authorize"]
    assert not desktop.roblox.busy


def test_blocked_dialog_cannot_offer_authorization_or_change_evidence(desktop, monkeypatch):
    monkeypatch.setattr(rovalid_review, "journal", rovalid_review._journal)
    guard = RobloxActivity(desktop.store.path)
    guard.block.write_bytes(b"http_429")
    before = desktop.store.db.total_changes

    def act(dialog):
        choices = dialog.findChild(QComboBox)
        assert choices.findData("authorize") == -1
        dialog.findChildren(QLineEdit)[0].setText("Offline reviewer")
        dialog.findChildren(QLineEdit)[1].setText("Legacy Bulk evidence has no endpoint-scoped exemption")
        dialog.findChild(QDialogButtonBox).accepted.emit()
        return dialog.result()

    monkeypatch.setattr(QDialog, "exec", act)
    review_dialog(desktop.roblox, desktop.store.path, "validator", "direct")
    assert not rovalid.permission(desktop.store.path, mode="validator")["allowed"]
    assert guard.block.read_bytes() == b"http_429"
    assert desktop.store.db.total_changes == before


@pytest.mark.parametrize("size", [(900, 650), (1180, 850)])
def test_blocked_status_layout_offscreen(desktop, qtbot, monkeypatch, size):
    monkeypatch.setattr(rovalid_review, "journal", rovalid_review._journal)
    RobloxActivity(desktop.store.path).block.write_bytes(b"http_429")
    desktop.resize(*size)
    desktop.tabs.setCurrentWidget(desktop.roblox)
    desktop.roblox.workspace.setCurrentIndex(1)
    desktop.roblox.controls()
    qtbot.wait(20)
    assert (desktop.width(), desktop.height()) == size
    assert desktop.roblox.review_button.isVisible()


def test_source_screen_review_is_separate_from_validator_pool_review(desktop, monkeypatch):
    panel = desktop.roblox
    panel.checking_mode.setCurrentIndex(1)
    panel.proxy_mode.setCurrentIndex(3)
    calls = []
    monkeypatch.setattr("username_finder.rovalid_ui.review_dialog", lambda *a: calls.append(a[2:]))
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a: ("فحص Validator عبر المصادر", True))
    panel.review_restriction()
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a: ("جلب المصادر وفحص اتصال Bulk", True))
    panel.review_restriction()
    assert calls == [("validator", "sources"), ("two_stage", "sources")]
    assert not panel.busy
