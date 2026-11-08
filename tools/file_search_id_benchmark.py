"""Compare literal and structured scans on temporary synthetic NDJSON only."""

from __future__ import annotations

import argparse
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

from file_search_benchmark import peak_working_set  # noqa: E402

from username_finder.file_search import CHUNK_BYTES, FileInfo, search_files  # noqa: E402
from username_finder.file_search_ids import search_account_ids  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=1500)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "runtime/file-search-id-development/structured-benchmark.json"
    )
    args = parser.parse_args()
    target = args.size_mb * 1_000_000
    if target < 1_000_000 or shutil.disk_usage(tempfile.gettempdir()).free < target + 512 * 1024**2:
        raise SystemExit("Insufficient temporary space or invalid size")
    identifier, absent = "9876543210123456789", "8765432109876543210"
    report = dict(
        python=platform.python_version(),
        platform=platform.platform(),
        logical_cpus=os.cpu_count(),
        chunk_bytes=CHUNK_BYTES,
        synthetic_only=True,
        cache_note="Freshly generated; cache uncontrolled; not a cold-disk benchmark.",
        memory_note="tracemalloc per case; working set cumulative, excludes Qt.",
        cases=[],
    )
    with tempfile.TemporaryDirectory(prefix="ufb-id-bench-") as folder:
        root = Path(folder).resolve()
        assert root.parent == Path(tempfile.gettempdir()).resolve()

        def generate(path, row, size, suffix=b""):
            block = row * max(1, CHUNK_BYTES // len(row))
            with path.open("wb") as stream:
                while stream.tell() + len(block) < size:
                    stream.write(block)
                while stream.tell() + len(row) < size:
                    stream.write(row)
                stream.write(suffix)

        row = (
            json.dumps(
                dict(u="synthetic_user", id="123456789", n="بيانات اختبار", padding="x" * 150),
                ensure_ascii=False,
            )
            + "\n"
        ).encode()
        large = root / "synthetic.jsonl"
        generate(large, row, target, ('{"id":"' + identifier + '"}\n').encode())
        before = FileInfo.capture(large)
        report.update(dataset_bytes=before.size, record_bytes=len(row))

        def run(name, scanner, path, query, expected, limit=100, cancel=None):
            count = 0

            def receive(rows):
                nonlocal count
                count += len(rows)

            tracemalloc.start()
            summary = scanner([path], query, limit, cancel=cancel, on_batch=receive)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            if expected is not None:
                assert count == expected and summary.status in ("complete", "limit"), summary
            p = summary.progress
            metrics = dict(
                case=name,
                status=summary.status,
                matches=count,
                bytes_processed=p.processed,
                total_bytes=p.total_bytes,
                elapsed_seconds=p.elapsed,
                MB_per_second=p.processed / 1e6 / max(1e-9, p.elapsed),
                python_peak_MiB=peak / 1024**2,
                process_peak_MiB_lifetime=peak_working_set(),
            )
            report["cases"].append(metrics)
            print(json.dumps(metrics), flush=True)
            return summary

        for scanner, label in [(search_files, "literal"), (search_account_ids, "structured")]:
            run(label + "_end", scanner, large, identifier, 1)
            run(label + "_absent", scanner, large, absent, 0)
        frequent = root / "frequent.jsonl"
        generate(frequent, ('{"id":"' + identifier + '","n":"synthetic"}\n').encode(), 1_000_000)
        run("structured_limit_100", search_account_ids, frequent, identifier, 100, 100)
        run("structured_limit_1000", search_account_ids, frequent, identifier, 1000, 1000)
        false_candidates = root / "unrelated.jsonl"
        false_row = (
            '{"id":"123456789","phone":"' + identifier + '","padding":"' + "x" * 100 + '"}\n'
        ).encode()
        generate(false_candidates, false_row, 10_000_000)
        run("structured_all_candidates_unrelated_10MB", search_account_ids, false_candidates, identifier, 0)
        cancel = Event()
        timer = Timer(0.05, cancel.set)
        timer.start()
        started = time.perf_counter()
        summary = run("structured_cancel", search_account_ids, large, absent, None, cancel=cancel)
        report["cancel_wall_seconds"] = time.perf_counter() - started
        timer.cancel()
        assert summary.status == "cancelled" and FileInfo.capture(large) == before
        report["sources_unchanged"] = True
    report["temporary_data_removed"] = not root.exists()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
