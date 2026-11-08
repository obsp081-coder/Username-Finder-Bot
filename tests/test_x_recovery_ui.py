import threading

from test_x import response
from test_x_ui import wait_done
from test_x_ui import window as window

from username_finder import x
from username_finder.x_runner import XRunner


def seed_restriction(panel):
    clock = panel.test_clock
    panel.recovery.clock = clock.now
    panel.repo.set_interval(0)
    identity = panel.repo.prepare(x.input_spec("first_old\nsecond_old"))
    XRunner(panel.repo, clock=clock.now, wait=clock.wait, fetcher=lambda *a: x.protocol.Response(429)).run(
        identity
    )
    panel.current_run = identity
    panel.refresh()
    return identity, clock


def test_two_confirmations_no_auto_resume_and_new_job_after_release(window, qtbot, monkeypatch):
    panel = window.x_panel
    old, clock = seed_restriction(panel)
    original = panel.repo.progress(old)
    assert panel.recovery_check_button.isEnabled() and not panel.start_button.isEnabled()
    assert "HTTP: 429" in panel.restriction.text()
    assert "وقت القيد" in panel.restriction.text()
    monkeypatch.setattr(panel, "_confirm", lambda _: False)
    panel.recovery_check_button.click()
    assert not clock.calls and not panel.recovery.events()
    monkeypatch.setattr(panel, "_confirm", lambda _: True)
    panel.recovery_check_button.click()
    wait_done(qtbot, panel)
    assert [name for name, _ in clock.calls] == ["OpenAI"]
    assert panel.repo.control()["blocked"] and not panel.start_button.isEnabled()
    assert not panel.recovery_release_button.isEnabled()
    assert "نجح طلب واحد" in panel.recovery_label.text()
    clock.seconds += 5
    panel.controls()
    assert panel.recovery_release_button.isEnabled()
    assert "بانتظار إقرار" in panel.recovery_label.text()
    monkeypatch.setattr(panel, "_confirm", lambda _: False)
    panel.recovery_release_button.click()
    assert panel.repo.control()["blocked"]
    monkeypatch.setattr(panel, "_confirm", lambda _: True)
    panel.recovery_release_button.click()
    assert not panel.repo.control()["blocked"]
    assert panel.speed.currentData() == 5
    assert "حُرر القيد" in panel.recovery_label.text()
    assert panel.repo.progress(old) == original
    assert not panel.start_button.isEnabled() and len(clock.calls) == 1
    panel.quantity.setText("2")
    panel.preview()
    wait_done(qtbot, panel)
    new = panel.current_run
    assert panel.repo.progress(new)["interval_seconds"] == 5 and panel.start_button.isEnabled()
    panel.start_button.click()
    wait_done(qtbot, panel)
    assert panel.repo.progress(new)["state"] == "Completed"
    assert len(clock.calls) == len({name for name, _ in clock.calls}) == 3
    assert panel.repo.progress(old) == original
    assert panel.recovery.events()[0]["released_at"]


def test_wait_and_failed_probe_keep_start_and_release_disabled(window, qtbot, monkeypatch):
    panel = window.x_panel
    _, clock = seed_restriction(panel)
    monkeypatch.setattr(panel, "_confirm", lambda _: True)
    panel.fetcher = lambda *a: x.protocol.Response(429, b"", {"retry-after": "120"})
    panel.recovery_check_button.click()
    wait_done(qtbot, panel)
    assert not panel.recovery_check_button.isEnabled()
    assert not panel.recovery_release_button.isEnabled() and not panel.start_button.isEnabled()
    clock.seconds += 119
    panel.controls()
    assert not panel.recovery_check_button.isEnabled()
    clock.seconds += 1
    panel.controls()
    assert panel.recovery_check_button.isEnabled() and "فشل طلب الاستعادة" in panel.recovery_label.text()
    assert len(panel.recovery.events()) == 1 and panel.repo.control()["blocked"]


def test_stop_recovery_keeps_audit_and_latch_without_retry(window, qtbot, monkeypatch):
    panel = window.x_panel
    seed_restriction(panel)
    entered = threading.Event()
    calls = []

    def fetch(*args):
        calls.append(args)
        entered.set()
        assert panel.cancel.wait(3)
        return response(False, "taken")

    panel.fetcher = fetch
    monkeypatch.setattr(panel, "_confirm", lambda _: True)
    panel.check_recovery()
    qtbot.waitUntil(entered.is_set)
    qtbot.waitUntil(lambda: "جارٍ طلب استعادة" in panel.recovery_label.text())
    assert not panel.start_button.isEnabled() and not panel.recovery_check_button.isEnabled()
    assert not panel.connection_mode.isEnabled() and panel.stop_button.isEnabled()
    panel.stop_button.click()
    wait_done(qtbot, panel)
    assert len(calls) == 1 and panel.recovery.events()[0]["state"] == "Cancelled"
    assert panel.repo.control()["blocked"]
