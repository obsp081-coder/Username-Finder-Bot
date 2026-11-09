import pytest
from test_roblox_bulk import response
from test_roblox_prefilter import execute
from test_roblox_prefilter import job as job
from test_roblox_prefilter import no_live as no_live
from test_roblox_ui import window as window


def test_review_displays_cooldown_without_enabling_execution(window, job, qtbot, monkeypatch):
    job.prepare(job.test_source)
    job.transport = lambda _: response(status=429, headers={"retry-after": "60"})
    execute(job)
    ui = window.roblox.prefilter
    window.tabs.setCurrentWidget(window.roblox)
    window.roblox.checking_mode.setCurrentIndex(2)
    ui.service = job
    ui.review_restriction()
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    assert "مهلة موثقة" in ui.review_details.text()
    assert "غير مطبق" in ui.review_details.text()
    assert not ui.buttons["pilot"].isEnabled() and not ui.buttons["full"].isEnabled()
    monkeypatch.setattr(ui, "confirm", lambda _: True)
    ui.acknowledge_restriction()
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    assert "دون إذن إرسال" in ui.review_details.text()
    job.test_clock.at += 61
    ui.review_restriction()
    qtbot.waitUntil(lambda: not ui.busy, timeout=5000)
    assert "إذن الموفر ما زال مطلوبًا" in ui.review_details.text()
    with pytest.raises(ValueError):
        execute(job)
    assert not window.roblox.test_calls and not window.roblox.repo.runs()
