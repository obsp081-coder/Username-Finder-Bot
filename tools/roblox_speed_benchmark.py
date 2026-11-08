"""Opt-in sequential benchmark; never imports the GUI, runner, or database."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import signal
import statistics
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from username_finder import roblox_protocol as protocol  # noqa: E402 - standalone terminal entrypoint
from username_finder.roblox import generate, now, result_for, syntax_error  # noqa: E402

STAGES = (2.0, 1.0, 0.5, 0.0)
PER_STAGE = 100
DEFAULT_OUTPUT = ROOT / "runtime" / "roblox-speed-benchmark"


def atomic(path, content):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def save(path, value):
    atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate(manifest):
    if not isinstance(manifest, list) or len(manifest) != PER_STAGE * len(STAGES):
        raise ValueError("Expected exactly 400 frozen candidates")
    seen = set()
    for index, entry in enumerate(manifest):
        name = entry.get("username")
        stage = index // PER_STAGE + 1
        if (
            not isinstance(name, str)
            or syntax_error(name)
            or name.casefold() in seen
            or entry.get("index") != index + 1
            or entry.get("stage") != stage
            or entry.get("requested_delay_seconds") != STAGES[stage - 1]
        ):
            raise ValueError("Duplicate, invalid, reordered or mismatched benchmark candidate")
        seen.add(name.casefold())


def prepare(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    for name in ("records", "raw", "intents"):
        (folder / name).mkdir()
    names = generate(12, PER_STAGE * len(STAGES)).names
    manifest = [
        {
            "index": i + 1,
            "username": name,
            "stage": i // PER_STAGE + 1,
            "requested_delay_seconds": STAGES[i // PER_STAGE],
        }
        for i, name in enumerate(names)
    ]
    validate(manifest)
    save(folder / "candidates.json", manifest)
    source_names = ("roblox.py", "roblox_protocol.py", "roblox_runner.py")
    save(
        folder / "config.json",
        {
            "prepared_at_utc": now(),
            "stages": list(STAGES),
            "requests_per_stage": PER_STAGE,
            "method": "GET",
            "endpoint": protocol.ENDPOINT,
            "parameters": {
                "Username": "verbatim per manifest",
                "Birthday": protocol.BIRTHDAY,
                "Context": protocol.CONTEXT,
            },
            "timeout_seconds": protocol.TIMEOUT_SECONDS,
            "intentional_concurrency": 1,
            "automatic_retries": 0,
            "automatic_resume": False,
            "manifest_sha256": hashlib.sha256((folder / "candidates.json").read_bytes()).hexdigest(),
            "source_sha256": {
                name: hashlib.sha256((ROOT / "username_finder" / name).read_bytes()).hexdigest()
                for name in source_names
            },
            "candidate_note": "Fresh local generation; no profile/history lookup; availability not assumed",
            "timing_note": "Gaps include evidence/report I/O; stages include their leading boundary wait.",
        },
    )
    state = {"status": "Prepared", "stop_reason": "not_started", "stages": {}}
    report(folder, [], state)
    return manifest


def stop_signals(response, result):
    signals = []
    if response.error:
        signals.append("transport_error:" + response.error)
    if not response.complete:
        signals.append("incomplete_response")
    if response.status != 200:
        signals.append(f"unexpected_http_status:{response.status}")
    if response.challenge:
        signals.append("provider_challenge")
    headers = {key.lower(): value for key, value in response.headers.items()}
    if "retry-after" in headers:
        signals.append("retry_after_present")
    for name in ("x-ratelimit-remaining", "ratelimit-remaining"):
        if name in headers:
            try:
                remaining = [int(part.split(";", 1)[0].strip()) for part in headers[name].split(",")]
                if any(value <= 0 for value in remaining):
                    signals.append("rate_limit_exhausted:" + name)
            except (ValueError, TypeError, AttributeError):
                signals.append("unexpected_rate_limit_header:" + name)
    if result["classification"] == "Unknown":
        signals.append("unknown_classification:" + result["reason"])
    else:
        # A changed schema is unsuitable evidence of a stable speed stage.
        if set(json.loads(response.body.decode("utf-8"))) != {"code", "message"}:
            signals.append("unexpected_api_fields")
    return signals


def write_csv(folder, rows):
    stream = io.StringIO(newline="")
    fields = [
        "index",
        "username",
        "stage",
        "requested_delay_seconds",
        "measured_gap_seconds",
        "request_started_at_utc",
        "response_returned_at_utc",
        "request_started_seconds",
        "response_returned_seconds",
        "request_latency_seconds",
        "latency_seconds",
        "http_status",
        "api_code",
        "message",
        "classification",
        "reason",
        "error",
        "stop_signals",
        "headers",
        "raw_complete",
        "delivery_uncertain",
        "raw_sha256",
        "raw_response_file",
        "response_data",
        "evidence_file",
    ]
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            dict(
                row,
                headers=json.dumps(row["headers"], ensure_ascii=False),
                stop_signals=json.dumps(row["stop_signals"]),
            )
        )
    atomic(folder / "results.csv", stream.getvalue().encode("utf-8-sig"))


def statistics_for(rows, runtime):
    latency = sorted(r["request_latency_seconds"] for r in rows if not r["delivery_uncertain"])
    gaps = [r["measured_gap_seconds"] for r in rows if r["measured_gap_seconds"] is not None]
    remaining = []
    observations = []
    for row in rows:
        headers = {k: v for k, v in row["headers"].items() if "ratelimit" in k or k == "retry-after"}
        if headers:
            observations.append({"index": row["index"], "headers": headers})
        value = headers.get("x-ratelimit-remaining", "")
        if value.isdigit():
            remaining.append(int(value))
    completed = sum(
        r["raw_complete"] and r["http_status"] is not None and not r["delivery_uncertain"] for r in rows
    )
    successful = sum(not r["stop_signals"] for r in rows)
    return {
        "recorded_attempts": len(rows),
        "completed_requests": completed,
        "successful_requests": successful,
        "runtime_seconds_including_pacing_and_persistence": runtime,
        "completed_requests_per_minute": completed * 60 / runtime if runtime else 0,
        "successful_requests_per_minute": successful * 60 / runtime if runtime else 0,
        "latency_mean_seconds": statistics.mean(latency) if latency else None,
        "latency_p95_seconds": latency[math.ceil(len(latency) * 0.95) - 1] if latency else None,
        "classification_counts": {
            key: sum(r["classification"] == key for r in rows) for key in protocol.CLASSES
        },
        "error_counts": dict(Counter(signal for r in rows for signal in r["stop_signals"])),
        "minimum_measured_gap_seconds": min(gaps) if gaps else None,
        "minimum_rate_limit_remaining": min(remaining) if remaining else None,
        "rate_limit_observations": observations,
    }


def report(folder, rows, state):
    stages = []
    stable = []
    for stage, delay in enumerate(STAGES, 1):
        selected = [r for r in rows if r["stage"] == stage]
        timing = state.get("stages", {}).get(str(stage), {})
        stats = statistics_for(selected, timing.get("runtime_seconds", 0))
        clean = stats["successful_requests"] == PER_STAGE and len(selected) == PER_STAGE
        if clean:
            stable.append(delay)
        stages.append(
            {
                "stage": stage,
                "requested_delay_seconds": delay,
                "completed_without_errors": clean,
                "status": timing.get("status", "NotStarted"),
                **stats,
            }
        )
    overall = statistics_for(rows, state.get("runtime_seconds", 0))
    value = {
        **state,
        "updated_at_utc": now(),
        "metrics": overall,
        "stage_reports": stages,
        "fastest_completed_interval_seconds": min(stable) if stable else None,
        "availability_independently_verified": False,
        "production_pacing_changed": False,
        "interpretation": "One sequential run only; no proof of future limits or production-safe pacing",
    }
    save(folder / "report.json", value)
    for stage in stages:
        if stage["recorded_attempts"]:
            save(
                folder / f"stage-{stage['stage']}-report.json",
                {
                    **stage,
                    "updated_at_utc": value["updated_at_utc"],
                    "stop_reason": None if stage["status"] == "Completed" else state["stop_reason"],
                },
            )
    lines = [
        "# قياس سرعة Roblox المتسلسل",
        "",
        f"الحالة: {state['status']}؛ سبب التوقف: {state['stop_reason']}.",
        f"الطلبات المسجلة: {len(rows)}؛ الاستجابات المكتملة: {overall['completed_requests']}.",
        f"أسرع فاصل أكمل 100 طلب دون أخطاء: {value['fastest_completed_interval_seconds']} ثانية.",
        "",
        "| المرحلة | الفاصل المطلوب (ث) | مكتمل | دون أخطاء | طلب/دقيقة | متوسط الطلب (ث) | p95 (ث) |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for stage in stages:
        lines.append(
            f"| {stage['stage']} | {stage['requested_delay_seconds']} | {stage['completed_requests']} | "
            f"{stage['successful_requests']} | {stage['completed_requests_per_minute']:.3f} | "
            f"{stage['latency_mean_seconds']} | {stage['latency_p95_seconds']} |"
        )
    lines += [
        "",
        "السرعة تشمل الانتظار وحفظ الأدلة؛ زمن الطلب مقاس حول استدعاء النقل وحده. "
        "الفاصل الفعلي يشمل حفظ الأدلة والتقارير، وقد يتجاوز المطلوب حتى عند صفر ثانية.",
        "تبدأ مدة كل مرحلة قبل انتظار طلبها الأول. تفاصيل التصنيفات ورؤوس الحدود في report.json.",
        "الأسماء مولدة حديثًا محليًا وصحيحة نحويًا، وليست أسماء مؤكدة الإتاحة. "
        "التصنيفات أولية وغير مؤكدة مقابل التسجيل.",
        "لا تغيّر هذه الأداة إعداد الواجهة البالغ 5 ثوانٍ. نجاح مرحلة واحدة لا يثبت استقرارًا طويل الأجل.",
        "لا يوجد استئناف أو إعادة محاولات تلقائية. يمنع run.started.json إعادة تشغيل المجلد نفسه.",
    ]
    atomic(folder / "REPORT.md", ("\n".join(lines) + "\n").encode("utf-8"))
    return value


@contextmanager
def exclusive(folder):
    with (Path(folder) / "run.lock").open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def run(folder, **kwargs):
    with exclusive(folder):
        return _run(folder, **kwargs)


def _run(folder, *, fetcher=None, clock=time.perf_counter, sleep=None, cancel=None):
    folder = Path(folder)
    config = read(folder / "config.json")
    manifest = read(folder / "candidates.json")
    validate(manifest)
    if hashlib.sha256((folder / "candidates.json").read_bytes()).hexdigest() != config["manifest_sha256"]:
        raise ValueError("Frozen manifest changed; no requests sent")
    for name, digest in config["source_sha256"].items():
        if hashlib.sha256((ROOT / "username_finder" / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Roblox source changed after preparation; review before benchmarking")
    # No implicit resume, even after a crash. The exclusive marker also acts as a run lock.
    if any(next((folder / name).glob("*.json"), None) for name in ("records", "intents")):
        raise ValueError("Existing attempt evidence; refusing duplicate checks")
    with (folder / "run.started.json").open("x", encoding="utf-8") as stream:
        json.dump({"started_at_utc": now(), "pid": os.getpid(), "no_resume": True}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    fetcher = fetcher or protocol.fetch
    cancel = cancel or threading.Event()
    started = clock()
    previous_end = None
    rows = []
    state = {"status": "Running", "stop_reason": "running", "stages": {}, "runtime_seconds": 0}
    stage_start = None
    active_stage = None
    try:
        for candidate in manifest:
            if cancel.is_set():
                raise KeyboardInterrupt
            stage = candidate["stage"]
            if stage != active_stage:
                active_stage, stage_start = stage, clock()
                state["stages"][str(stage)] = {"status": "Running", "runtime_seconds": 0}
            delay = candidate["requested_delay_seconds"]
            if previous_end is not None:
                remaining = max(0, delay - (clock() - previous_end))
                if remaining:
                    (sleep or cancel.wait)(remaining)
            if cancel.is_set():
                raise KeyboardInterrupt
            index = candidate["index"]
            save(folder / "intents" / f"{index:04d}.json", {**candidate, "intent_at_utc": now()})
            if cancel.is_set():
                save(folder / "intents" / f"{index:04d}.json", {**candidate, "not_sent": True})
                raise KeyboardInterrupt
            request_at = now()
            request_start = clock()
            interrupted = False
            try:
                response = fetcher(protocol.request_url(candidate["username"]))
            except KeyboardInterrupt:
                response = protocol.Response(error="cancelled_during_request", complete=False)
                interrupted = True
            except Exception as exc:
                response = protocol.Response(
                    error="unexpected_execution_error:" + type(exc).__name__, complete=False
                )
            response_end = clock()
            response_at = now()
            row = {
                **result_for(candidate["username"], response),
                **candidate,
                "request_started_at_utc": request_at,
                "response_returned_at_utc": response_at,
                "request_started_seconds": request_start - started,
                "response_returned_seconds": response_end - started,
                "request_latency_seconds": response_end - request_start,
                "stage_started_seconds": stage_start - started,
                "measured_gap_seconds": request_start - previous_end if previous_end is not None else None,
                "delivery_uncertain": interrupted,
                "raw_response_file": f"raw/{index:04d}.body",
                "evidence_file": f"records/{index:04d}.json",
            }
            row["stop_signals"] = stop_signals(response, row)
            if interrupted:
                row["stop_signals"].insert(0, "cancelled")
            elif cancel.is_set():
                row["stop_signals"].insert(0, "cancelled_after_response")
            atomic(folder / row["raw_response_file"], response.body)
            save(folder / row["evidence_file"], row)
            rows.append(row)
            previous_end = response_end
            write_csv(folder, rows)
            state["runtime_seconds"] = clock() - started
            timing = state["stages"][str(stage)]
            timing["runtime_seconds"] = clock() - stage_start
            if row["stop_signals"]:
                state.update(
                    status="Cancelled" if interrupted or cancel.is_set() else "Stopped",
                    stop_reason=row["stop_signals"][0],
                )
                timing["status"] = state["status"]
                save(folder / "checkpoint.json", state)
                break
            save(folder / "checkpoint.json", state)
            if index % PER_STAGE == 0:
                timing["status"] = "Completed"
                report(folder, rows, state)
                print(f"Stage {stage}: 100/100 completed without errors; delay={delay}s", flush=True)
        else:
            state.update(status="Completed", stop_reason="all_four_stages_completed")
    except KeyboardInterrupt:
        state.update(status="Cancelled", stop_reason="ctrl_c_before_or_between_requests")
    except Exception as exc:
        state.update(status="Stopped", stop_reason="unexpected_execution_error:" + type(exc).__name__)
        state["exception_message"] = str(exc)
    finally:
        state["runtime_seconds"] = clock() - started
        if active_stage is not None:
            timing = state["stages"][str(active_stage)]
            timing["runtime_seconds"] = clock() - stage_start
            if timing["status"] == "Running":
                timing["status"] = state["status"]
        save(folder / "checkpoint.json", state)
        write_csv(folder, rows)
        value = report(folder, rows, state)
        print(f"{state['status']}: {state['stop_reason']}; recorded={len(rows)}; output={folder}", flush=True)
    return value


def report_only(folder):
    with exclusive(folder):
        return _report_only(Path(folder))


def _report_only(folder):
    rows = [read(path) for path in sorted((folder / "records").glob("*.json"))]
    previous = (
        read(folder / "checkpoint.json")
        if (folder / "checkpoint.json").exists()
        else read(folder / "report.json")
    )
    if (folder / "run.started.json").exists() and previous["status"] in {"Prepared", "Running"}:
        previous.update(status="Interrupted", stop_reason="process_ended_without_final_report")
    previous["uncertain_intents_without_results"] = [
        path.name
        for path in (folder / "intents").glob("*.json")
        if not (folder / "records" / path.name).exists() and not read(path).get("not_sent")
    ]
    # After a hard kill, report only measured lower bounds, never invent missing wall time.
    for row in rows:
        timing = previous.setdefault("stages", {}).setdefault(
            str(row["stage"]), {"status": "Interrupted", "runtime_seconds": 0}
        )
        timing["runtime_seconds"] = max(
            timing["runtime_seconds"], row["response_returned_seconds"] - row["stage_started_seconds"]
        )
        previous["runtime_seconds"] = max(
            previous.get("runtime_seconds", 0), row["response_returned_seconds"]
        )
    if previous["status"] == "Interrupted":
        previous["runtime_is_lower_bound_after_hard_interruption"] = True
        previous["recorded_requests_throughput_is_upper_bound_after_hard_interruption"] = True
        for stage, timing in previous.get("stages", {}).items():
            selected = [r for r in rows if r["stage"] == int(stage)]
            timing["status"] = (
                "Completed"
                if len(selected) == PER_STAGE and all(not r["stop_signals"] for r in selected)
                else "Interrupted"
            )
    write_csv(folder, rows)
    return report(folder, rows, previous)


def main():
    parser = argparse.ArgumentParser(description="Independent sequential Roblox speed benchmark; no user DB")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare-only", action="store_true")
    actions.add_argument("--run", action="store_true")
    actions.add_argument("--report-only", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.prepare_only:
        prepare(args.output)
        print(f"Prepared 400 candidates; no live requests. Output: {args.output.resolve()}")
    elif args.report_only:
        report_only(args.output)
    else:
        if not args.output.exists():
            prepare(args.output)
        cancel = threading.Event()
        old_handler = signal.signal(signal.SIGINT, lambda *_: cancel.set())
        try:
            value = run(args.output, cancel=cancel)
        finally:
            signal.signal(signal.SIGINT, old_handler)
        return 0 if value["status"] == "Completed" else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
