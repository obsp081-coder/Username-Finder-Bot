"""Synthetic, temporary local benchmarks; retain metrics, never the large datasets."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import shutil
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from threading import Event, Timer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from username_finder.file_search import CHUNK_BYTES, FileInfo, search_files  # noqa: E402


def peak_working_set():
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD), ("values", ctypes.c_size_t * 8)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    data = Counters(cb=ctypes.sizeof(Counters))
    if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(data), ctypes.sizeof(data)):
        return data.values[0] / 1024**2
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=1500)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "runtime/file-search-development/benchmark.json"
    )
    args = parser.parse_args()
    target = args.size_mb * 1_000_000
    if target < 1_000_000 or shutil.disk_usage(tempfile.gettempdir()).free < target + 512 * 1024**2:
        raise SystemExit("Insufficient temporary disk space or invalid dataset size")
    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "logical_cpus": os.cpu_count(),
        "chunk_bytes": CHUNK_BYTES,
        "cache_note": "Freshly generated files; OS caching uncontrolled, not a cold-disk benchmark.",
        "memory_note": (
            "tracemalloc peak per search; Windows peak working set is process-lifetime cumulative."
        ),
        "cases": [],
    }
    with tempfile.TemporaryDirectory(prefix="ufb-search-bench-") as folder:
        root = Path(folder).resolve()
        assert root.parent == Path(tempfile.gettempdir()).resolve()
        large = root / "synthetic.jsonl"
        row = (
            json.dumps(
                {
                    "u": "synthetic_user",
                    "id": "123456789",
                    "n": "بيانات اختبار",
                    "e": "user@example.invalid",
                    "padding": "x" * 110,
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode()
        block = row * (CHUNK_BYTES // len(row))
        generation = time.perf_counter()
        with large.open("wb") as stream:
            stream.write(b'{"u":"BEGIN_MARKER"}\n')
            while stream.tell() + len(block) < target:
                stream.write(block)
            while stream.tell() + len(row) < target:
                stream.write(row)
            stream.write(b'{"u":"END_MARKER"}\n')
        before = FileInfo.capture(large)
        report.update(
            dataset_bytes=before.size,
            record_bytes=len(row),
            generation_seconds=time.perf_counter() - generation,
        )
        print(f"Generated {before.size:,} bytes", flush=True)

        def run(name, paths, needle, limit=100, cancel=None, expected=None):
            count = 0

            def receive(batch):
                nonlocal count
                count += len(batch)

            tracemalloc.start()
            result = search_files(paths, needle, limit, cancel=cancel, on_batch=receive)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            p = result.progress
            record = {
                "case": name,
                "status": result.status,
                "limit": limit,
                "matches": count,
                "dataset_bytes": p.total_bytes,
                "bytes_processed": p.processed,
                "bytes_read": p.bytes_read,
                "elapsed_seconds": p.elapsed,
                "MB_per_second": p.processed / 1_000_000 / max(p.elapsed, 1e-9),
                "python_peak_MiB": peak / 1024**2,
                "process_peak_working_set_MiB_lifetime": peak_working_set(),
            }
            if expected is not None:
                assert count == expected, (name, count, expected)
            assert result.status != "error", result.error_code
            report["cases"].append(record)
            print(json.dumps(record), flush=True)
            return record

        run("beginning", [large], "BEGIN_MARKER", 1, expected=1)
        run("end", [large], "END_MARKER", expected=1)
        run("no_match", [large], "needle_that_is_not_in_the_dataset", expected=0)
        run("frequent_10", [large], "synthetic_user", 10, expected=10)
        run("frequent_1000", [large], "synthetic_user", 1000, expected=1000)
        unicode = root / "unicode.txt"
        unicode.write_bytes(("عربي🙂 حرفي عربي🙂\r\n" * 10000).encode())
        run("unicode", [unicode], "عربي🙂", 1000, expected=1000)
        run("several_files", [unicode, large], "END_MARKER", expected=1)
        boundary = root / "boundary.txt"
        boundary.write_bytes(b"x" * (CHUNK_BYTES - 3) + b"boundary-match\n")
        run("buffer_boundary", [boundary], "boundary-match", expected=1)
        dense = root / "dense.txt"
        dense.write_bytes(b"a" * CHUNK_BYTES)
        run("dense_100000", [dense], "a", 100000, expected=100000)
        long_line = root / "long.txt"
        with long_line.open("wb") as stream:
            for _ in range(16):
                stream.write(b"x" * CHUNK_BYTES)
            stream.write(b"END_OF_LONG_LINE")
        run("long_record", [long_line], "END_OF_LONG_LINE", expected=1)
        cancel = Event()
        sent = []

        def stop():
            sent.append(time.perf_counter())
            cancel.set()

        timer = Timer(0.05, stop)
        timer.start()
        try:
            record = run("cancel_active", [large], "no_such_match", cancel=cancel)
            record["cancel_latency_seconds"] = time.perf_counter() - sent[0] if sent else None
            assert record["status"] == "cancelled"
        finally:
            timer.cancel()
            timer.join()
        report["source_unchanged"] = before == FileInfo.capture(large)
    report["temporary_data_removed"] = not root.exists()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
