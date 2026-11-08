"""Evidence-backed overnight lifecycle and persistence regressions."""

import sqlite3
import threading

import pytest
from test_overnight import execute
from test_overnight import rigs as rigs
from test_roblox import response as roblox_response
from test_x import response as x_response

from username_finder import x
from username_finder.overnight import OvernightStore
from username_finder.roblox import result_for


def commit(rig, task):
    if rig.platform == "Roblox":
        rig.repo.finish_check(
            task,
            result_for(task["username"], roblox_response(), checked_at=rig.clock.now().isoformat()),
            response_finished_at=rig.clock.now(),
        )
    else:
        rig.repo.finish(
            task, x.result_for(task["username"], x_response(), checked_at=rig.clock.now().isoformat())
        )


@pytest.mark.parametrize("platform", ["Roblox", "X"])
@pytest.mark.parametrize("end_committed", [False, True])
def test_last_result_commit_and_job_completion_reconcile_after_crash(rigs, platform, end_committed):
    rig = rigs(platform, ["LastName"])
    rig.repo.start(rig.identity)
    rig.night.begin(rig.identity, clock=rig.clock.now)
    task = rig.repo.claim_next(rig.identity) if platform == "Roblox" else rig.repo.claim(rig.identity)
    commit(rig, task)
    if end_committed:
        rig.repo.end(rig.identity, "Completed")
    # Crash before the overnight completion metadata was saved.
    rig.repo.recover()
    assert rig.night.startup_candidates(rig.repo) == []
    assert rig.repo.progress(rig.identity)["state"] == "Completed"
    assert rig.night.get(rig.identity)["state"] == "Completed"
    assert len(rig.repo.history("LastName")) == 1
    assert not rig.calls


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_existing_overnight_schema_does_not_need_writer_lock(rigs, platform):
    rig = rigs(platform)
    writer = sqlite3.connect(rig.store.path)
    rig.store.db.execute("PRAGMA busy_timeout=20")
    try:
        writer.execute("BEGIN IMMEDIATE")
        # Runner construction happens on the GUI thread; an existing schema only needs a read.
        second = OvernightStore(rig.store, platform)
        assert second.get(rig.identity)["enabled"]
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_failed_retry_write_rolls_back_candidate_and_budget_together(rigs, platform):
    from username_finder.overnight import transport_failure
    from username_finder.x_protocol import Response

    rig = rigs(platform)
    rig.repo.start(rig.identity)
    rig.night.begin(rig.identity, clock=rig.clock.now)
    task = rig.repo.claim_next(rig.identity) if platform == "Roblox" else rig.repo.claim(rig.identity)
    rig.store.db.execute("PRAGMA busy_timeout=20")
    writer = sqlite3.connect(rig.store.path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            rig.night.retry(
                task, transport_failure(Response(error="timeout"), TimeoutError()), rig.clock.now()
            )
        row = rig.store.db.execute(
            f"SELECT state FROM {rig.night.prefix}_queue WHERE run_id=? AND position=?",
            (rig.identity, task["position"]),
        ).fetchone()
        assert row[0] == "InFlight"
        assert rig.night.get(rig.identity)["attempts"] == 0
    finally:
        writer.rollback()
        writer.close()
    rig.repo.recover()
    assert not rig.night.startup_candidates(rig.repo)
    assert rig.repo.results()[0][0]["classification"] == "Unknown"


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_automatic_restart_keeps_retry_budget_and_deadline_across_cycles(rigs, platform):
    rig = rigs(platform)
    execute(rig, [TimeoutError()], wait=lambda _: True)
    for count in (2, 3):
        rig.repo.recover()
        assert rig.night.startup_candidates(rig.repo) == [rig.identity]

        def wait(seconds, count=count):
            # Honor the saved wait, then interrupt the next newly scheduled retry wait.
            if seconds < 31 and count == 2 or seconds < 61 and count == 3:
                rig.clock.wait(seconds)
                return False
            return True

        execute(rig, [TimeoutError()], automatic=True, wait=wait)
        assert rig.night.get(rig.identity)["attempts"] == count
    rig.repo.recover()
    execute(rig, [TimeoutError()], automatic=True)
    assert rig.night.get(rig.identity)["state"] == "Review"
    assert len(rig.calls) == 4
    assert [round(t - rig.calls[i - 1][1], 2) for i, (_, t) in enumerate(rig.calls) if i] == [
        30.25,
        60.25,
        120.25,
    ]


def test_writer_lock_wait_does_not_block_saved_result_reads(rigs):
    rig = rigs("Roblox")
    rig.store.db.execute("PRAGMA busy_timeout=600")
    attempted, read_done = threading.Event(), threading.Event()
    errors = []
    rig.store.db.set_trace_callback(lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None)
    external = sqlite3.connect(rig.store.path)
    external.execute("BEGIN IMMEDIATE")

    def write():
        try:
            with rig.store.transaction() as db:
                db.execute("UPDATE roblox_runs SET error='contention-test' WHERE id=?", (rig.identity,))
        except Exception as exc:
            errors.append(exc)

    def read():
        rig.repo.results()
        read_done.set()

    worker, reader = threading.Thread(target=write), threading.Thread(target=read)
    worker.start()
    try:
        assert attempted.wait(1)
        reader.start()
        readable_while_locked = read_done.wait(0.2)
    finally:
        external.rollback()
        external.close()
        worker.join(2)
        reader.join(2)
        rig.store.db.set_trace_callback(None)
    assert readable_while_locked, "A waiting background writer held the shared read mutex"
    assert not errors
    assert rig.repo.progress(rig.identity)["error"] == "contention-test"
