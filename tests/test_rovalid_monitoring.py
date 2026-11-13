"""Compatibility with the retired monitoring preference; progress is always live."""

import pytest
from PySide6.QtWidgets import QMessageBox
from test_rovalid import engine as engine
from test_rovalid_ui import desktop as desktop
from test_rovalid_ui import wait_idle

from username_finder import rovalid, rovalid_child
from username_finder.roblox_activity import RobloxActivity
from username_finder.rovalid_ui import RoValidPanel


def test_legacy_field_validation_and_original_engine_configuration(engine):
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


@pytest.mark.parametrize("legacy", [None, True, False])
def test_old_preferences_and_history_load_without_a_toggle_or_disk_rewrite(desktop, qtbot, legacy):
    panel = desktop.roblox
    assert not hasattr(panel, "integration_monitoring")
    saved = rovalid.DEFAULTS | dict(count=27)
    if legacy is None:
        saved.pop("integration_monitoring")
    else:
        saved["integration_monitoring"] = legacy
    path = panel.root / "settings.json"
    rovalid.atomic_json(path, saved)
    original = path.read_bytes()
    second = RoValidPanel(desktop.store, desktop.make_table, desktop.fill_table)
    qtbot.addWidget(second)
    assert second.count.value() == 27 and path.read_bytes() == original
    assert second.options()["integration_monitoring"] is True
    second.apply_options(rovalid.settings(saved))
    assert path.read_bytes() == original and not second.busy


@pytest.mark.parametrize("legacy", [True, False])
def test_legacy_setting_cannot_bypass_parent_or_real_child_guard(desktop, qtbot, monkeypatch, legacy):
    panel = desktop.roblox
    panel.count.setValue(1)
    panel.prepare()
    wait_idle(qtbot, panel)
    panel.record["settings"]["integration_monitoring"] = legacy
    path = panel.root / panel.current_run / "run.json"
    rovalid.atomic_json(path, panel.record)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    panel.start()
    wait_idle(qtbot, panel)
    assert panel.outcome == "error" and panel.record["state"] == "Prepared"
    assert rovalid.read_json(path)["settings"]["integration_monitoring"] == legacy
    guard = RobloxActivity(desktop.store.path)
    guard.block.write_bytes(b"http_429")
    panel.controls()
    assert not panel.start_button.isEnabled()
    monkeypatch.setattr(panel, "launch", lambda *a: pytest.fail("No restricted launch"))
    panel.start()
    assert guard.block.read_bytes() == b"http_429"


@pytest.mark.parametrize("legacy", [None, True, False])
def test_start_and_resume_accept_old_manifests_without_rewriting_them(desktop, monkeypatch, legacy):
    panel = desktop.roblox
    saved = panel.options()
    if legacy is None:
        saved.pop("integration_monitoring")
    else:
        saved["integration_monitoring"] = legacy
    panel.current_run = "a" * 32
    panel.record = dict(state="Prepared", settings=saved.copy())
    calls = []
    monkeypatch.setattr(panel, "launch", lambda *args: calls.append(args))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    panel.start()
    assert calls and panel.record["settings"] == saved
    panel.record["state"] = "Stopped"
    panel.resume()
    assert calls[-1][1]["resume"] and panel.record["settings"] == saved


@pytest.mark.parametrize("legacy", [True, False])
def test_launch_always_announces_live_progress_and_discards_stale_snapshot(desktop, monkeypatch, legacy):
    panel = desktop.roblox
    panel.current_run = "a" * 32
    panel.record = dict(settings=panel.options() | dict(integration_monitoring=legacy))
    panel.view_progress = (panel.current_run, dict(total=100, completed=70))
    monkeypatch.setattr(panel.process, "start", lambda: None)
    monkeypatch.setattr(panel.process, "write", lambda *a: 1)
    panel.launch("run", dict(resume=True))
    assert panel.view_progress == (panel.current_run, None)
    assert "العرض الحي" in panel.statistics.text()
    assert not panel.busy
