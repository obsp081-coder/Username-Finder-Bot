import asyncio
import csv
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from username_finder.domain import Classification, Evidence, Filters, JobState, SearchConfig
from username_finder.engine import ExecutionEngine
from username_finder.providers import MockVerifier, ProviderFailure
from username_finder.sources import CandidateSource, InputRecord, make_snapshot
from username_finder.storage import Store


class StorageEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "history.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def job(self, **kwargs):
        config = SearchConfig(filters=Filters(3, 3, prefix="a"), **kwargs)
        return config, self.store.create_job(config, make_snapshot(config))

    def test_streaming_txt_counters_bom_errors_and_duplicates(self):
        a = self.root / "a.txt"
        b = self.root / "b.txt"
        a.write_bytes(b"\xef\xbb\xbfAbc\r\nabc\n\n@abc\naaa\nxyz\n\xff\n")
        b.write_text("ABC\naab\n", encoding="utf-8")
        config = SearchConfig(
            source="txt", files=(str(a), str(b)), filters=Filters(3, 3, prefix="a"), limit=None
        )
        job_id = self.store.create_job(config, make_snapshot(config))
        asyncio.run(ExecutionEngine(self.store).run(job_id))
        job = self.store.job(job_id)
        self.assertEqual(job["state"], JobState.COMPLETED)
        for key, expected in {
            "lines": 9,
            "blank": 1,
            "malformed": 1,
            "invalid": 1,
            "filtered": 1,
            "duplicate": 2,
            "unique": 3,
            "completed": 4,
            "dispatched": 3,
        }.items():
            self.assertEqual(job["counters"][key], expected, key)

    def test_changed_file_rejected_and_cursor_transactional(self):
        path = self.root / "names.txt"
        path.write_text("abc\nabd\n", encoding="utf-8")
        config = SearchConfig(source="txt", files=(str(path),))
        snapshot = make_snapshot(config)
        source = CandidateSource(config, snapshot)
        record = source.next_record()
        job = self.store.create_job(config, snapshot)
        # Failure before commit must leave both cursor and task unchanged.
        original = self.store._save_counters

        def fail(*args):
            raise sqlite3.OperationalError("disk full")

        self.store._save_counters = fail
        with self.assertRaises(sqlite3.OperationalError):
            self.store.ingest(job, record, config, "simulated", "local-mock", "1")
        self.store._save_counters = original
        self.assertEqual(self.store.job(job)["cursor"], {})
        self.assertEqual(self.store.pending_count(job), 0)
        self.store.ingest(job, record, config, "simulated", "local-mock", "1")
        source.close()
        resumed = CandidateSource(config, snapshot, self.store.job(job)["cursor"])
        self.assertEqual(resumed.next_record().username, "abd")
        resumed.close()
        path.write_text("abc\nabe\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            CandidateSource(config, snapshot)

    def test_latest_unknown_keeps_previous_trusted_timestamp(self):
        config, first = self.job()
        self.store.ingest(first, InputRecord("abc", {"rank": "1"}), config, "simulated", "local-mock", "1")
        task = self.store.pending(first)[0]
        taken = Evidence(Classification.TAKEN, "explicitly_taken", "local-mock", "1", "simulated", True)
        self.store.claim(task["id"])
        self.store.finish(task["id"], taken)
        config = replace(config, smart_history=False)
        second = self.store.create_job(config, make_snapshot(config))
        self.store.ingest(second, InputRecord("abc", {}), config, "simulated", "local-mock", "1")
        task = self.store.pending(second)[0]
        self.store.claim(task["id"])
        self.store.finish(
            task["id"], Evidence(Classification.UNKNOWN, "network_error", "local-mock", "1", "simulated")
        )
        history = self.store.history_page(mode="simulated")[0]
        self.assertEqual(history["latest"], "Unknown")
        self.assertEqual(history["trusted"], "Taken")
        self.assertEqual(history["trusted_at"], taken.checked_at)
        third = self.store.create_job(config, make_snapshot(config))
        self.assertEqual(
            self.store.ingest(
                third,
                InputRecord("abc", {}),
                replace(config, smart_history=True),
                "simulated",
                "local-mock",
                "1",
            ),
            "skipped",
        )
        self.assertEqual(self.store.history_page(mode="real"), [])
        self.store.quarantine("local-mock", "1")
        self.assertEqual(self.store.results(mode="simulated", trusted=True), [])

    def test_backup_restore_export_separation(self):
        _, job = self.job(limit=20)
        asyncio.run(ExecutionEngine(self.store).run(job))
        backup = self.root / "backups" / "safe.db"
        self.store.backup(backup)
        Store.validate_backup(backup)
        restored = Store(backup)
        self.assertEqual(restored.job(job)["counters"]["completed"], 20)
        restored.close()
        outputs = self.store.export(self.root / "exports", mode="simulated", job_id=job)
        self.assertTrue(all(p.name.startswith("simulated_") for p in outputs))
        with outputs[-1].open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 20)
        self.assertEqual({r["mode"] for r in rows}, {"simulated"})
        self.assertEqual(self.store.results(job, mode="real"), [])
        _, later = self.job(limit=2)
        self.store.restore(backup, self.root / "pre-restore.db")
        with self.assertRaises(ValueError):
            self.store.job(later)
        self.assertEqual(self.store.job(job)["state"], JobState.COMPLETED)

    def test_pause_resume_and_interrupted_recovery(self):
        _, job = self.job(limit=80, workers=5, queue_size=20)
        engine = ExecutionEngine(self.store, MockVerifier(delay=0.02))

        async def run_pause():
            task = asyncio.create_task(engine.run(job))
            await asyncio.sleep(0.08)
            engine.request_pause()
            await task

        asyncio.run(run_pause())
        self.assertEqual(self.store.job(job)["state"], JobState.PAUSED)
        asyncio.run(engine.run(job))
        self.assertEqual(self.store.job(job)["counters"]["completed"], 80)
        self.assertLessEqual(engine.max_pending, 20)
        self.assertLessEqual(engine.max_inflight, 5)
        # Persisted Checking tasks become resumable after process interruption.
        config, interrupted = self.job(limit=1, smart_history=False)
        self.store.ingest(
            interrupted,
            InputRecord("abc", {"stage": "all", "rank": "1"}),
            config,
            "simulated",
            "local-mock",
            "1",
        )
        task = self.store.pending(interrupted)[0]
        self.store.claim(task["id"])
        self.store.set_state(interrupted, JobState.RUNNING)
        self.store.recover()
        self.assertEqual(self.store.job(interrupted)["state"], JobState.INTERRUPTED)
        self.assertEqual(self.store.pending(interrupted)[0]["state"], "Interrupted")
        asyncio.run(engine.run(interrupted))
        self.assertEqual(self.store.job(interrupted)["counters"]["completed"], 1)

    def test_retry_budget_and_circuit_and_real_guard(self):
        class Failing(MockVerifier):
            safe_concurrency = 2

            async def verify(self, username):
                raise ProviderFailure("429", retry_after=0.001)

        _, job = self.job(
            limit=20, workers=1000, auto_workers=False, circuit_threshold=3, max_retries=1, retry_budget=2
        )
        engine = ExecutionEngine(self.store, Failing())
        asyncio.run(engine.run(job))
        self.assertEqual(self.store.job(job)["state"], JobState.BLOCKED)
        self.assertLessEqual(self.store.job(job)["counters"]["retries"], 2)
        self.assertLessEqual(engine.max_inflight, 2)
        self.assertFalse(self.store.results(job, mode="simulated", classification="Available"))
        _, real = self.job(provider="real", limit=1)
        asyncio.run(ExecutionEngine(self.store).run(real))
        self.assertEqual(self.store.job(real)["state"], JobState.BLOCKED)
        self.assertEqual(self.store.job(real)["counters"]["dispatched"], 0)

    def test_ten_files_dedup_and_pending_bound(self):
        paths = []
        for index in range(10):
            path = self.root / f"{index}.txt"
            path.write_text("abc\naaa\nabc\n", encoding="utf-8")
            paths.append(str(path))
        config = SearchConfig(source="txt", files=tuple(paths), limit=None, queue_size=2)
        job = self.store.create_job(config, make_snapshot(config))
        engine = ExecutionEngine(self.store)
        asyncio.run(engine.run(job))
        self.assertEqual(self.store.job(job)["counters"]["duplicate"], 28)
        self.assertEqual(self.store.job(job)["counters"]["completed"], 2)
        self.assertLessEqual(engine.max_pending, 2)


if __name__ == "__main__":
    unittest.main()
