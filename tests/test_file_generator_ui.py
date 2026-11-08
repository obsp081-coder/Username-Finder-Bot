import threading

import pytest
import test_roblox_ui
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from username_finder.file_generator import generate_file
from username_finder.file_generator_output import proposed_output
from username_finder.file_generator_rules import GAMING, SOCIAL
from username_finder.roblox import prepare_inputs

window = test_roblox_ui.window


def wait(qtbot, panel):
    qtbot.waitUntil(lambda: not panel.busy, timeout=10_000)


def small_rule(panel):
    panel.primary.minimum.setValue(1)
    panel.primary.maximum.setValue(2)
    panel.primary.groups["lower"][0].setChecked(True)
    panel.primary.groups["lower"][1].setText("ab")
    for key in ("upper", "digits"):
        panel.primary.groups[key][0].setChecked(False)
    for enabled, _, _, _ in panel.primary.specials.values():
        enabled.setChecked(False)


def test_new_navigation_exact_lists_rtl_sizes_and_no_provider_calls(window, qtbot):
    panel = window.file_generator
    assert window.tabs.tabText(4) == "صانع ملفات اليوزرات"
    assert panel.layoutDirection() == Qt.LayoutDirection.RightToLeft
    assert tuple(panel.platforms[0].itemText(i) for i in range(panel.platforms[0].count())) == SOCIAL
    assert tuple(panel.platforms[1].itemText(i) for i in range(panel.platforms[1].count())) == GAMING
    window.tabs.setCurrentWidget(panel)
    for width, height in ((900, 650), (1180, 850)):
        window.resize(width, height)
        qtbot.wait(20)
        assert panel.horizontalScrollBar().maximum() == 0
    assert not window.roblox.test_calls and window.store.jobs() == []


def test_custom_override_reset_incomplete_warning_and_shared_components(window):
    panel = window.file_generator
    panel.platforms[0].setCurrentText("Discord")
    assert panel.mode.currentData() == "Platform Rules"
    assert panel.primary.minimum.value() == 2
    panel.primary.minimum.setValue(1)
    assert panel.mode.currentData() == "Custom Rules"
    assert "الطول" in panel.rule_warning.text()
    panel.mode.setCurrentIndex(0)
    with pytest.raises(ValueError):
        panel.config()
    panel.restore_defaults()
    assert panel.primary.minimum.value() == 2 and panel.mode.currentIndex() == 0
    panel.primary.groups["lower"][1].setText("ab")
    with pytest.raises(ValueError):
        panel.config()
    panel.restore_defaults()
    panel.platforms[0].setCurrentText("Tellonym")
    assert "Incomplete" in panel.metadata.text()
    with pytest.raises(ValueError):
        panel.config()
    panel.mode.setCurrentIndex(1)
    assert panel.config()["mode"] == "Custom Rules"
    panel.categories.setCurrentIndex(1)
    panel.platforms[1].setCurrentText("Valorant")
    assert not panel.secondary.isHidden()
    assert panel.secondary.minimum.value() == 3 and panel.secondary.maximum.value() == 5
    assert "مركّب" in panel.metadata.text()
    panel.platforms[1].setCurrentText("Minecraft")
    assert panel.variant.count() == 2
    panel.variant.setCurrentIndex(1)
    assert panel.profile.key == "xbox" and "غير مضمون" in panel.metadata.text()


def test_preview_preserves_configuration_confirmation_required_and_txt_csv(
    window, qtbot, tmp_path, monkeypatch
):
    panel = window.file_generator
    small_rule(panel)
    before = panel.primary.rule()
    panel.prepare(False)
    wait(qtbot, panel)
    assert "6" in panel.summary.text() and panel.primary.rule() == before
    panel.destination.setText(str(tmp_path))
    panel.format.setCurrentText("CSV")
    path = proposed_output(tmp_path, panel.config())
    defaults = []

    def decline(*args):
        defaults.append(args[-1])
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", decline)
    panel.prepare(True)
    wait(qtbot, panel)
    assert defaults == [QMessageBox.StandardButton.No] and not path.exists()
    assert not (tmp_path / "Username Finder Bot").exists()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    panel.prepare(True)
    wait(qtbot, panel)
    assert path.read_text().splitlines() == ["identifier", "a", "b", "aa", "ab", "ba", "bb"]
    # A generic generated file must still undergo Roblox-specific syntax validation.
    checked = prepare_inputs(files=[path])
    assert checked.total == 0 and checked.invalid_count == 6
    assert not window.roblox.test_calls and window.store.jobs() == []


def test_gui_resume_loads_saved_configuration_without_automatic_checking(
    window, qtbot, tmp_path, monkeypatch
):
    panel = window.file_generator
    panel.categories.setCurrentIndex(1)
    panel.platforms[1].setCurrentText("Roblox")
    small_rule(panel)
    panel.primary.minimum.setValue(5)
    panel.primary.maximum.setValue(5)
    panel.primary.groups["lower"][0].setChecked(False)
    panel.primary.groups["digits"][0].setChecked(True)
    cfg = panel.config()
    path = tmp_path / "resume.txt"
    cancel = threading.Event()
    record = generate_file(cfg, path, cancel=cancel, notify=lambda _: cancel.set())
    assert record["index"] < record["total"]
    panel.categories.setCurrentIndex(0)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(path) + ".generation.json", ""))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    panel.resume_file()
    wait(qtbot, panel)
    assert panel.platform == "Roblox" and panel.primary.minimum.value() == 5
    assert path.stat().st_size == 600_000 and panel.bar.value() == 1000
    assert not window.roblox.test_calls and window.store.jobs() == []


def test_large_roblox_preparation_keeps_gui_responsive_and_stops_after_one_mock_check(
    window, qtbot, tmp_path
):
    panel = window.roblox
    path = tmp_path / "large.txt"
    with path.open("w") as stream:
        for index in range(36_504):
            stream.write(f"Name{index:08d}\n")
    panel.modes.setCurrentIndex(1)
    panel.files.addItem(str(path))
    heartbeat = []
    timer = QTimer(window)
    timer.setInterval(10)
    timer.timeout.connect(lambda: heartbeat.append(True))
    timer.start()

    def fetch(_):
        from test_roblox import response

        panel.runner.cancel.set()
        return response()

    panel.fetcher = fetch
    panel.start()
    wait(qtbot, panel)
    timer.stop()
    result = panel.repo.progress(panel.current_run)
    assert result["total"] == 36_504 and result["counts"]["Done"] == 1
    assert len(heartbeat) >= 2
    assert window.store.jobs() == []


def test_advanced_collapses_without_mutating_any_rule_values(window):
    panel = window.file_generator
    assert panel.advanced.isHidden() and not panel.advanced_toggle.isChecked()
    panel.primary.prefix.setText("a")
    panel.primary.suffix.setText("b")
    panel.primary.specials["_"][1].setValue(2)
    panel.primary.specials["_"][2][0].setChecked(False)
    panel.primary.specials["_"][3].setChecked(True)
    before = panel.primary.rule()
    mode = panel.mode.currentData()
    for _ in range(3):
        panel.advanced_toggle.setChecked(True)
        assert not panel.advanced.isHidden()
        panel.advanced_toggle.setChecked(False)
        assert panel.advanced.isHidden()
    assert panel.primary.rule() == before and panel.mode.currentData() == mode
    assert panel.primary.prefix.toolTip() and panel.primary.minimum.parent() is not panel.advanced


def test_labels_match_social_player_and_compound_identity(window):
    panel = window.file_generator
    assert panel.primary.title() == "اسم المستخدم" and "GameName" not in panel.metadata.text()
    panel.categories.setCurrentIndex(1)
    panel.platforms[1].setCurrentText("PUBG Mobile")
    assert panel.primary.title() == "اسم اللاعب"
    panel.platforms[1].setCurrentText("Roblox")
    assert panel.primary.title() == "اسم المستخدم"
    panel.platforms[1].setCurrentText("Valorant")
    assert "GameName" in panel.primary.title() and "Tagline" in panel.secondary.title()
    assert "Riot ID" in panel.metadata.text()


def test_directory_picker_does_not_create_output_until_confirmation(window, tmp_path, monkeypatch):
    panel = window.file_generator
    small_rule(panel)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: str(tmp_path))
    panel.choose_file()
    assert panel.destination.text() == str(tmp_path)
    assert "Username Finder Bot" in panel.suggested_path.text()
    assert not (tmp_path / "Username Finder Bot").exists()


def test_numbered_generation_preserves_earlier_output_and_metadata(window, qtbot, tmp_path, monkeypatch):
    panel = window.file_generator
    small_rule(panel)
    panel.destination.setText(str(tmp_path))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    panel.prepare(True)
    wait(qtbot, panel)
    first = proposed_output(tmp_path, panel.config())
    assert first.name.endswith("_2.txt")
    panel.prepare(True)
    wait(qtbot, panel)
    assert first.read_text().splitlines() == ["a", "b", "aa", "ab", "ba", "bb"]
    from pathlib import Path

    assert len(list(first.parent.glob("*.txt"))) == 2
    assert not list(first.parent.glob("*.md"))
    assert Path(panel.suggested_path.text()) == first


def test_permission_failure_is_arabic_and_does_not_produce_file(window, qtbot, tmp_path, monkeypatch):
    from username_finder import file_generator_ui

    panel = window.file_generator
    small_rule(panel)
    panel.destination.setText(str(tmp_path))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)

    def denied(*args):
        raise PermissionError("denied")

    monkeypatch.setattr(file_generator_ui, "create_output_directory", denied)
    panel.prepare(True)
    wait(qtbot, panel)
    assert "إذن" in panel.summary.text()
    assert not (tmp_path / "Username Finder Bot").exists()
