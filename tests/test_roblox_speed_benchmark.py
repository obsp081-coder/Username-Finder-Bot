"""Offline benchmark tests; every HTTP call is replaced before execution."""

import csv
import json
import threading
from collections import Counter

import pytest

from tools import roblox_speed_benchmark as benchmark
from username_finder import roblox_protocol as protocol


class Clock:
    def __init__(self):
        self.value = 1000.0
        self.waits = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.value += seconds


def response(code=0, status=200, *, body=None, headers=None, **kwargs):
    return protocol.Response(
        status,
        body
        if body is not None
        else json.dumps(
            {"code": code, "message": "Username is valid" if code == 0 else "Validation outcome"}
        ).encode(),
        {"content-type": "application/json", **(headers or {})},
        elapsed=0.125,
        **kwargs,
    )


@pytest.fixture
def prepared(tmp_path):
    folder = tmp_path / "benchmark"
    benchmark.prepare(folder)
    return folder


def test_manifest_is_frozen_valid_unique_and_complete_before_network(prepared):
    manifest = benchmark.read(prepared / "candidates.json")
    benchmark.validate(manifest)
    assert len({r["username"].casefold() for r in manifest}) == 400
    assert Counter(r["stage"] for r in manifest) == {1: 100, 2: 100, 3: 100, 4: 100}
    assert benchmark.read(prepared / "report.json")["status"] == "Prepared"
    assert not (prepared / "run.started.json").exists()
    manifest[-1]["username"] = manifest[0]["username"].upper()
    with pytest.raises(ValueError, match="Duplicate"):
        benchmark.validate(manifest)


def test_all_four_stages_are_sequential_with_exact_boundary_delays_and_durable_results(prepared):
    clock = Clock()
    calls = []
    in_request = False

    def fetch(url):
        nonlocal in_request
        assert not in_request
        in_request = True
        manifest = benchmark.read(prepared / "candidates.json")
        assert len(manifest) == 400
        if calls:
            assert (prepared / "records" / f"{len(calls):04d}.json").exists()
        calls.append(url)
        clock.value += 0.125
        in_request = False
        return response()

    report = benchmark.run(prepared, fetcher=fetch, clock=clock, sleep=clock.sleep)
    assert len(calls) == len(set(calls)) == 400
    assert report["status"] == "Completed" and report["fastest_completed_interval_seconds"] == 0
    rows = [benchmark.read(p) for p in sorted((prepared / "records").glob("*.json"))]
    for row in rows[1:]:
        assert row["measured_gap_seconds"] == row["requested_delay_seconds"]
    assert rows[100]["measured_gap_seconds"] == 1
    assert rows[200]["measured_gap_seconds"] == 0.5
    assert rows[300]["measured_gap_seconds"] == 0
    assert len(clock.waits) == 299
    assert all(stage["completed_requests"] == 100 for stage in report["stage_reports"])
    assert all(
        benchmark.read(prepared / f"stage-{i}-report.json")["successful_requests"] == 100 for i in range(1, 5)
    )
    assert all(stage["latency_p95_seconds"] == 0.125 for stage in report["stage_reports"])
    assert report["stage_reports"][0]["completed_requests_per_minute"] == pytest.approx(6000 / 210.5)
    assert len(list((prepared / "raw").glob("*.body"))) == 400
    with (prepared / "results.csv").open(encoding="utf-8-sig", newline="") as stream:
        exported = list(csv.DictReader(stream))
    assert len(exported) == 400
    assert all(row["message"] == "Username is valid" for row in exported)
    with pytest.raises(ValueError, match="duplicate"):
        benchmark.run(prepared, fetcher=lambda _: pytest.fail("Never recheck completed usernames"))


@pytest.mark.parametrize(
    "value,reason",
    [
        (response(status=401), "unexpected_http_status:401"),
        (response(status=403), "unexpected_http_status:403"),
        (response(status=429, headers={"retry-after": "60"}), "unexpected_http_status:429"),
        (response(status=500), "unexpected_http_status:500"),
        (response(status=302), "unexpected_http_status:302"),
        (response(headers={"retry-after": ""}), "retry_after_present"),
        (response(headers={"x-ratelimit-remaining": "0"}), "rate_limit_exhausted"),
        (response(headers={"x-ratelimit-remaining": "0;w=60, 4;w=120"}), "rate_limit_exhausted"),
        (response(headers={"x-ratelimit-remaining": "unexpected"}), "unexpected_rate_limit_header"),
        (response(challenge=True), "provider_challenge"),
        (response(error="timeout", complete=False), "transport_error:timeout"),
        (response(error="network_error", complete=False), "transport_error:network_error"),
        (response(body=b"{"), "unknown_classification:malformed_json"),
        (
            response(body=b'{"code":0,"message":"CAPTCHA required"}'),
            "unknown_classification:restriction_message",
        ),
        (response(code=99), "unknown_classification:undocumented_code"),
        (response(body=b'{"code":0,"message":"Username is valid","new_field":1}'), "unexpected_api_fields"),
    ],
)
def test_first_error_stops_entire_benchmark_without_retry(prepared, value, reason):
    calls = []
    clock = Clock()

    def fetch(url):
        calls.append(url)
        clock.value += 0.125
        return value

    report = benchmark.run(prepared, fetcher=fetch, clock=clock, sleep=clock.sleep)
    assert len(calls) == 1
    assert report["status"] == "Stopped" and reason in report["stop_reason"]
    assert report["fastest_completed_interval_seconds"] is None
    assert (prepared / "raw" / "0001.body").read_bytes() == value.body
    assert len(list((prepared / "records").glob("*.json"))) == 1
    assert report["stage_reports"][1]["recorded_attempts"] == 0
    with pytest.raises(ValueError):
        benchmark.run(prepared, fetcher=lambda _: pytest.fail("No resume after restrictions"))


@pytest.mark.parametrize("code", [1, 2, 12])
def test_valid_nonavailable_classifications_are_not_stop_conditions(prepared, code):
    clock = Clock()
    cancel = threading.Event()
    calls = []

    def fetch(url):
        calls.append(url)
        clock.value += 0.125
        if len(calls) == 2:
            cancel.set()
        return response(code)

    report = benchmark.run(prepared, fetcher=fetch, clock=clock, sleep=clock.sleep, cancel=cancel)
    assert len(calls) == 2 and report["status"] == "Cancelled"
    first = benchmark.read(prepared / "records" / "0001.json")
    assert first["stop_signals"] == [] and first["classification"] == protocol.CODES[code][0]


def test_stop_in_second_stage_reports_only_first_stage_as_stable(prepared):
    clock = Clock()
    calls = 0

    def fetch(_):
        nonlocal calls
        calls += 1
        clock.value += 0.125
        return response(status=429) if calls == 101 else response()

    report = benchmark.run(prepared, fetcher=fetch, clock=clock, sleep=clock.sleep)
    assert calls == 101 and report["fastest_completed_interval_seconds"] == 2
    assert report["stage_reports"][0]["completed_without_errors"]
    assert not report["stage_reports"][1]["completed_without_errors"]


def test_cancellation_during_wait_preserves_previous_record(prepared):
    clock = Clock()

    def sleep(_):
        raise KeyboardInterrupt

    report = benchmark.run(prepared, fetcher=lambda _: response(), clock=clock, sleep=sleep)
    assert report["status"] == "Cancelled" and report["metrics"]["recorded_attempts"] == 1
    assert len(list((prepared / "intents").glob("*.json"))) == 1
    assert (prepared / "REPORT.md").exists()


def test_cancellation_during_request_is_uncertain_not_a_completed_response(prepared):
    def fetch(_):
        raise KeyboardInterrupt

    report = benchmark.run(prepared, fetcher=fetch)
    assert report["status"] == "Cancelled"
    assert report["metrics"]["completed_requests"] == 0
    row = benchmark.read(prepared / "records" / "0001.json")
    assert row["delivery_uncertain"] and row["classification"] == "Unknown"


def test_unexpected_exception_is_saved_and_stops(prepared):
    def fetch(_):
        raise RuntimeError("Unexpected client failure")

    report = benchmark.run(prepared, fetcher=fetch)
    assert report["status"] == "Stopped"
    assert report["stop_reason"] == "transport_error:unexpected_execution_error:RuntimeError"
    assert benchmark.read(prepared / "records" / "0001.json")["error"].endswith("RuntimeError")


def test_changed_manifest_or_wrong_index_refuses_network(prepared):
    path = prepared / "candidates.json"
    manifest = benchmark.read(path)
    manifest[0]["index"] = 2
    benchmark.save(path, manifest)
    with pytest.raises(ValueError):
        benchmark.run(prepared, fetcher=lambda _: pytest.fail("Invalid manifest must not send"))
    assert not (prepared / "run.started.json").exists()


def test_report_recovery_never_sends_or_restarts(prepared):
    manifest = benchmark.read(prepared / "candidates.json")
    benchmark.save(prepared / "run.started.json", {"no_resume": True})
    benchmark.save(prepared / "intents" / "0001.json", manifest[0])
    report = benchmark.report_only(prepared)
    assert report["status"] == "Interrupted"
    assert report["runtime_is_lower_bound_after_hard_interruption"]
    assert report["recorded_requests_throughput_is_upper_bound_after_hard_interruption"]
    assert report["uncertain_intents_without_results"] == ["0001.json"]
    assert report["metrics"]["recorded_attempts"] == 0
    with pytest.raises(ValueError):
        benchmark.run(prepared, fetcher=lambda _: pytest.fail("Uncertain intent must not repeat"))


def test_persistence_overhead_is_subtracted_from_requested_wait(prepared, monkeypatch):
    clock = Clock()
    cancel = threading.Event()
    actual_csv = benchmark.write_csv
    calls = []

    def write_csv(folder, rows):
        actual_csv(folder, rows)
        clock.value += 0.75

    monkeypatch.setattr(benchmark, "write_csv", write_csv)

    def fetch(url):
        calls.append(url)
        clock.value += 0.125
        if len(calls) == 2:
            cancel.set()
        return response()

    benchmark.run(prepared, fetcher=fetch, clock=clock, sleep=clock.sleep, cancel=cancel)
    second = benchmark.read(prepared / "records" / "0002.json")
    assert clock.waits == [1.25]
    assert second["measured_gap_seconds"] == 2


def test_recovery_cannot_overwrite_results_of_an_active_run(prepared):
    before = (prepared / "report.json").read_bytes()
    with benchmark.exclusive(prepared), pytest.raises(OSError):
        benchmark.report_only(prepared)
    assert (prepared / "report.json").read_bytes() == before
