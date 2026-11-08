"""Indexed restart recovery and native startup against temporary large databases."""

import json
import os
import time
from contextlib import contextmanager

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox
from test_reliability_stress import memory_bytes

from username_finder import x
from username_finder.gui import MainWindow
from username_finder.overnight import OvernightStore
from username_finder.overnight_preflight import JobIntent, check_job
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store
from username_finder.x_storage import XStore


def seed(store, prefix, identity, count, state="Ready"):
    """Populate real schemas with unique, valid five-character names in bounded SQL batches."""
    if prefix == "x":
        repo = XStore(store)
        saved = repo.prepare(x.input_spec(""))
        with store.transaction() as db:
            db.execute("DELETE FROM x_runs WHERE id=?", (saved,))
            db.execute(
                "INSERT INTO x_runs(id,created_at,state,spec,skip_previous,interval_seconds,cursor) "
                "VALUES(?,?,?, ?,1,5,?)",
                (identity, x.now(), state, json.dumps(x.input_spec("fixture")), count),
            )
    else:
        repo = RobloxStore(store)
        with store.transaction() as db:
            db.execute(
                "INSERT INTO roblox_runs(id,created_at,updated_at,kind,state,skip_previous,"
                "interval_seconds,preparation,input_cursor) VALUES(?,?,?,'normal',?,1,0,?,?)",
                (identity, x.now(), x.now(), state, json.dumps({"manual": "fixture", "files": []}), count),
            )
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789_"
    expression = "||".join(f"substr('{alphabet}',(n/{37**power})%37+1,1)" for power in reversed(range(5)))
    if prefix == "roblox":
        # Roblox seed uses only letters/digits and an initial letter.
        expression = "substr('abcdefghijklmnopqrstuvwxyz',n/1679616+1,1)||" + "||".join(
            f"substr('abcdefghijklmnopqrstuvwxyz0123456789',(n/{36**power})%36+1,1)"
            for power in reversed(range(4))
        )
    for start in range(0, count, 10000):
        with store.transaction() as db:
            db.execute(
                f"WITH RECURSIVE numbers(n) AS (SELECT ? UNION ALL SELECT n+1 FROM numbers WHERE n<?) "
                f"INSERT INTO {prefix}_queue SELECT ?,n+1,{expression},{expression},'Pending' FROM numbers",
                (start, min(start + 10000, count) - 1, identity),
            )
    return repo


@pytest.mark.parametrize("prefix", ["roblox", "x"])
@pytest.mark.parametrize("inflight", [0, 1, 125])
def test_indexed_recovery_is_bounded_idempotent_and_preserves_states(tmp_path, prefix, inflight):
    store = Store(tmp_path / "recovery.db")
    try:
        repo = seed(store, prefix, "active", 300, "Running")
        night = OvernightStore(store, "Roblox" if prefix == "roblox" else "X")
        for state in ("Ready", "Completed", "Restricted", "PreparationPaused", "Preparing", "Paused"):
            with store.transaction() as db:
                if prefix == "x":
                    db.execute(
                        "INSERT INTO x_runs SELECT ?,created_at,?,spec,skip_previous,interval_seconds,"
                        "0,0,0,0,0,'' FROM x_runs WHERE id='active'",
                        (state, state),
                    )
                else:
                    db.execute(
                        "INSERT INTO roblox_runs(id,created_at,updated_at,kind,state,skip_previous) "
                        "VALUES(?,?,?,'normal',?,1)",
                        (state, x.now(), x.now(), state),
                    )
        night.enable("Paused", True)
        night.stop("Paused")
        with store.transaction() as db:
            db.execute(
                f"UPDATE {prefix}_queue SET state='InFlight' WHERE run_id='active' AND position<=?",
                (inflight,),
            )
        sql = (
            f"SELECT * FROM {prefix}_queue WHERE run_id IN (SELECT id FROM {prefix}_runs) "
            "AND state='InFlight' LIMIT 100"
        )
        plan = [row[3] for row in store.db.execute("EXPLAIN QUERY PLAN " + sql)]
        assert any(f"SEARCH {prefix}_queue USING INDEX {prefix}_pending" in item for item in plan)
        assert not any(f"SCAN {prefix}_queue" in item for item in plan)
        schema = list(store.db.execute("SELECT * FROM sqlite_master ORDER BY name"))
        durations = []
        original_transaction = store.transaction

        @contextmanager
        def timed_transaction(**kwargs):
            begin = time.perf_counter()
            with original_transaction(**kwargs) as db:
                yield db
            durations.append(time.perf_counter() - begin)

        store.transaction = timed_transaction
        started = time.perf_counter()
        repo.recover()
        elapsed = time.perf_counter() - started
        store.transaction = original_transaction
        records = list(store.db.execute(f"SELECT * FROM {prefix}_checks ORDER BY id"))
        assert len(records) == inflight
        assert all(row["classification"] == "Unknown" for row in records)
        assert all(json.loads(row["evidence"])["delivery_uncertain"] for row in records)
        for _ in range(2):
            repo.recover()
        assert records == list(store.db.execute(f"SELECT * FROM {prefix}_checks ORDER BY id"))
        physical = dict(store.db.execute(f"SELECT state,COUNT(*) FROM {prefix}_queue GROUP BY state"))
        progress = repo.progress("active")
        assert progress["total"] == 300 and physical.get("Done", 0) == inflight
        assert physical.get("Pending", 0) == 300 - inflight
        assert progress.get("counts", physical) == physical
        states = dict(store.db.execute(f"SELECT id,state FROM {prefix}_runs"))
        assert states["Completed"] == "Completed" and states["Ready"] == "Ready"
        assert states["Restricted"] == "Restricted" and states["Preparing"] == "PreparationPaused"
        assert states["PreparationPaused"] == "PreparationPaused" and states["Paused"] == "Paused"
        assert not night.startup_candidates(repo)
        assert schema == list(store.db.execute("SELECT * FROM sqlite_master ORDER BY name"))
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
        print(
            f"RECOVERY_{prefix}_{inflight}={elapsed:.6f}s MAX_TRANSACTION={max(durations):.6f}s PLAN={plan}"
        )
    finally:
        store.close()


def test_progress_counts_match_sql_after_filtering_and_runtime_state_changes(tmp_path):
    store = Store(tmp_path / "counts.db")
    try:
        for prefix in ("roblox", "x"):
            repo = seed(store, prefix, prefix, 300)
            with store.transaction() as db:
                db.execute(f"UPDATE {prefix}_queue SET state='Done' WHERE position<=12")
                db.execute(f"UPDATE {prefix}_queue SET state='InFlight' WHERE position=13")
                if prefix == "roblox":
                    db.execute("UPDATE roblox_queue SET state='Skipped' WHERE position=14")
            actual = dict(store.db.execute(f"SELECT state,COUNT(*) FROM {prefix}_queue GROUP BY state"))
            progress = repo.progress(prefix)
            assert progress["total"] == sum(actual.values()) == 300
            if prefix == "roblox":
                assert progress["counts"] == actual
            else:
                assert progress["completed"] == actual["Done"] and progress["pending"] == actual["Pending"]
    finally:
        store.close()


@pytest.mark.parametrize("prefix", ["roblox", "x"])
def test_preflight_uses_state_seeks_and_still_rejects_missing_or_unknown_states(tmp_path, prefix):
    store = Store(tmp_path / "preflight.db")
    try:
        repo = seed(store, prefix, prefix, 10000)
        platform = "Roblox" if prefix == "roblox" else "X"
        night = OvernightStore(store, platform)
        night.enable(prefix, True)
        intent = JobIntent(platform, prefix)
        # A pending-range GROUP BY executes far more VM steps than these indexed probes.
        steps = [0]

        def bounded():
            steps[0] += 1000
            return int(steps[0] > 5000)

        store.db.set_progress_handler(bounded, 1000)
        result = check_job(repo, night, intent)
        store.db.set_progress_handler(None, 0)
        assert not result.errors, result.text()
        with store.transaction() as db:
            db.execute(f"UPDATE {prefix}_queue SET state='Unexpected' WHERE position=1")
        assert check_job(repo, night, intent).errors
        with store.transaction() as db:
            db.execute(f"DELETE FROM {prefix}_queue")
        assert check_job(repo, night, intent).errors
    finally:
        store.db.set_progress_handler(None, 0)
        store.close()


def test_native_repeated_startup_on_large_pending_queues(qtbot, tmp_path, monkeypatch):
    large = bool(os.environ.get("UFB_STARTUP_SCALE"))
    counts = {"roblox": 1772928 if large else 1000, "x": 10000000 if large else 2000}
    path = tmp_path / "startup.db"
    store = Store(path)
    before_memory = memory_bytes()
    try:
        for prefix, count in counts.items():
            seed(store, prefix, prefix, count, "PreparationPaused" if prefix == "x" else "Ready")
            OvernightStore(store, "Roblox" if prefix == "roblox" else "X")
        size_before = path.stat().st_size
        pages_before = store.db.execute("PRAGMA page_count").fetchone()[0]
        plans = {
            prefix: [
                r[3]
                for r in store.db.execute(
                    f"EXPLAIN QUERY PLAN SELECT * FROM {prefix}_queue "
                    f"WHERE run_id IN (SELECT id FROM {prefix}_runs) AND state='InFlight' LIMIT 100"
                )
            ]
            for prefix in counts
        }
    finally:
        store.close()
    monkeypatch.setattr(MainWindow, "unsaved_choice", lambda _: QMessageBox.StandardButton.Discard)
    monkeypatch.setattr(x.protocol, "fetch", lambda *a, **kw: pytest.fail("No live X requests"))
    monkeypatch.setattr(
        "username_finder.roblox_protocol.fetch", lambda *a, **kw: pytest.fail("No live Roblox requests")
    )
    measurements = []
    for _iteration in range(2):
        ticks = [time.perf_counter()]
        timer = QTimer()
        timer.timeout.connect(lambda ticks=ticks: ticks.append(time.perf_counter()))
        timer.start(20)
        at = time.perf_counter()
        window = MainWindow(path)
        qtbot.addWidget(window)
        window.show()
        qtbot.waitUntil(window.isVisible)
        startup = time.perf_counter() - at
        try:
            for panel in (window.roblox, window.x_panel, window.job_manager):
                window.tabs.setCurrentWidget(panel)
                qtbot.wait(30)
            assert not window.roblox.busy and not window.x_panel.busy
            assert window.roblox.repo.progress("roblox")["total"] == counts["roblox"]
            assert window.x_panel.repo.progress("x")["state"] == "PreparationPaused"
            assert window.x_panel.repo.progress("x")["total"] == counts["x"]
            assert window.store.db.execute("PRAGMA page_count").fetchone()[0] == pages_before
            assert startup < 3
        finally:
            timer.stop()
            ticks.append(time.perf_counter())
            window.close()
            qtbot.waitUntil(lambda window=window: window.closed, timeout=5000)
        measurements.append(
            dict(
                startup_seconds=round(startup, 4),
                max_gap=round(max(b - a for a, b in zip(ticks, ticks[1:], strict=False)), 4),
            )
        )
    print(
        "LARGE_STARTUP="
        + json.dumps(
            dict(
                counts=counts,
                measurements=measurements,
                memory_before=before_memory,
                memory_after=memory_bytes(),
                database_bytes=size_before,
                additional_index_bytes=0,
                plans=plans,
            )
        )
    )
