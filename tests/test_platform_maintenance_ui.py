"""Offline maintenance access with real guards/state machines and mocked provider transport."""

import json
import os
import subprocess
import sys
import threading

import pytest
from test_rovalid_ui import desktop as desktop
from test_rovalid_ui import wait_idle
from test_x import response
from test_x_ui import wait_done
from test_x_ui import window as window

from username_finder import rovalid, rovalid_review, x
from username_finder.platform_maintenance_ui import PlatformMaintenanceDialog
from username_finder.roblox_activity import RobloxActivity
from username_finder.x_runner import XRunner

REAL_JOURNAL = rovalid_review.journal


def dialog(ui, qtbot):
    result = PlatformMaintenanceDialog(ui)
    qtbot.addWidget(result)
    result.show()
    return result


def review_fields(d, decision="authorize"):
    d.mode.setCurrentIndex(d.mode.findData("validator"))
    d.reviewer.setText("Offline reviewer")
    d.evidence.setText("Synthetic isolated evidence reference")
    d.decision.setCurrentIndex(d.decision.findData(decision))


def restrict_x(panel, *, status=429, headers=None):
    clock = panel.test_clock
    panel.recovery.clock = clock.now
    panel.repo.set_interval(0)
    identity = panel.repo.prepare(x.input_spec("first_old\nsecond_old"))
    XRunner(
        panel.repo,
        clock=clock.now,
        wait=clock.wait,
        fetcher=lambda *a: x.protocol.Response(status, b"", headers or {}),
    ).run(identity)
    panel.current_run = identity
    panel.refresh()
    return identity, clock


def test_one_discoverable_entry_without_normal_page_review_panels(desktop, qtbot):
    manager = desktop.job_manager
    assert manager.maintenance_button.text() == "صيانة المنصات…"
    manager.maintenance_button.click()
    d = manager.maintenance_dialog
    assert d.isVisible() and d.pages.count() == 2
    manager.open_maintenance()
    assert manager.maintenance_dialog is d
    assert not hasattr(desktop.roblox, "review_button")
    assert not hasattr(desktop.x_panel, "recovery_check_button")
    d.reject()
    qtbot.waitUntil(lambda: manager.maintenance_dialog is None)


def test_fresh_explicit_review_is_scoped_and_never_starts_work(desktop, qtbot, monkeypatch):
    monkeypatch.setattr(rovalid_review, "journal", REAL_JOURNAL)
    d = dialog(desktop, qtbot)
    review_fields(d)
    before = desktop.store.db.total_changes
    assert (
        d.save_review.isEnabled() and not rovalid.permission(desktop.store.path, mode="validator")["allowed"]
    )
    monkeypatch.setattr(d, "confirm", lambda _: False)
    d.record_review()
    assert rovalid_review.journal(desktop.store.path) == []
    monkeypatch.setattr(d, "confirm", lambda _: True)
    d.record_review()
    assert rovalid.permission(desktop.store.path, mode="validator")["allowed"]
    assert not rovalid.permission(desktop.store.path, mode="two_stage")["allowed"]
    assert not rovalid.permission(desktop.store.path, mode="validator", connection="private")["allowed"]
    assert not desktop.roblox.busy and not desktop.x_panel.busy
    assert desktop.store.db.total_changes == before
    probe = (
        "import json,sys; from username_finder import rovalid; "
        "print(json.dumps([rovalid.permission(sys.argv[1],mode=m,connection=c)['allowed'] "
        "for m,c in [('validator','direct'),('two_stage','direct'),('validator','private')]]))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", probe, str(desktop.store.path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert json.loads(result.stdout) == [True, False, False]


@pytest.mark.parametrize("marker", ["block", "pending", "cooldown", "malformed"])
def test_unresolved_marker_only_allows_review_not_authorization(desktop, qtbot, monkeypatch, marker):
    monkeypatch.setattr(rovalid_review, "journal", REAL_JOURNAL)
    guard = RobloxActivity(desktop.store.path)
    path = getattr(guard, "cooldown" if marker == "malformed" else marker)
    values = dict(
        block=b"http_429",
        pending=b"Pending:unknown\n",
        malformed=b"bad checkpoint",
        cooldown=(
            json.dumps(dict(version=1, owner="a" * 64 + ":" + "b" * 64, restriction="c" * 64, state="Hold"))
            + "\n"
        ).encode(),
    )
    path.write_bytes(values[marker])
    d = dialog(desktop, qtbot)
    review_fields(d, "review")
    assert d.decision.findData("authorize") == -1
    monkeypatch.setattr(d, "confirm", lambda _: True)
    d.record_review()
    assert rovalid_review.journal(desktop.store.path)[-1]["decision"] == "review"
    # A direct UI call cannot turn a forged selection into guard bypass.
    d.decision.addItem("forged", "authorize")
    d.decision.setCurrentIndex(d.decision.findData("authorize"))
    d.record_review()
    assert not rovalid.permission(desktop.store.path, mode="validator")["allowed"]
    assert all(r["decision"] != "authorize" for r in rovalid_review.journal(desktop.store.path))
    assert path.read_bytes() == values[marker]


def test_stale_and_corrupt_review_evidence_fail_closed(desktop, qtbot, monkeypatch):
    monkeypatch.setattr(rovalid_review, "journal", REAL_JOURNAL)
    d = dialog(desktop, qtbot)
    review_fields(d)

    def changed_during_confirmation(_):
        RobloxActivity(desktop.store.path).pending.write_bytes(b"Clear\n")
        d.refresh()
        return True

    monkeypatch.setattr(d, "confirm", changed_during_confirmation)
    d.record_review()
    assert rovalid_review.journal(desktop.store.path) == []
    path = rovalid_review.journal_path(desktop.store.path)
    path.write_bytes(b"incomplete audit")
    d.refresh()
    assert not d.save_review.isEnabled() and d.decision.findData("authorize") == -1
    d.record_review()
    assert path.read_bytes() == b"incomplete audit"


def test_v1_audit_remains_intact_when_user_explicitly_reviews_v2(desktop, qtbot, monkeypatch):
    monkeypatch.setattr(rovalid_review, "journal", REAL_JOURNAL)
    status = rovalid.permission(desktop.store.path, mode="validator")
    row = rovalid_review.record_review(
        desktop.store.path,
        mode="validator",
        connection="direct",
        decision="authorize",
        reviewer="Offline reviewer",
        evidence="Fixture",
        fingerprint=status["fingerprint"],
    )
    row["policy"] = row["snapshot"]["policy"] = rovalid_review.LEGACY_POLICY
    row["fingerprint"] = rovalid_review.digest(row["snapshot"])
    row["hash"] = rovalid_review.digest({k: v for k, v in row.items() if k != "hash"})
    path = rovalid_review.journal_path(desktop.store.path)
    original = (json.dumps(row) + "\n").encode()
    path.write_bytes(original)
    d = dialog(desktop, qtbot)
    review_fields(d)
    assert "سياسة ربط أقدم" in d.roblox_reason.toPlainText()
    monkeypatch.setattr(d, "confirm", lambda _: True)
    d.record_review()
    assert path.read_bytes().startswith(original)
    records = rovalid_review.journal(desktop.store.path)
    assert len(records) == 2 and records[-1]["previous"] == row["hash"]
    assert rovalid.permission(desktop.store.path, mode="validator")["allowed"]


@pytest.mark.parametrize("mode,connection", [("validator", "direct"), ("two_stage", "private")])
def test_child_rechecks_mode_and_route_after_gui_maintenance(desktop, qtbot, monkeypatch, mode, connection):
    monkeypatch.setattr(rovalid_review, "journal", REAL_JOURNAL)
    panel = desktop.roblox
    panel.count.setValue(1)
    panel.prepare()  # Two-stage/direct. No HTTP.
    wait_idle(qtbot, panel)
    d = dialog(desktop, qtbot)
    d.mode.setCurrentIndex(d.mode.findData(mode))
    d.connection.setCurrentIndex(d.connection.findData(connection))
    d.reviewer.setText("Offline reviewer")
    d.evidence.setText("Scoped fixture")
    d.decision.setCurrentIndex(d.decision.findData("authorize"))
    monkeypatch.setattr(d, "confirm", lambda _: True)
    d.record_review()
    assert rovalid.permission(desktop.store.path, mode=mode, connection=connection)["allowed"]
    assert not panel.start_button.isEnabled()
    panel.launch("run", {})  # Direct managed child cannot use a different approved scope.
    wait_idle(qtbot, panel)
    assert panel.outcome == "error" and panel.record["state"] == "Prepared"
    assert not (panel.root / panel.current_run / "results").exists()


def test_x_requires_two_confirmations_waits_and_never_resumes_old_job(window, qtbot, monkeypatch):
    panel = window.x_panel
    identity, clock = restrict_x(panel)
    before = panel.repo.progress(identity), panel.repo.history("first_old")
    d = dialog(window, qtbot)
    assert d.x_check.isEnabled() and not d.x_release.isEnabled()
    monkeypatch.setattr(d, "confirm", lambda _: False)
    d.check_x()
    assert not clock.calls and not panel.recovery.events()
    monkeypatch.setattr(d, "confirm", lambda _: True)
    d.check_x()
    d.check_x()
    wait_done(qtbot, panel)
    assert len(clock.calls) == 1 and panel.repo.control()["blocked"]
    assert not d.x_release.isEnabled() and "Waiting" in d.x_reason.toPlainText()
    clock.seconds += 5
    d.refresh()
    assert d.x_release.isEnabled()
    monkeypatch.setattr(d, "confirm", lambda _: False)
    d.release_x()
    assert panel.repo.control()["blocked"]
    monkeypatch.setattr(d, "confirm", lambda _: True)
    d.release_x()
    assert not panel.repo.control()["blocked"] and panel.speed.currentData() == 5
    assert before == (panel.repo.progress(identity), panel.repo.history("first_old"))
    assert len(panel.repo.runs()) == 1 and len(clock.calls) == 1 and not panel.start_button.isEnabled()


@pytest.mark.parametrize(
    "status,headers", [(403, {}), (429, {"retry-after": "120"}), (429, {"retry-after": "invalid"})]
)
def test_x_ineligible_states_explain_why_without_requests(window, qtbot, monkeypatch, status, headers):
    panel = window.x_panel
    restrict_x(panel, status=status, headers=headers)
    d = dialog(window, qtbot)
    assert not d.x_check.isEnabled() and not d.x_release.isEnabled()
    assert "Waiting" in d.x_reason.toPlainText() or "ReviewRequired" in d.x_reason.toPlainText()
    monkeypatch.setattr(d, "confirm", lambda _: pytest.fail("No ineligible confirmation"))
    d.check_x()
    d.release_x()
    assert not panel.test_clock.calls


@pytest.mark.parametrize(
    "sample",
    [
        x.protocol.Response(403),
        x.protocol.Response(error="timeout"),
        x.protocol.Response(429, b"", {"retry-after": "120"}),
    ],
)
def test_failed_x_recovery_is_durable_without_retry_or_release(window, qtbot, monkeypatch, sample):
    panel = window.x_panel
    restrict_x(panel)
    calls = []
    panel.fetcher = lambda *a: calls.append(a) or sample
    d = dialog(window, qtbot)
    monkeypatch.setattr(d, "confirm", lambda _: True)
    d.check_x()
    wait_done(qtbot, panel)
    assert len(calls) == 1 and panel.recovery.events()[0]["state"] == "Failed"
    d.refresh()
    assert len(calls) == 1 and panel.repo.control()["blocked"] and not d.x_release.isEnabled()


@pytest.mark.parametrize("closing", [False, True])
def test_x_cancellation_and_close_wait_for_durable_cleanup(window, qtbot, monkeypatch, closing):
    panel = window.x_panel
    identity, _ = restrict_x(panel)
    before = panel.repo.progress(identity)
    entered = threading.Event()
    calls = []

    def fetch(*args):
        calls.append(args)
        entered.set()
        assert panel.cancel.wait(3)
        return response(False, "taken")

    panel.fetcher = fetch
    d = dialog(window, qtbot)
    monkeypatch.setattr(d, "confirm", lambda _: True)
    d.check_x()
    qtbot.waitUntil(entered.is_set)
    assert not d.x_check.isEnabled() and d.x_stop.isEnabled()
    d.reject() if closing else d.stop_x()
    wait_done(qtbot, panel)
    assert len(calls) == 1 and panel.recovery.events()[0]["state"] == "Cancelled"
    assert panel.repo.control()["blocked"] and panel.repo.progress(identity) == before
    assert not d.request_active
    if closing:
        assert not d.isVisible()


def test_uncertain_x_request_never_replays_on_open_or_refresh(window, qtbot):
    panel = window.x_panel
    restrict_x(panel)
    panel.recovery.begin(confirmed=True)  # Simulate an interrupted maintenance process.
    d = dialog(window, qtbot)
    assert not d.x_check.isEnabled() and "InFlight" in d.x_reason.toPlainText()
    panel.repo.recover()
    d.refresh()
    assert panel.recovery.events()[0]["state"] == "Uncertain"
    assert not panel.test_clock.calls and panel.repo.control()["blocked"]


def test_x_original_route_is_used_even_after_preference_changes(window, qtbot, monkeypatch):
    panel = window.x_panel
    restrict_x(panel)
    panel.repo.set_connection(dict(mode="proxy", proxy_url="http://different.example:8080"))
    panel.fetcher = None
    calls = []
    monkeypatch.setattr(
        x.protocol,
        "fetch",
        lambda name, timeout, *, connection: (
            calls.append((name, timeout, connection)) or response(False, "taken")
        ),
    )
    d = dialog(window, qtbot)
    monkeypatch.setattr(d, "confirm", lambda _: True)
    d.check_x()
    wait_done(qtbot, panel)
    assert calls == [("OpenAI", 15.0, dict(mode="direct", proxy_url=""))]
    assert panel.repo.control()["blocked"]


def test_recorded_running_operation_and_corrupt_x_evidence_disable_actions(window, qtbot, monkeypatch):
    panel = window.x_panel
    identity, _ = restrict_x(panel)
    d = dialog(window, qtbot)
    with panel.repo.store.transaction() as db:
        db.execute("UPDATE x_runs SET state='Running' WHERE id=?", (identity,))
    d.refresh()
    assert not d.x_check.isEnabled() and "جارية" in d.x_reason.toPlainText()
    with panel.repo.store.transaction() as db:
        db.execute("UPDATE x_runs SET state='Restricted' WHERE id=?", (identity,))
    monkeypatch.setattr(
        panel.recovery, "status", lambda: (_ for _ in ()).throw(ValueError("Malformed evidence"))
    )
    d.refresh()
    assert not d.x_check.isEnabled() and not d.x_release.isEnabled()
    assert "غير سليم" in d.x_reason.toPlainText()
    d.check_x()
    d.release_x()
    assert not panel.test_clock.calls


@pytest.mark.parametrize("size", [(900, 650), (1180, 850)])
def test_maintenance_is_compact_offscreen_and_navigation_unchanged(desktop, qtbot, size):
    desktop.resize(*size)
    count = desktop.tabs.count()
    d = dialog(desktop, qtbot)
    qtbot.wait(20)
    assert d.width() <= desktop.width() and d.height() < desktop.height()
    assert desktop.tabs.count() == count
    for i in range(d.pages.count()):
        assert d.pages.widget(i).horizontalScrollBar().maximum() == 0
