"""Bounded, fabricated individual-check workload; all network access is forbidden."""

import argparse
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import tracemalloc
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.roblox_pipeline_benchmark import (  # noqa: E402
    MeasuredConnection,
    distribution,
    memory,
    semantic_fixtures,
)
from username_finder import roblox_concurrent  # noqa: E402
from username_finder.roblox_runner import RobloxRunner  # noqa: E402
from username_finder.roblox_storage import RobloxStore  # noqa: E402
from username_finder.storage import Store  # noqa: E402


def measure(*, count=5000, workers=1, rate=1.0, latency=0.002, time_scale=1.0, cancel=False):
    """time_scale compresses ONLY concurrent pacing; latency is real mock sleep."""
    if not 2 <= count <= 5000 or not 0 <= latency <= 1 or not 1 <= time_scale <= 1000:
        raise ValueError("Use a bounded offline sample")
    roblox_concurrent.validate_policy(dict(version=1, workers=workers, rate=rate))
    if cancel and (workers < 2 or count <= workers):
        raise ValueError("Cancellation probe requires more inputs than concurrent workers")
    fixtures = semantic_fixtures()
    outcomes = ("available", "taken", "reserved", "invalid")
    original = sqlite3.connect
    forbidden = AssertionError("Offline profile forbids all network access")
    with (
        tempfile.TemporaryDirectory(prefix="ufb-individual-concurrency-") as directory,
        patch.object(socket.socket, "connect", side_effect=forbidden),
        patch.object(socket, "getaddrinfo", side_effect=forbidden),
        patch.object(
            sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=MeasuredConnection, **kw)
        ),
    ):
        store = Store(Path(directory) / "profile.db")
        try:
            repo = RobloxStore(store)
            repo.set_preferred_interval(0)
            identity = repo.create_run((f"Synthetic{i:08d}" for i in range(count)), skip=False)
            if workers > 1:
                from username_finder.roblox_concurrent import configure_execution

                configure_execution(repo, identity, workers=workers, rate=rate)
            store.db.commit_times = []
            active = threading.Barrier(workers + 1) if cancel else None
            release = threading.Event()
            attempts, attempt_lock = [0], threading.Lock()

            def fetch(url):
                if workers > 1:
                    runner.execution.transmit(
                        lambda: None
                    )  # Mock HTTP write, then independent response wait.
                with attempt_lock:
                    attempts[0] += 1
                name = parse_qs(urlsplit(url).query)["Username"][0]
                time.sleep(latency)
                if cancel:
                    active.wait(5)
                    if not release.wait(5):
                        raise RuntimeError("Cancellation probe did not release its mocked transport")
                return fixtures[outcomes[int(name[-8:]) % len(outcomes)]]

            runner = RobloxRunner(repo, fetcher=fetch)
            tracemalloc.start()
            started, cpu = time.perf_counter(), time.process_time()
            with ExitStack() as stack:
                if workers > 1 and time_scale != 1:
                    original_execution = roblox_concurrent.ConcurrentExecution
                    wall = datetime.now(UTC)

                    def execution(*a, **kw):
                        value = original_execution(
                            *a,
                            **kw,
                            monotonic=lambda: (time.perf_counter() - started) * time_scale,
                            wall=lambda: (
                                wall + timedelta(seconds=(time.perf_counter() - started) * time_scale)
                            ),
                        )
                        value.wait = lambda seconds: time.sleep(seconds / time_scale)
                        return value

                    stack.enter_context(patch.object(roblox_concurrent, "ConcurrentExecution", execution))
                cancellation_latency = None
                if cancel:
                    errors = []

                    def run():
                        try:
                            runner.run(identity)
                        except BaseException as exc:
                            errors.append(exc)

                    thread = threading.Thread(target=run)
                    thread.start()
                    try:
                        active.wait(5)
                        canceled_at = time.perf_counter()
                        runner.cancel.set()
                    finally:
                        release.set()
                        thread.join(10)
                    if thread.is_alive() or errors:
                        raise RuntimeError("Offline cancellation failed")
                    cancellation_latency = time.perf_counter() - canceled_at
                else:
                    runner.run(identity)
            wall, cpu = time.perf_counter() - started, time.process_time() - cpu
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            progress = repo.progress(identity)
            with store.lock:
                rows = store.db.execute(
                    "SELECT username,classification,evidence FROM roblox_checks ORDER BY username_key"
                ).fetchall()
                accepted = workers if cancel else count
                assert len(rows) == accepted
                for row in rows:
                    expected = ("Available", "Taken", "Reserved", "Invalid")[int(row[0][-8:]) % 4]
                    assert row[1] == expected and json.loads(row[2])["username"] == row[0]
                assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
            assert progress["state"] == ("Paused" if cancel else "Completed")
            assert progress["counts"] == (
                {"Done": workers, "Pending": count - workers} if cancel else {"Done": count}
            )
            return dict(
                measurement="offline_mock_only",
                count=count,
                workers=workers,
                configured_rate=rate if workers > 1 else None,
                mock_sleep_seconds=latency,
                pacing_time_scale=time_scale if workers > 1 else 1,
                wall_seconds=wall,
                cpu_seconds=cpu,
                accepted_per_second=accepted / wall,
                attempts_per_second=attempts[0] / wall,
                actual_attempts=attempts[0],
                traced_peak_bytes=peak,
                process_memory=memory(),
                sqlite_commits=distribution(store.db.commit_times),
                completeness=accepted / count,
                cancellation_latency_seconds=cancellation_latency,
                measured_at=datetime.now(UTC).isoformat(),
            )
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=5000)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--rate", type=float, default=1)
    parser.add_argument("--latency", type=float, default=0.002)
    parser.add_argument("--time-scale", type=float, default=1)
    parser.add_argument("--cancel", action="store_true")
    args = parser.parse_args()
    print(json.dumps(measure(**vars(args)), indent=2))


if __name__ == "__main__":
    main()
