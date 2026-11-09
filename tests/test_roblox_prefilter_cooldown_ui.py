import pytest
from test_roblox_prefilter import job as job
from test_roblox_prefilter import no_live as no_live
from test_roblox_prefilter_cooldown import restrict
from test_roblox_ui import window as window


def test_future_cooldown_gui_review_wait_and_separate_confirmed_continuation(window, job, qtbot, monkeypatch):
    stopped = restrict(job)
    ui = window.roblox.prefilter
    ui.service = job
    ui.show_progress(stopped)
    assert not ui.buttons["pilot"].isEnabled() and not ui.buttons["full"].isEnabled()
    assert not ui.buttons["continue_cooldown"].isEnabled()
    assert "Unknown" in ui.review_details.text()
    monkeypatch.setattr(ui, "confirm", lambda _: False)
    ui.acknowledge_restriction()
    assert job.summary()["cooldown"]["stage"] == "PendingReview"
    monkeypatch.setattr(ui, "confirm", lambda _: True)
    ui.acknowledge_restriction()
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    assert ui.snapshot["cooldown"]["stage"] == "Reviewed"
    assert ui.buttons["continue_cooldown"].isEnabled() and len(job.all_calls) == 1
    monkeypatch.setattr(ui, "confirm", lambda _: False)
    ui.continue_cooldown()
    assert len(job.all_calls) == 1
    monkeypatch.setattr(ui, "confirm", lambda _: True)
    ui.continue_cooldown()
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    assert ui.snapshot["phases"]["pilot"]["state"] == "Completed"
    assert ui.snapshot["counts"]["unknown"] == 20 and len(job.all_calls) == 3
    assert not window.roblox.test_calls and not window.roblox.repo.runs()


def test_historical_marker_never_enables_gui_continuation(window, job, monkeypatch):
    from username_finder.roblox_activity import RobloxActivity

    stopped = restrict(job)
    RobloxActivity(job.database).block.write_bytes(b"http_429")
    ui = window.roblox.prefilter
    ui.service = job
    ui.show_progress(stopped)
    # The owner database also has no generic continuation path; validation always checks the real marker.
    monkeypatch.setattr(ui, "confirm", lambda _: pytest.fail("No confirmation should be reached"))
    ui.continue_cooldown()
    assert len(job.all_calls) == 1 and job.summary()["counts"]["unknown"] == 20
