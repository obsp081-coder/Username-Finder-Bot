import asyncio
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from username_finder.domain import Classification, Evidence, Filters, JobState, SearchConfig
from username_finder.engine import ExecutionEngine
from username_finder.providers import MockVerifier, ProviderFailure
from username_finder.sources import InputRecord, make_snapshot
from username_finder.storage import Store


def new_job(store, **kwargs):
    config = SearchConfig(filters=Filters(3, 3), smart_history=False, **kwargs)
    return config, store.create_job(config, make_snapshot(config))


def test_result_and_completion_rollback_on_disk_full(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    config, job = new_job(store, limit=1)
    store.ingest(job, InputRecord("abc", {"rank": "1"}), config, "simulated", "local-mock", "1")
    task = store.pending(job)[0]
    store.claim(task["id"])
    original = store._save_counters

    def fail(*args):
        raise sqlite3.OperationalError("disk full")

    store._save_counters = fail
    with pytest.raises(sqlite3.OperationalError):
        store.finish(
            task["id"],
            Evidence(Classification.TAKEN, "explicitly_taken", "local-mock", "1", "simulated", True),
        )
    store._save_counters = original
    assert store.results(job, mode="simulated") == []
    assert store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert store.db.execute("SELECT state FROM tasks").fetchone()[0] == "Checking"
    store.recover()
    asyncio.run(ExecutionEngine(store).run(job))
    assert store.job(job)["counters"]["completed"] == 1
    store.close()


def test_engine_stops_sending_on_writer_failure(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    _, job = new_job(store, limit=40, workers=2, auto_workers=False, queue_size=8)
    original = store.finish
    calls = 0

    def fail(task, evidence, **kwargs):
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("disk full")

    store.finish = fail
    engine = ExecutionEngine(store)
    asyncio.run(engine.run(job))
    assert store.job(job)["state"] == JobState.BLOCKED
    assert store.job(job)["counters"]["dispatched"] <= 2
    assert store.pending_count(job) <= 8
    store.finish = original
    store.close()


def test_hard_process_exit_after_commit_and_during_transaction(tmp_path):
    path = tmp_path / "db.sqlite"
    store = Store(path)
    config, job = new_job(store, limit=2)
    store.close()
    # The child dies without closing SQLite, leaving a committed cursor plus one rolled-back task.
    script = """
import os,sys
from username_finder.storage import Store
from username_finder.domain import SearchConfig
from username_finder.sources import InputRecord
s=Store(sys.argv[1]); job=sys.argv[2]; c=SearchConfig.from_dict(s.job(job)['config'])
s.ingest(job,InputRecord('aaa',{'stage':'all','rank':'1'}),c,'simulated','local-mock','1')
s.claim(s.pending(job)[0]['id'])
s.db.execute('BEGIN IMMEDIATE')
s.db.execute("INSERT INTO tasks(job_id,username,state) VALUES(?,?,'Pending')",(job,'aab'))
os._exit(23)
"""
    child = subprocess.run([sys.executable, "-c", script, str(path), job], timeout=10, capture_output=True)
    assert child.returncode == 23, child.stderr.decode()
    store = Store(path)
    store.recover()
    assert store.job(job)["cursor"]["rank"] == "1"
    assert store.pending_count(job) == 1
    asyncio.run(ExecutionEngine(store).run(job))
    assert store.job(job)["counters"]["completed"] == 2
    assert len(store.results(job, mode="simulated")) == 2
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
    store.close()


def test_unknown_never_skipped_expiry_version_and_quarantine(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    for name, status, kind, date in [
        ("abc", Classification.TAKEN, "explicitly_taken", datetime.now(UTC) - timedelta(hours=25)),
        ("abd", Classification.UNKNOWN, "network_error", datetime.now(UTC)),
        ("abe", Classification.TAKEN, "explicitly_taken", datetime.now(UTC)),
    ]:
        config, job = new_job(store, limit=1)
        store.ingest(job, InputRecord(name, {}), config, "simulated", "local-mock", "1")
        task = store.pending(job)[0]
        store.claim(task["id"])
        store.finish(
            task["id"],
            Evidence(
                status,
                kind,
                "local-mock",
                "1",
                "simulated",
                status != Classification.UNKNOWN,
                checked_at=date.isoformat(),
            ),
        )
    config = replace(config, smart_history=True)
    for name, version, mode in [
        ("abc", "1", "simulated"),
        ("abd", "1", "simulated"),
        ("abe", "2", "simulated"),
        ("abe", "1", "real"),
    ]:
        job = store.create_job(config, make_snapshot(config))
        assert store.ingest(job, InputRecord(name, {}), config, mode, "local-mock", version) == "candidate"
    store.quarantine("local-mock", "1")
    job = store.create_job(config, make_snapshot(config))
    assert store.ingest(job, InputRecord("abe", {}), config, "simulated", "local-mock", "1") == "candidate"
    store.close()


def test_timeout_403_semantic_failure_and_bounded_stop(tmp_path):
    class Slow(MockVerifier):
        async def verify(self, username):
            await asyncio.sleep(30)
            return await super().verify(username)

    class Denied(MockVerifier):
        async def verify(self, username):
            raise ProviderFailure("403", retryable=False)

    class Semantic(MockVerifier):
        async def verify(self, username):
            raise ProviderFailure("SEMANTIC_MISMATCH", retryable=False)

    store = Store(tmp_path / "db.sqlite")
    _, timed = new_job(store, limit=2, workers=1, max_retries=0, timeout_seconds=0.01)
    asyncio.run(ExecutionEngine(store, Slow()).run(timed))
    assert store.job(timed)["state"] == JobState.COMPLETED
    assert {r["classification"] for r in store.results(timed, mode="simulated")} == {"Unknown"}
    _, denied = new_job(store, limit=2)
    asyncio.run(ExecutionEngine(store, Denied()).run(denied))
    assert store.job(denied)["state"] == JobState.BLOCKED
    _, semantic = new_job(store, limit=2)
    asyncio.run(ExecutionEngine(store, Semantic()).run(semantic))
    assert store.provider_quarantined("local-mock", "1")
    store.close()


def test_pause_cancels_slow_read_only_checks_and_preserves_tasks(tmp_path):
    class Slow(MockVerifier):
        async def verify(self, username):
            await asyncio.sleep(60)
            return await super().verify(username)

    store = Store(tmp_path / "db.sqlite")
    _, job = new_job(store, limit=20, workers=2, auto_workers=False, queue_size=4)
    engine = ExecutionEngine(store, Slow())

    async def pause():
        running = asyncio.create_task(engine.run(job))
        await asyncio.sleep(0.02)
        engine.request_pause()
        await asyncio.wait_for(running, timeout=2)

    asyncio.run(pause())
    assert store.job(job)["state"] == JobState.PAUSED
    assert store.pending_count(job) > 0
    asyncio.run(ExecutionEngine(store).run(job))
    assert store.job(job)["counters"]["completed"] == 20
    store.close()


def test_csv_formula_neutralization_and_empty_real_exports(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    config, job = new_job(store, limit=1)
    store.ingest(job, InputRecord("=sum(1)", {}), config, "simulated", "local-mock", "1")
    files = store.export(tmp_path / "export", mode="simulated", job_id=job)
    text = files[-1].read_text(encoding="utf-8-sig")
    assert "'=sum(1)" in text
    real = store.export(tmp_path / "export", mode="real", job_id=job)
    assert all(not p.read_text(encoding="utf-8") for p in real[:-1])
    store.close()


@pytest.mark.parametrize("workers", [5, 20, 100, 500, 1000])
def test_bounded_pipeline_reaches_requested_local_mock_concurrency(tmp_path, workers):
    class BarrierMock(MockVerifier):
        def __init__(self):
            super().__init__(0)
            self.arrived = 0
            self.gate = asyncio.Event()

        async def verify(self, username):
            self.arrived += 1
            if self.arrived == workers:
                self.gate.set()
            await self.gate.wait()
            return await super().verify(username)

    store = Store(tmp_path / "db.sqlite")
    _, job = new_job(
        store, limit=workers, workers=workers, auto_workers=False, queue_size=workers, timeout_seconds=10
    )
    provider = BarrierMock()
    engine = ExecutionEngine(store, provider)
    asyncio.run(engine.run(job))
    assert provider.arrived == workers
    assert engine.max_inflight == workers
    assert engine.max_pending <= workers
    assert store.job(job)["state"] == JobState.COMPLETED
    assert store.job(job)["counters"]["completed"] == workers
    store.close()


def test_duplicate_engine_start_cannot_change_running_job(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    _, job = new_job(store, limit=5, workers=1)
    first = ExecutionEngine(store, MockVerifier(0.05))

    async def attempt():
        task = asyncio.create_task(first.run(job))
        await asyncio.sleep(0.01)
        await ExecutionEngine(store).run(job)
        assert store.job(job)["state"] == JobState.RUNNING
        await task

    asyncio.run(attempt())
    assert store.job(job)["counters"]["completed"] == 5
    store.close()


def test_quarantined_available_is_never_exported_as_trustworthy_available(tmp_path):
    store = Store(tmp_path / "quarantine.db")
    config, job = new_job(store, limit=1)
    store.ingest(job, InputRecord("abc", {}), config, "simulated", "local-mock", "1")
    task = store.pending(job)[0]
    store.claim(task["id"])
    store.finish(
        task["id"],
        Evidence(Classification.AVAILABLE, "explicitly_available", "local-mock", "1", "simulated", True),
    )
    store.quarantine("local-mock", "1")
    assert store.results(job, mode="simulated", classification="Available") == []
    result = store.results(job, mode="simulated", classification="Unknown")[0]
    assert result["classification"] == "Available"  # Historical evidence is retained.
    assert result["effective_classification"] == "Unknown"
    assert not result["trustworthy"]
    files = store.export(tmp_path / "export", mode="simulated", job_id=job)
    assert files[0].read_text(encoding="utf-8") == ""
    assert files[4].read_text(encoding="utf-8") == "abc\n"
    store.close()


def test_single_instance_lock_and_backup_target_guards(tmp_path):
    from username_finder.instance import InstanceLock

    path = tmp_path / "session.lock"
    first = InstanceLock(path)
    with pytest.raises(RuntimeError):
        InstanceLock(path)
    first.close()
    again = InstanceLock(path)
    again.close()
    store = Store(tmp_path / "db.sqlite")
    with pytest.raises(ValueError):
        store.backup(store.path)
    with pytest.raises(ValueError):
        store.restore(store.path, tmp_path / "backup.db")
    store.close()


def test_committed_result_survives_hard_exit_without_rechecking(tmp_path):
    path = tmp_path / "db.sqlite"
    store = Store(path)
    _, job = new_job(store, limit=2)
    store.close()
    script = """
import os,sys
from username_finder.storage import Store
from username_finder.domain import SearchConfig,Evidence,Classification
from username_finder.sources import InputRecord
s=Store(sys.argv[1]); j=sys.argv[2]; c=SearchConfig.from_dict(s.job(j)['config'])
s.ingest(j,InputRecord('aaa',{'stage':'all','rank':'1'}),c,'simulated','local-mock','1')
t=s.pending(j)[0]; s.claim(t['id'])
s.finish(t['id'],Evidence(Classification.TAKEN,'explicitly_taken','local-mock','1','simulated',True))
os._exit(24)
"""
    child = subprocess.run([sys.executable, "-c", script, str(path), job], timeout=10, capture_output=True)
    assert child.returncode == 24
    store = Store(path)
    store.recover()
    asyncio.run(ExecutionEngine(store).run(job))
    assert store.job(job)["counters"]["completed"] == 2
    assert store.db.execute("SELECT COUNT(*) FROM attempts WHERE username='aaa'").fetchone()[0] == 1
    store.close()


def test_retry_after_is_respected_by_retried_request(tmp_path):
    import time

    class ThrottledOnce(MockVerifier):
        def __init__(self):
            super().__init__(0)
            self.sent_at = []

        async def verify(self, username):
            self.sent_at.append(time.monotonic())
            if len(self.sent_at) == 1:
                raise ProviderFailure("429", retry_after=0.15)
            return await super().verify(username)

    store = Store(tmp_path / "retry.db")
    _, job = new_job(store, limit=1, workers=1)
    provider = ThrottledOnce()
    asyncio.run(ExecutionEngine(store, provider).run(job))
    assert len(provider.sent_at) == 2
    assert provider.sent_at[1] - provider.sent_at[0] >= 0.15
    assert store.job(job)["state"] == JobState.COMPLETED
    store.close()
