"""Virtual overnight workload; these measurements are local mock/SQLite performance only."""

import csv
import ctypes
import json
import os
import socket
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

from test_result_search import record
from test_roblox import response as roblox_response
from test_x import Clock
from test_x import response as x_response

from username_finder import x
from username_finder.overnight import OvernightStore
from username_finder.result_search import ResultFilter, ResultSearch
from username_finder.roblox_runner import RobloxRunner
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store
from username_finder.x_runner import XRunner
from username_finder.x_storage import XStore


def memory_bytes():
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("faults", wintypes.DWORD),
            *[
                (name, ctypes.c_size_t)
                for name in (
                    "peak_working_set",
                    "working_set",
                    "peak_paged",
                    "paged",
                    "peak_nonpaged",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                    "private",
                )
            ],
        ]

    kernel, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    result = Counters()
    result.cb = ctypes.sizeof(result)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(result), result.cb):
        raise ctypes.WinError()
    return {"working_set": result.working_set, "peak_working_set": result.peak_working_set}


def test_virtual_overnight_concurrent_with_large_history_and_readers(tmp_path):
    before_memory = memory_bytes()
    store = Store(tmp_path / "overnight-stress.db")
    repositories = {"Roblox": RobloxStore(store), "X": XStore(store)}
    size = {"Roblox": 6000, "X": 1200}
    clocks = {key: Clock() for key in size}
    nights = {key: OvernightStore(store, key) for key in size}
    calls = {key: Counter() for key in size}
    active, maximum = Counter(), Counter()
    mutex = threading.Lock()
    first = set()
    barrier = threading.Barrier(2)
    committed = threading.Event()
    ids = {}
    try:
        with store.transaction() as db:
            for index in range(15000):
                for platform in size:
                    record(db, platform, f"Saved{index:06}", "Taken")
        for platform, repo in repositories.items():
            if platform == "Roblox":
                repo.set_preferred_interval(5)
            else:
                repo.set_interval(30)
                repo.set_connection({"mode": "proxy", "proxy_url": "http://127.0.0.1:9"})
            names = [f"Night{index:06}" for index in range(size[platform])]
            ids[platform] = (
                repo.create_run(names)
                if platform == "Roblox"
                else repo.prepare(x.input_spec("\n".join(names)))
            )
            nights[platform].enable(ids[platform], True)

        def fetch(platform, value):
            name = parse_qs(urlsplit(value).query)["Username"][0] if platform == "Roblox" else value
            index = int(name[-6:])
            with mutex:
                calls[platform][name] += 1
                attempt = calls[platform][name]
                active[platform] += 1
                maximum[platform] = max(maximum[platform], active[platform])
                initial = platform not in first
                first.add(platform)
            try:
                if initial:
                    barrier.wait(timeout=10)
                clocks[platform].seconds += 0.05
                if index in {100, 200, 300, 400, 500} and attempt <= 2:
                    error = (TimeoutError, ConnectionResetError, socket.gaierror)[index % 3]
                    raise error()
                if platform == "Roblox":
                    return roblox_response((0, 1, 12, 3)[index % 4])
                # Availability is disputed: an overnight success fixture must use supported negatives.
                valid, reason = (
                    (False, "taken"),
                    (False, "is_banned_word"),
                    (False, "contains_banned_word"),
                )[index % 3]
                return x_response(valid, reason)
            finally:
                with mutex:
                    active[platform] -= 1

        runners = {
            "Roblox": RobloxRunner(
                repositories["Roblox"],
                fetcher=lambda value: fetch("Roblox", value),
                clock=clocks["Roblox"].now,
                wait=clocks["Roblox"].wait,
                updated=committed.set,
            ),
            "X": XRunner(
                repositories["X"],
                fetcher=lambda value, timeout: fetch("X", value),
                clock=clocks["X"].now,
                monotonic=lambda: clocks["X"].seconds,
                wait=clocks["X"].wait,
                updated=committed.set,
            ),
        }

        def read_pages():
            assert committed.wait(10)
            observed = []
            for _ in range(5):
                rows, total, _ = ResultSearch(store.path).page(
                    "All", ResultFilter(text="Night", repetition="digits")
                )
                assert len(rows) <= min(total, 100)
                observed.append(total)
            return observed

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(runners[key].run, identity) for key, identity in ids.items()]
            reader = pool.submit(read_pages)
            for future in futures:
                future.result(timeout=180)
            observed = reader.result(timeout=10)
        elapsed = time.perf_counter() - started
        assert maximum == {"Roblox": 1, "X": 1}
        for platform, repo in repositories.items():
            assert repo.progress(ids[platform])["state"] == "Completed"
            assert repo.results()[1] == size[platform] + 15000
            data = nights[platform].get(ids[platform])
            assert data["attempts"] == data["retries_performed"] == 10
            assert data["state"] == "Completed" and not data["auto_resume"]
            assert len(data["events"]) <= 100
            assert sum(calls[platform].values()) == size[platform] + 10
            prefix = "roblox" if platform == "Roblox" else "x"
            counts = store.db.execute(
                f"SELECT COUNT(*),COUNT(DISTINCT username_key) FROM {prefix}_checks WHERE run_id=?",
                (ids[platform],),
            ).fetchone()
            assert tuple(counts) == (size[platform], size[platform])
            assert not nights[platform].startup_candidates(repo)
        assert clocks["Roblox"].seconds >= 8 * 3600 and clocks["X"].seconds >= 10 * 3600
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
        path = tmp_path / "stress-filtered.csv"
        assert ResultSearch(store.path).export(path, "All", ResultFilter(text="Night")) == 7200
        with path.open(encoding="utf-8-sig", newline="") as stream:
            assert sum(1 for _ in csv.DictReader(stream)) == 7200
        after_memory = memory_bytes()
        print(
            "RELIABILITY_STRESS="
            + json.dumps(
                {
                    "saved_history_before": 30000,
                    "new_observations": 7200,
                    "mock_attempts": 7220,
                    "mock_check_seconds": round(elapsed, 4),
                    "local_observations_per_second": round(7200 / elapsed, 2),
                    "virtual_hours": {key: round(clock.seconds / 3600, 3) for key, clock in clocks.items()},
                    "reader_counts": observed,
                    "memory_before_bytes": before_memory,
                    "memory_after_bytes": after_memory,
                    "database_bytes": store.path.stat().st_size,
                }
            )
        )
    finally:
        store.close()
