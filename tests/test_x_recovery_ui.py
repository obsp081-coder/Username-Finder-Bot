"""No ordinary restriction-recovery UI; X engine guards and historical audits remain unchanged."""

import pytest
from PySide6.QtWidgets import QPushButton
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


def test_recovery_controls_handlers_and_polling_are_removed(window):
    panel = window.x_panel
    for name in (
        "restriction",
        "review_restriction_button",
        "recovery_check_button",
        "recovery_release_button",
        "recovery_label",
        "recovery_timer",
        "check_recovery",
        "release_restriction",
        "review_restriction",
        "restriction_text",
    ):
        assert not hasattr(panel, name)
    labels = {button.text() for button in panel.findChildren(QPushButton)}
    assert not {"مراجعة دليل قيد X — دون اتصال", "طلب استعادة X واحد…", "تحرير القيد يدويًا…"} & labels


def test_refresh_and_direct_actions_never_release_or_probe_a_saved_restriction(window, qtbot):
    panel = window.x_panel
    identity, clock = seed_restriction(panel)
    before = (
        panel.repo.progress(identity),
        panel.repo.control(),
        panel.repo.history("first_old"),
        panel.recovery.events(),
    )
    for action in (
        panel.controls,
        panel.refresh,
        panel.start,
        panel.resume,
        panel.recheck_all,
        panel.recheck_selected,
    ):
        action()
    qtbot.wait(40)
    assert not panel.start_button.isEnabled() and not panel.resume_button.isEnabled()
    assert "unexpected_http_status:429" in panel.status.text()
    assert not clock.calls
    assert before == (
        panel.repo.progress(identity),
        panel.repo.control(),
        panel.repo.history("first_old"),
        panel.recovery.events(),
    )


def test_historical_successful_probe_is_not_implicitly_released_by_ui_refresh(window):
    panel = window.x_panel
    identity, clock = seed_restriction(panel)
    panel.recovery.check(confirmed=True, fetcher=panel.fetcher)
    clock.seconds += 5
    before = panel.recovery.events(), panel.repo.control(), panel.repo.progress(identity)
    panel.refresh()
    assert before == (panel.recovery.events(), panel.repo.control(), panel.repo.progress(identity))
    assert panel.repo.control()["blocked"] and not panel.start_button.isEnabled()
    assert len(clock.calls) == 1


@pytest.mark.parametrize("size", [(900, 650), (1180, 850)])
def test_x_operations_layout_keeps_history_and_actual_error(window, qtbot, size):
    panel = window.x_panel
    seed_restriction(panel)
    window.resize(*size)
    window.tabs.setCurrentWidget(panel)
    panel.workspace.setCurrentIndex(2)
    qtbot.wait(20)
    assert (window.width(), window.height()) == size
    assert panel.operations.isVisible() and "429" in panel.status.text()
