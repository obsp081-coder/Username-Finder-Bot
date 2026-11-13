"""Optional presentation must never switch off safety or change engine configuration."""

import pytest
from PySide6.QtWidgets import QMessageBox
from test_rovalid import engine as engine
from test_rovalid_ui import desktop as desktop
from test_rovalid_ui import wait_idle

from username_finder import rovalid, rovalid_child
from username_finder.roblox_activity import RobloxActivity
from username_finder.rovalid_ui import RoValidPanel


def test_default_validation_and_original_engine_configuration(engine):
    assert rovalid.settings({})["integration_monitoring"] is True
    for value in (None, 0, 1, "false"):
        with pytest.raises(ValueError):
            rovalid.settings(dict(integration_monitoring=value))
    outputs = []
    for enabled in (True, False):
        cfg, app = rovalid_child.configure(
            dict(settings=rovalid.DEFAULTS | dict(integration_monitoring=enabled)),
            ["Example"],
            {},
            engine[1],
        )
        outputs.append((vars(cfg), vars(app)))
    assert outputs[0] == outputs[1]
    assert "integration_monitoring" not in outputs[0][0] and "integration_monitoring" not in outputs[0][1]
    rovalid.verify_source()


def test_preference_persists_without_preparation_and_history_never_rewrites_it(desktop, qtbot):
    panel = desktop.roblox
    assert panel.integration_monitoring.isChecked()
    path = panel.root / "settings.json"
    rovalid.atomic_json(path, rovalid.DEFAULTS | dict(count=27))
    before = desktop.store.db.total_changes
    panel.integration_monitoring.setChecked(False)
    saved = rovalid.read_json(path)
    assert saved["integration_monitoring"] is False and saved["count"] == 27
    assert not panel.busy and desktop.store.db.total_changes == before
    second = RoValidPanel(desktop.store, desktop.make_table, desktop.fill_table)
    qtbot.addWidget(second)
    assert not second.integration_monitoring.isChecked()
    second.apply_options(
        rovalid.DEFAULTS
    )  # Loading an old operation defaults ON but is not a preference edit.
    assert second.integration_monitoring.isChecked()
    assert rovalid.read_json(path) == saved


@pytest.mark.parametrize("enabled", [True, False])
def test_gui_and_real_child_stay_denied_without_review(desktop, qtbot, monkeypatch, enabled):
    panel = desktop.roblox
    panel.integration_monitoring.setChecked(enabled)
    panel.count.setValue(1)
    panel.prepare()
    wait_idle(qtbot, panel)
    assert panel.record["settings"]["integration_monitoring"] == enabled
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    panel.start()  # Parent fixture mocks review; real child must independently refuse.
    wait_idle(qtbot, panel)
    assert panel.outcome == "error" and panel.record["state"] == "Prepared"
    guard = RobloxActivity(desktop.store.path)
    guard.block.write_bytes(b"http_429")
    panel.controls()
    assert not panel.start_button.isEnabled()
    monkeypatch.setattr(panel, "launch", lambda *a: pytest.fail("Historical restriction must win"))
    panel.start()
    assert guard.block.read_bytes() == b"http_429"


def test_existing_record_default_and_saved_monitoring_survive_editor_changes(desktop, monkeypatch):
    panel = desktop.roblox
    options = panel.options()
    del options["integration_monitoring"]
    panel.current_run = "a" * 32
    panel.record = dict(state="Prepared", settings=options)
    calls = []
    monkeypatch.setattr(panel, "launch", lambda *args: calls.append(args))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    panel.start()
    assert calls  # Old manifests are compared with normalized defaults, never rewritten.
    assert "integration_monitoring" not in panel.record["settings"]
    panel.record = dict(state="Stopped", settings=panel.options() | dict(integration_monitoring=False))
    panel.integration_monitoring.setChecked(True)
    panel.resume()
    assert calls[-1][1]["resume"] and panel.record["settings"]["integration_monitoring"] is False


def test_safety_notification_visible_with_monitoring_off(desktop, monkeypatch):
    panel = desktop.roblox
    panel.integration_monitoring.setChecked(False)
    monkeypatch.setattr(panel.process, "readAllStandardOutput", lambda: b'{"event":"restriction"}\n')
    panel._read_output()
    assert "قيد جديد" in panel.activity_status.text()


@pytest.mark.parametrize("enabled", [True, False])
def test_launch_displays_saved_monitoring_and_clears_stale_live_progress(desktop, monkeypatch, enabled):
    panel = desktop.roblox
    panel.current_run = "a" * 32
    panel.record = dict(settings=panel.options() | dict(integration_monitoring=enabled))
    panel.integration_monitoring.setChecked(not enabled)
    panel.view_progress = (panel.current_run, dict(total=100, completed=70))
    monkeypatch.setattr(panel.process, "start", lambda: None)
    monkeypatch.setattr(panel.process, "write", lambda *a: 1)
    panel.launch("run", dict(resume=True))
    assert panel.view_progress == (panel.current_run, None)
    assert ("مفعلة" if enabled else "متوقفة") in panel.statistics.text()
    assert not panel.busy
