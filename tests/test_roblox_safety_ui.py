"""Fail-closed GUI controls and cross-mode background lifecycle regressions."""

from datetime import UTC, datetime, timedelta

import pytest
from test_roblox_restriction import checkpoint, control
from test_roblox_ui import window as window

from username_finder.roblox_activity import RobloxActivity


def test_historical_marker_disables_individual_start_and_resume(window):
    panel = window.roblox
    panel.current_run = panel.repo.create_run(["PendingName"])
    marker = RobloxActivity(panel.repo.store.path).block
    marker.write_bytes(b"http_429")
    panel.refresh()
    assert not panel.start_button.isEnabled()
    assert not panel.resume_button.isEnabled()
    assert not panel.recheck_all_button.isEnabled()
    assert not panel.recheck_selected_button.isEnabled()
    assert marker.read_bytes() == b"http_429" and not panel.test_calls


def test_bulk_controls_disable_during_another_local_worker(window):
    panel = window.roblox
    panel.bulk.prepare()
    assert panel.bulk.buttons["stage1"].isEnabled()
    panel.busy = True
    try:
        panel.bulk.refresh()
        assert not panel.bulk.buttons["stage1"].isEnabled()
        assert not panel.bulk.buttons["prepare"].isEnabled()
        assert not panel.bulk.buttons["csv"].isEnabled()
    finally:
        panel.busy = False


@pytest.mark.parametrize("kind", ["historical", "pending", "cooldown", "malformed", "individual", "streak"])
def test_shared_restrictions_gate_all_modes_and_direct_entry(window, qtbot, tmp_path, monkeypatch, kind):
    panel = window.roblox
    panel.bulk.prepare()
    source = tmp_path / "names.txt"
    source.write_text("FirstName\nOtherName")
    panel.prefilter.source.setText(str(source))
    panel.prefilter.prepare()
    qtbot.waitUntil(lambda: not panel.busy)
    panel.current_run = panel.repo.create_run(["PendingName"])
    guard = RobloxActivity(panel.repo.store.path)
    if kind == "historical":
        guard.block.write_bytes(b"http_429")
    elif kind == "pending":
        guard.pending.write_bytes(b"Pending:unknown\n")
    elif kind == "cooldown":
        checkpoint(guard)
    elif kind == "malformed":
        guard.cooldown.write_bytes(b"invalid")
    elif kind == "individual":
        control(panel.repo, blocked=True)
    else:
        control(panel.repo, restriction_streak=1)
    panel.refresh()
    panel.bulk.refresh()
    panel.prefilter.controls()
    for button in ("stage1", "comparison", "stage2"):
        assert not panel.bulk.buttons[button].isEnabled()
    assert not panel.prefilter.buttons["pilot"].isEnabled()
    assert not panel.prefilter.buttons["full"].isEnabled()
    if kind != "streak":
        assert not panel.start_button.isEnabled() and not panel.resume_button.isEnabled()
        assert not panel.recheck_all_button.isEnabled()
        before = len(panel.repo.runs())
        monkeypatch.setattr(panel, "_confirm", lambda _: pytest.fail("Blocked before confirmation"))
        panel.start()
        panel.resume()
        panel.recheck_all()
        panel.recheck_selected()
        assert len(panel.repo.runs()) == before
    monkeypatch.setattr(panel.bulk, "confirm", lambda _: pytest.fail("Blocked before confirmation"))
    monkeypatch.setattr(panel.prefilter, "confirm", lambda _: pytest.fail("Blocked before confirmation"))
    panel.bulk.start("stage1")
    panel.prefilter.start("pilot")
    panel.check_restriction()
    if kind != "streak":
        assert not panel.diagnostic_button.isEnabled()
    assert not panel.test_calls and not panel.bulk.experiment.events()


def test_individual_wait_is_preserved_while_bulk_and_diagnostic_wait_for_deadline(window):
    panel = window.roblox
    control(panel.repo, not_before=(datetime.now(UTC) + timedelta(minutes=1)).isoformat())
    panel.current_run = panel.repo.create_run(["PendingName"])
    panel.refresh()
    assert panel.start_button.isEnabled() and panel.resume_button.isEnabled()
    assert "سينتظر" in panel.start_button.toolTip()
    panel.check_restriction()
    assert not panel.diagnostic_button.isEnabled() and not panel.test_calls


def test_shutdown_controls_do_not_probe_database_or_reopen_evidence(window, monkeypatch):
    panel = window.roblox

    def forbidden(*args, **kwargs):
        pytest.fail("Closing must not probe readiness or evidence")

    with monkeypatch.context() as patch:
        patch.setattr(panel.repo, "control", forbidden)
        patch.setattr(panel.bulk.experiment, "report", forbidden)
        panel.prefilter.shutdown()
        panel.bulk.shutdown()
        panel.closing = True
        panel.controls()
    assert not panel.start_button.isEnabled() and not panel.test_calls
