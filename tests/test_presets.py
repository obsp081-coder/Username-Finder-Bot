import asyncio
import copy
import sqlite3
from dataclasses import asdict

import pytest
from PySide6.QtWidgets import QMessageBox

from username_finder.domain import Filters, SearchConfig
from username_finder.engine import ExecutionEngine
from username_finder.gui import MainWindow
from username_finder.presets import file_issues, make_payload, validate_payload
from username_finder.scheduling import ScheduleConfig
from username_finder.sources import make_snapshot
from username_finder.storage import Store


def payload():
    config = SearchConfig(
        filters=Filters(
            4,
            7,
            letters=False,
            digits=True,
            specials="_.-",
            prefix="a",
            suffix="9",
            required="0",
            repeat_target="0",
            repeat_count=3,
            repeat_exact=False,
            adjacency="separated",
        ),
        generation_mode="mixed",
        categories=("gaming_terms",),
        limit=12345,
        workers=71,
        auto_workers=False,
        queue_size=234,
        smart_history=False,
        history_ttl_hours=9.5,
        max_retries=3,
        retry_budget=67,
        timeout_seconds=24,
        circuit_threshold=9,
    )
    schedule = ScheduleConfig(True, "23:55", True, "03:15", True, 210)
    return make_payload(config, schedule, {"repeat_target": "0", "limit_text": "12345"})


def test_multiple_names_save_as_update_rename_delete_restart_and_roundtrip(tmp_path):
    path = tmp_path / "presets.db"
    store = Store(path)
    original = payload()
    first = store.save_preset("3 حروف", original)
    second = store.save_preset("يوزرات ألعاب", original)
    assert first != second
    changed = copy.deepcopy(original)
    changed["search"]["workers"] = 8
    store.save_preset("3 حروف", changed, first)
    assert store.preset(second)["payload"] == original
    store.rename_preset(first, "حروف وأرقام")
    store.close()
    reopened = Store(path)
    assert reopened.preset(first)["name"] == "حروف وأرقام"
    assert reopened.preset(first)["payload"] == changed
    config, schedule, editor = validate_payload(reopened.preset(second)["payload"])
    assert config.workers == 71 and config.filters.adjacency == "separated"
    assert asdict(schedule) == original["schedule"]
    assert editor["repeat_target"] == "0"
    reopened.delete_preset(first)
    assert [p["id"] for p in reopened.presets()] == [second]
    reopened.close()


@pytest.mark.parametrize("name", ["", "   ", "a\nb", "x" * 101])
def test_invalid_names_rejected(tmp_path, name):
    store = Store(tmp_path / "names.db")
    with pytest.raises(ValueError):
        store.save_preset(name, payload())
    store.close()


def test_duplicates_and_malformed_outdated_data_are_rejected(tmp_path):
    store = Store(tmp_path / "bad.db")
    first = store.save_preset("Example", payload())
    with pytest.raises(ValueError):
        store.save_preset(" example ", payload())
    second = store.save_preset("آخر", payload())
    with pytest.raises(ValueError):
        store.rename_preset(second, "EXAMPLE")
    with store.transaction() as db:
        db.execute("UPDATE presets SET payload=? WHERE id=?", ('{"version":99}', first))
    with pytest.raises(ValueError):
        store.preset(first)
    for data in [[], {}, {"version": 1, "search": []}, {**payload(), "version": 99}]:
        with pytest.raises(ValueError):
            validate_payload(data)
    data = payload()
    data["search"]["workers"] = "71"
    with pytest.raises(ValueError):
        validate_payload(data)
    store.close()


def test_txt_identity_missing_and_changed_sources_and_no_irrelevant_paths(tmp_path):
    file = tmp_path / "names.txt"
    file.write_text("abc\n", encoding="utf-8")
    data = make_payload(SearchConfig(source="txt", files=(str(file),)), ScheduleConfig(), {})
    assert file_issues(data) == []
    file.write_text("abd\n", encoding="utf-8")
    assert "تغير" in file_issues(data)[0]
    # A reference to a missing path is editable as a template; it is never treated as validated input.
    missing = tmp_path / "moved.txt"
    data["files"][0]["path"] = str(missing)
    data["search"]["files"] = [str(missing)]
    assert "غير متاح" in file_issues(data)[0]
    assert validate_payload(data)[0].files == (str(missing),)
    generated = make_payload(SearchConfig(files=(str(file),)), ScheduleConfig(), {})
    assert generated["search"]["files"] == [] and generated["files"] == []


def test_v1_migration_backup_preserves_jobs_results_and_history(tmp_path):
    path = tmp_path / "legacy.db"
    store = Store(path)
    config = SearchConfig(limit=7)
    job = store.create_job(config, make_snapshot(config))
    asyncio.run(ExecutionEngine(store).run(job))
    tables = ["jobs", "tasks", "attempts", "username_state"]
    before = {t: [tuple(r) for r in store.db.execute("SELECT * FROM " + t)] for t in tables}
    store.db.execute("DROP TABLE presets")
    store.db.execute("PRAGMA user_version=1")
    store.close()
    upgraded = Store(path)
    assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert before == {t: [tuple(r) for r in upgraded.db.execute("SELECT * FROM " + t)] for t in tables}
    backup = list((tmp_path / "Backups").glob("before-schema-2-*.db"))
    assert len(backup) == 1
    with sqlite3.connect(backup[0]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 7
    upgraded.close()
    reopened = Store(path)
    assert len(list((tmp_path / "Backups").glob("before-schema-2-*.db"))) == 1
    reopened.close()


def close_draft(window, monkeypatch):
    monkeypatch.setattr(window, "unsaved_choice", lambda: QMessageBox.StandardButton.Discard)
    window.close()


def test_gui_full_roundtrip_load_has_no_execution_or_schedule_side_effects(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path / "gui.db")
    qtbot.addWidget(window)
    config, schedule, editor = validate_payload(payload())
    window.apply_settings(config, schedule, editor)
    before = window.editor_signature()
    saved = window.store.save_preset("يوزرات ألعاب", payload())
    window.load_preset(saved)
    qtbot.waitUntil(lambda: window.preset_id == saved)
    assert window.editor_signature() == before
    assert not window.preset_dirty
    assert window.current_job is None and window.scheduler is None and not window.is_running()
    assert not window.store.jobs()
    assert window.read_schedule() == schedule
    window.advanced_toggle.setChecked(False)
    assert window.read_config() == config
    window.source.setCurrentIndex(1)
    window.source.setCurrentIndex(0)
    window.mode.setCurrentIndex(0)
    window.mode.setCurrentIndex(2)
    assert window.read_config() == config
    close_draft(window, monkeypatch)


def test_gui_save_save_as_update_rename_delete_and_unsaved_cancel(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path / "gui.db")
    qtbot.addWidget(window)
    window.show()
    window.workers.setValue(17)
    monkeypatch.setattr(window, "ask_preset_name", lambda *a: "الأول")
    window.save_preset()
    qtbot.waitUntil(lambda: window.preset_id is not None)
    first = window.preset_id
    window.workers.setValue(18)
    assert window.preset_dirty
    window.save_preset()
    qtbot.waitUntil(lambda: not window.busy)
    assert window.store.preset(first)["payload"]["search"]["workers"] == 18
    monkeypatch.setattr(window, "ask_preset_name", lambda *a: "الثاني")
    window.save_preset(save_as=True)
    qtbot.waitUntil(lambda: window.preset_id != first)
    second = window.preset_id
    window.workers.setValue(19)
    monkeypatch.setattr(window, "unsaved_choice", lambda: QMessageBox.StandardButton.Cancel)
    window.preset_selector.setCurrentIndex(window.preset_selector.findData(first))
    assert window.preset_id == second and window.workers.value() == 19 and window.preset_dirty
    assert not window.close() and window.isVisible()
    monkeypatch.setattr(window, "ask_preset_name", lambda *a: "اسم جديد")
    window.rename_preset()
    assert window.store.preset(second)["name"] == "اسم جديد"
    monkeypatch.setattr(window, "ask", lambda *a: QMessageBox.StandardButton.No)
    window.delete_preset()
    assert len(window.store.presets()) == 2
    monkeypatch.setattr(window, "ask", lambda *a: QMessageBox.StandardButton.Yes)
    window.delete_preset()
    assert len(window.store.presets()) == 1 and window.preset_id is None
    assert window.workers.value() == 19  # Deleting a template does not discard its editable settings.
    close_draft(window, monkeypatch)


def test_unsaved_switch_save_and_discard(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path / "switch.db")
    qtbot.addWidget(window)
    first = window.store.save_preset("أول", payload())
    second = window.store.save_preset("ثان", payload())
    window.load_preset(first)
    qtbot.waitUntil(lambda: window.preset_id == first)
    window.workers.setValue(100)
    monkeypatch.setattr(window, "unsaved_choice", lambda: QMessageBox.StandardButton.Save)
    window.refresh_presets()
    window.preset_selector.setCurrentIndex(window.preset_selector.findData(second))
    qtbot.waitUntil(lambda: window.preset_id == second)
    assert window.store.preset(first)["payload"]["search"]["workers"] == 100
    window.workers.setValue(101)
    monkeypatch.setattr(window, "unsaved_choice", lambda: QMessageBox.StandardButton.Discard)
    window.preset_selector.setCurrentIndex(window.preset_selector.findData(first))
    qtbot.waitUntil(lambda: window.preset_id == first)
    assert window.workers.value() == 100
    assert window.store.preset(second)["payload"]["search"]["workers"] == 71
    window.close()


def test_loaded_changed_txt_preset_blocks_job_creation(qtbot, tmp_path, monkeypatch):
    file = tmp_path / "names.txt"
    file.write_text("abc\n", encoding="utf-8")
    window = MainWindow(tmp_path / "input.db")
    qtbot.addWidget(window)
    data = make_payload(SearchConfig(source="txt", files=(str(file),)), ScheduleConfig(), {})
    saved = window.store.save_preset("ملف", data)
    file.write_text("abd\n", encoding="utf-8")
    window.load_preset(saved)
    qtbot.waitUntil(lambda: window.preset_id == saved)
    assert "تغير" in window.preset_notice.text()
    monkeypatch.setattr(window, "ask", lambda *a: QMessageBox.StandardButton.Ok)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: errors.append(a[-1]))
    window.prepare_job()
    qtbot.waitUntil(lambda: bool(errors))
    assert window.store.jobs() == [] and window.current_job is None
    window.close()


def test_inactive_advanced_values_survive_preset_save_and_load(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QTime

    ui = MainWindow(tmp_path / "inactive.db")
    qtbot.addWidget(ui)
    ui.repeat_target.setText("a")
    ui.repeat_count.setValue(4)
    ui.repeat_kind.setCurrentIndex(1)
    ui.limit.setText("9876")
    ui.limit_enabled.setChecked(False)
    ui.start_time.setTime(QTime(23, 12))
    ui.stop_time.setTime(QTime(4, 34))
    ui.duration_hours.setValue(5)
    ui.duration_minutes.setValue(7)
    ui.smart_history.setChecked(False)
    ui.ttl.setValue(9.5)
    before = ui.editor_signature()
    monkeypatch.setattr(ui, "ask_preset_name", lambda *a: "خيارات معطّلة")
    ui.save_preset()
    qtbot.waitUntil(lambda: ui.preset_id is not None)
    saved = ui.preset_id
    ui.load_preset(None)
    ui.load_preset(saved)
    qtbot.waitUntil(lambda: ui.preset_id == saved)
    assert ui.editor_signature() == before
    assert not ui.repeat_count.isEnabled() and not ui.start_time.isEnabled()
    ui.close()


def test_corrupt_preset_load_preserves_editor_and_can_be_deleted(qtbot, tmp_path, monkeypatch):
    ui = MainWindow(tmp_path / "corrupt.db")
    qtbot.addWidget(ui)
    saved = ui.store.save_preset("تالف", payload())
    with ui.store.transaction() as db:
        db.execute("UPDATE presets SET payload='broken' WHERE id=?", (saved,))
    before = ui.editor_signature()
    ui.load_preset(saved)
    qtbot.waitUntil(lambda: not ui.busy)
    assert ui.editor_signature() == before
    assert "تالفة" in ui.preset_notice.text()
    monkeypatch.setattr(ui, "ask", lambda *a: QMessageBox.StandardButton.Yes)
    ui.delete_preset()
    assert ui.store.presets() == []
    close_draft(ui, monkeypatch)


def test_loading_preset_during_run_does_not_change_job_or_active_schedule(qtbot, tmp_path, monkeypatch):
    ui = MainWindow(tmp_path / "running.db")
    qtbot.addWidget(ui)
    running_config = SearchConfig(limit=500, workers=1)
    job = ui.store.create_job(running_config, make_snapshot(running_config))
    ui.load_job(job)
    original_schedule = ui.scheduler.config
    ui.start_engine()
    qtbot.waitUntil(lambda: ui.store.job(job)["state"] == "Running")
    saved = ui.store.save_preset("جدولة أخرى", payload())
    ui.load_preset(saved)
    qtbot.waitUntil(lambda: ui.preset_id == saved)
    assert ui.current_job == job and ui.is_running()
    assert SearchConfig.from_dict(ui.store.job(job)["config"]) == running_config
    assert ui.scheduler.config == original_schedule
    assert len(ui.store.jobs()) == 1
    # Drain the queued GUI completion before closing the isolated database.
    with qtbot.waitSignal(ui.bridge.finished, timeout=3000):
        ui.stop_engine()
    qtbot.waitUntil(lambda: not ui.is_running())
    close_draft(ui, monkeypatch)


def test_save_cannot_silently_adopt_changed_preset_input(qtbot, tmp_path, monkeypatch):
    file = tmp_path / "names.txt"
    file.write_text("abc\n", encoding="utf-8")
    ui = MainWindow(tmp_path / "save-files.db")
    qtbot.addWidget(ui)
    saved = ui.store.save_preset(
        "ملفات", make_payload(SearchConfig(source="txt", files=(str(file),)), ScheduleConfig(), {})
    )
    original = ui.store.preset(saved)["payload"]
    ui.load_preset(saved)
    qtbot.waitUntil(lambda: ui.preset_id == saved)
    file.write_text("abd\n", encoding="utf-8")
    ui.workers.setValue(22)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: errors.append(a[-1]))
    ui.save_preset()
    qtbot.waitUntil(lambda: bool(errors))
    assert ui.store.preset(saved)["payload"] == original
    assert ui.preset_dirty
    close_draft(ui, monkeypatch)
