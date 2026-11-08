"""Bounded LOCAL MOCK benchmark. Never contacts Snapchat."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import platform
import statistics
import sys
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from username_finder.domain import Filters, SearchConfig
from username_finder.engine import ExecutionEngine
from username_finder.providers import MockVerifier
from username_finder.sources import make_snapshot
from username_finder.storage import Store


def windows_metrics():
    from ctypes import wintypes

    class Memory(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak_rss",
                "rss",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
            )
        ]

    class IO(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")
        ]

    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    handle = kernel.GetCurrentProcess()
    memory = Memory()
    memory.cb = ctypes.sizeof(memory)
    io = IO()
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Memory),
        wintypes.DWORD,
    ]
    kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IO)]
    ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb)
    kernel.GetProcessIoCounters(handle, ctypes.byref(io))
    return {
        "rss_bytes": memory.rss,
        "peak_rss_process_bytes": memory.peak_rss,
        "read_bytes": io.read_bytes,
        "write_bytes": io.write_bytes,
    }


class MeasuredMock(MockVerifier):
    def __init__(self):
        super().__init__(delay=0.1)
        self.active = 0
        self.peak = 0

    async def verify(self, username):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            return await super().verify(username)
        finally:
            self.active -= 1


def run(workers, sample, directory):
    store = Store(directory / f"mock-{workers}.db")
    config = SearchConfig(
        filters=Filters(3, 3),
        limit=sample,
        workers=workers,
        auto_workers=False,
        queue_size=max(128, workers * 2),
        smart_history=False,
    )
    job = store.create_job(config, make_snapshot(config))
    provider = MeasuredMock()
    engine = ExecutionEngine(store, provider)
    before = windows_metrics()
    tracemalloc.start()
    started, cpu = time.perf_counter(), time.process_time()
    asyncio.run(engine.run(job))
    elapsed, cpu_seconds = time.perf_counter() - started, time.process_time() - cpu
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    after = windows_metrics()
    latencies = [
        r[0]
        for r in store.db.execute(
            "SELECT latency_ms FROM attempts WHERE task_id IN (SELECT id FROM tasks WHERE job_id=?)", (job,)
        )
    ]
    quantiles = statistics.quantiles(latencies, n=100) if len(latencies) >= 2 else [0] * 99
    final = store.job(job)
    result = {
        "configured_workers": workers,
        "sample": sample,
        "job_id": job,
        "state": final["state"],
        "elapsed_seconds": elapsed,
        "cpu_seconds": cpu_seconds,
        "python_peak_bytes": peak,
        "rss_before_bytes": before["rss_bytes"],
        "rss_after_bytes": after["rss_bytes"],
        "process_peak_rss_bytes": after["peak_rss_process_bytes"],
        "io_read_bytes": after["read_bytes"] - before["read_bytes"],
        "io_write_bytes": after["write_bytes"] - before["write_bytes"],
        "provider_peak_simultaneous": provider.peak,
        "engine_peak_tasks": engine.max_inflight,
        "max_pending": engine.max_pending,
        "pending_limit": config.queue_size,
        "completed": final["counters"]["completed"],
        "unknown": store.db.execute(
            "SELECT COUNT(*) FROM attempts WHERE classification='Unknown'"
        ).fetchone()[0],
        "p50_ms": statistics.median(latencies),
        "p95_ms": quantiles[94],
        "p99_ms": quantiles[98],
        "integrity": store.db.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_key_errors": len(store.db.execute("PRAGMA foreign_key_check").fetchall()),
    }
    store.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=1500)
    args = parser.parse_args()
    directory = (
        Path(__file__).resolve().parents[1]
        / "runtime"
        / ("benchmark-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    )
    directory.mkdir(parents=True)
    results = []
    for workers in (5, 20, 100, 500, 1000):
        result = run(workers, args.sample, directory)
        results.append(result)
        print(json.dumps(result), flush=True)
    report = {
        "date_utc": datetime.now(UTC).isoformat(),
        "os": platform.platform(),
        "python": sys.version,
        "provider": "local-mock v1 (100ms simulated latency)",
        "mode": "simulated",
        "snapchat_network_requests": 0,
        "limitations": "Short local samples, with tracemalloc overhead; "
        "not a soak test or Snapchat capacity measurement. "
        "Peak RSS is process cumulative. False Available and costs are not applicable to mock data.",
        "results": results,
    }
    output = directory / "measurements.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("REPORT=" + str(output))


if __name__ == "__main__":
    main()
