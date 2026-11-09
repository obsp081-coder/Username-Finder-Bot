"""Offline paired measurement: existing count-only preparation, source-order selection, prioritization."""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.roblox_prefilter_offline_benchmark import disk_bytes  # noqa: E402
from username_finder.result_search import ResultFilter  # noqa: E402
from username_finder.roblox import prepare_inputs  # noqa: E402
from username_finder.roblox_local_selection import LocalSelection, Selection  # noqa: E402
from username_finder.roblox_prefilter import memory_peak  # noqa: E402


def encode(value, alphabet="abcdefghijklmnopqrstuvwxyz"):
    result = ""
    while value:
        value, digit = divmod(value, len(alphabet))
        result = alphabet[digit] + result
    return result or alphabet[0]


def synthetic(index):
    key = encode(index).rjust(5, "a")
    match index % 20:
        case 0:
            return "RepeatKey"
        case 1:
            return "invalid-" + key
        case 2:
            return "K" + key
        case 3:
            return ""
        case 4:
            return "L" * 21 + key
        case 5:
            return "p" + key
        case 6:
            return "mix" + str(index)
        case 7:
            return "a_" + key
        case 8:
            return "Repeat" + key
        case 9:
            return "s" + encode(index, "abcdefghijklmnopqrstuvwxyz0123456789")
        case 10:
            return f"{index:08}"
        case 11:
            return key + "zz"
        case 12:
            return "aaa" + key
        case 13:
            return "UP" + key
        case 14:
            return "12" + key
        case 15:
            return "aa" + str(index)
        case 16:
            return "LongName" + key
        case 17:
            return "letters" + key
        case 18:
            return "plain" + key
        case _:
            return "plain" + encode(index - 1).rjust(5, "a")


def forbidden(*args, **kwargs):
    raise AssertionError("No network access permitted in offline benchmark")


def worker(mode, source, directory):
    socket.socket.connect = socket.create_connection = forbidden
    directory.mkdir()
    started, cpu_started = time.perf_counter(), time.process_time()
    metrics = {"mode": mode, "source_bytes": source.stat().st_size, "network_attempts": 0}
    if mode == "existing":
        result = prepare_inputs(files=(source,))
        metrics.update(
            input_records=result.scanned,
            valid_unique=result.total,
            duplicates=result.duplicates,
            invalid=result.invalid_count,
            blank=result.blank,
        )
        metrics["import_seconds"] = time.perf_counter() - started
        metrics["import_cpu_seconds"] = time.process_time() - cpu_started
        metrics["export_seconds"] = None  # Existing count/preview API does not export this audit.
    else:
        phases = {}
        sampled_disk = 0
        last = 0

        def observe(event):
            nonlocal sampled_disk, last
            now = time.perf_counter()
            if event["phase"] in ("Selecting", "Ready"):
                phases[event["phase"]] = (now, time.process_time())
            if now - last > 0.5:
                sampled_disk = max(sampled_disk, disk_bytes(directory))
                last = now

        config = Selection(
            budget=100000,
            prioritize=(mode == "prioritized"),
            shorter_first=True,
            fewer_digits_first=True,
            filters=ResultFilter(length_mode="range", minimum=3, maximum=10),
            preferred=ResultFilter(composition="letters"),
        )
        tool = LocalSelection(directory / "run", notify=observe)
        tool.create(source, config)
        summary = tool.prepare()
        imported, import_cpu = phases["Selecting"]
        ready, ready_cpu = phases["Ready"]
        metrics.update(
            input_records=summary["cursor"],
            counts=summary["counts"],
            budget_examples=summary["budget_examples"],
            import_seconds=imported - started,
            import_cpu_seconds=import_cpu - cpu_started,
            selection_seconds=ready - imported,
            selection_cpu_seconds=ready_cpu - import_cpu,
            scratch_bytes_after_prepare=disk_bytes(tool.directory),
        )
        before, cpu_before = time.perf_counter(), time.process_time()
        exported = tool.export(directory / "export")
        metrics.update(
            export_seconds=time.perf_counter() - before,
            export_cpu_seconds=time.process_time() - cpu_before,
            output_bytes=disk_bytes(directory / "export"),
        )
        before = time.perf_counter()
        # Independent streaming audit: every logical source record, original bytes and ordinal preserved.
        with (
            source.open(encoding="utf-8", newline="") as original,
            (directory / "export" / "audit.csv").open(encoding="utf-8", newline="") as audit,
        ):
            for ordinal, (line, record) in enumerate(
                itertools.zip_longest(original, csv.DictReader(audit)), 1
            ):
                assert line is not None and record is not None
                for field in json.loads(record.get("csv_json_fields", "[]")):
                    record[field] = json.loads(record[field])
                assert record["original_value"] == line and int(record["source_record"]) == ordinal
        assert ordinal == summary["cursor"] == sum(summary["counts"].values())
        for filename, evidence in exported["files"].items():
            digest = hashlib.sha256()
            with (directory / "export" / filename).open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            assert digest.hexdigest() == evidence["sha256"]
        metrics["independent_audit_seconds"] = time.perf_counter() - before
        metrics["sampled_peak_run_disk_bytes"] = max(sampled_disk, disk_bytes(directory))
    metrics.update(
        total_seconds=time.perf_counter() - started,
        cpu_seconds=time.process_time() - cpu_started,
        peak_process_bytes=memory_peak(),
        final_run_disk_bytes=disk_bytes(directory),
    )
    metrics["import_records_per_second"] = metrics["input_records"] / metrics["import_seconds"]
    (directory / "measurement.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="NEW directory outside checkout")
    parser.add_argument("--sizes", nargs="+", type=int, default=[100000, 500000, 2000000])
    parser.add_argument(
        "--worker", choices=("existing", "source_order", "prioritized"), help=argparse.SUPPRESS
    )
    parser.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        worker(args.worker, args.source, args.output)
        return 0
    output = args.output.resolve()
    if output.is_relative_to(ROOT) or output.exists() or not output.parent.is_dir():
        parser.error("Choose a new output directory outside checkout with an existing parent")
    if any(not 100 <= n <= 2000000 for n in args.sizes):
        parser.error("Sizes must be between 100 and 2000000")
    if shutil.disk_usage(output.parent).free < max(args.sizes) * 1800 + 512 * 1024 * 1024:
        parser.error("Insufficient conservative disk headroom")
    output.mkdir()
    report = {
        "network_requests": 0,
        "measurements": [],
        "same_output_comparison": ["source_order", "prioritized"],
        "existing_limit": "Count/preview only; no equivalent export or budget/rank API",
    }
    for size in args.sizes:
        source = output / f"mixed-{size}.txt"
        with source.open("x", encoding="utf-8", newline="\n") as stream:
            for i in range(size):
                stream.write(synthetic(i) + "\n")
        for mode in ("existing", "source_order", "prioritized"):
            target = output / f"{size}-{mode}"
            env = os.environ.copy()
            env["QT_QPA_PLATFORM"] = "offscreen"
            env.pop("UFB_SCREENSHOTS", None)
            env.pop("UFB_NATIVE_POWER", None)
            flags = (
                subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0
            )
            subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--worker",
                    mode,
                    "--source",
                    str(source),
                    "--output",
                    str(target),
                ],
                check=True,
                env=env,
                creationflags=flags,
            )
            measurement = json.loads((target / "measurement.json").read_text(encoding="utf-8"))
            report["measurements"].append({"rows": size, **measurement})
            (output / "benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps({"rows": size, **measurement}), flush=True)
        existing, source_order, prioritized = report["measurements"][-3:]
        assert source_order["counts"] == prioritized["counts"]
        assert source_order["input_records"] == existing["input_records"] == size
        for measured in (source_order, prioritized):
            c = measured["counts"]
            assert (
                c["SelectedValid"] + c["LowerPriorityValid"] + c["UserExcluded"] == existing["valid_unique"]
            )
            assert c["Duplicate"] == existing["duplicates"]
            assert c["LocalInvalid"] == existing["invalid"] and c["Blank"] == existing["blank"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
