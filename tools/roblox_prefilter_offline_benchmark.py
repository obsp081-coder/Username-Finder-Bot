"""Bounded import/export/calculation measurement. No live transport is ever permitted."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from username_finder.roblox_prefilter import Prefilter, memory_peak  # noqa: E402
from username_finder.roblox_prefilter_estimates import calculate  # noqa: E402


def disk_bytes(root):
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            pass  # A SQLite WAL may be checkpointed between directory listing and stat.
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--output", type=Path, help="New directory outside the application checkout")
    args = parser.parse_args(argv)
    if not 100 <= args.rows <= 2_000_000:
        parser.error("Rows must be between 100 and 2000000")
    parent = args.output.resolve().parent if args.output else Path(tempfile.gettempdir())
    if args.output and (args.output.resolve().is_relative_to(ROOT) or args.output.exists()):
        parser.error("Output must be new and outside the application checkout")
    if not parent.is_dir():
        parser.error("Output parent must already exist")
    # Conservative headroom for source, SQLite, all export formats and filesystem journals.
    required = args.rows * 2000 + 512 * 1024 * 1024
    free = shutil.disk_usage(parent).free
    if free < required:
        parser.error(f"Insufficient safe disk headroom: require {required} bytes; free {free}")
    root = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix="ufb-prefilter-offline-"))
    root.mkdir(exist_ok=True)
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("All provider/network requests are forbidden in this benchmark")

    socket.socket.connect = socket.create_connection = forbidden
    report = {
        "status": "running",
        "rows": args.rows,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "directory": str(root),
        "baseline_comparison": False,
        "free_disk_before_bytes": free,
        "required_headroom_bytes": required,
        "initial_process_peak_bytes": memory_peak(),
    }
    last_sample, peak_disk = 0.0, 0

    def observe(force=False):
        nonlocal last_sample, peak_disk
        now = time.perf_counter()
        if force or now - last_sample >= 0.5:
            last_sample = now
            peak_disk = max(peak_disk, disk_bytes(root))

    started = time.perf_counter()
    try:
        source = root / "synthetic.txt"
        before = time.perf_counter()
        with source.open("x", encoding="ascii", newline="\n") as stream:
            for i in range(args.rows):
                stream.write(f"Bench{i:010}\n")
        report["source_write_seconds"] = time.perf_counter() - before
        tool = Prefilter(
            root / "run", root / "production-must-not-exist.db", transport=forbidden, updated=observe
        )
        before = time.perf_counter()
        summary = tool.prepare(source)
        report["import_seconds"] = time.perf_counter() - before
        assert summary["counts"]["valid"] == summary["counts"]["unique"] == args.rows
        assert summary["counts"]["attempts"] == 0 and summary["import_state"] == "Ready"
        assert not tool.database.exists()
        report["import_names_per_second"] = args.rows / report["import_seconds"]
        report["import_process_peak_bytes"] = memory_peak()
        report["source_bytes"] = source.stat().st_size
        report["work_disk_after_import_bytes"] = disk_bytes(tool.directory)
        print(json.dumps({"phase": "import_complete", **report}), flush=True)
        before = time.perf_counter()
        output = tool.export(root / "exports")
        report["export_seconds"] = time.perf_counter() - before
        state = json.loads((output / "export-state.json").read_bytes())
        assert state["complete"]
        for filename in ("candidates.csv", "candidates.json", "individual-candidates.txt"):
            assert state["files"][filename]["rows"] == args.rows
        before = time.perf_counter()
        for filename, entry in state["files"].items():
            digest = hashlib.sha256()
            with (output / filename).open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            assert digest.hexdigest() == entry["sha256"]
        report["independent_export_hash_verification_seconds"] = time.perf_counter() - before
        report["export_names_per_second"] = args.rows / report["export_seconds"]
        report["export_bytes"] = disk_bytes(output)
        report["export_files"] = len(state["files"])
        before = time.perf_counter()
        for _ in range(10_000):
            estimate = calculate(10, 20, 100, summary)
        report["calculation_iterations"] = 10_000
        report["calculation_total_seconds"] = time.perf_counter() - before
        report["calculation_mean_microseconds"] = report["calculation_total_seconds"] * 100
        assert estimate["remaining_requests"] == (args.rows + 19) // 20
        report["status"] = "complete"
    except KeyboardInterrupt:
        report["status"] = "cancelled"
    except Exception as exc:
        report.update(status="error", error=type(exc).__name__ + ": " + str(exc))
    finally:
        observe(True)
        report.update(
            total_seconds=time.perf_counter() - started,
            peak_process_bytes=memory_peak(),
            sampled_peak_temporary_disk_bytes=peak_disk,
            final_temporary_disk_bytes=disk_bytes(root),
            provider_request_attempts=calls,
        )
        (root / "benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf8")
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report["status"] == "complete" and calls == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
