"""Small offline checks for the reproducible performance harness."""

import json
import socket
import subprocess
import sys

import pytest

from tools import roblox_pipeline_benchmark as benchmark


def test_benchmark_rejects_provider_connections_before_dns_or_dispatch():
    with benchmark.loopback_network(), pytest.raises(ValueError, match="forbids external"):
        socket.create_connection(("auth.roblox.com", 443))


def test_distributions_use_nearest_rank_p95_and_report_empty_samples_honestly():
    result = benchmark.distribution([1, 3, 2, 4])
    assert result == dict(count=4, total_s=10, mean_s=2.5, median_s=2.5, p95_s=4)
    assert benchmark.distribution([]) == dict(count=0, total_s=0, mean_s=0, median_s=0, p95_s=0)


def test_small_pipeline_profiles_real_full_wal_commits_without_network(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return benchmark.protocol.Response(
            200, b'{"code":1,"message":"Taken"}', {"content-type": "application/json"}
        )

    result = benchmark.pipeline(tmp_path, count=4, queue=20, history=1, night=True, gui=False, fetcher=fetch)
    assert len(calls) == len(set(calls)) == result["count"] == 4
    assert result["classifications"] == {"Taken": 4}
    assert result["sqlite_commit"]["count"] == result["sql_statements"]["COMMIT"] == 12
    assert result["durability"] == "WAL/FULL" and result["checks_per_s"] > 0


@pytest.mark.parametrize("label,case", [("../escape", "sqlite_fresh"), ("valid", "not_a_case")])
def test_cli_rejects_invalid_case_and_unsafe_output_label_before_checking(tmp_path, label, case):
    child = subprocess.run(
        [
            sys.executable,
            str(benchmark.ROOT / "tools/roblox_pipeline_benchmark.py"),
            "--label",
            label,
            "--case",
            case,
            "--output",
            str(tmp_path),
            "--count",
            "4",
            "--queue",
            "4",
        ],
        capture_output=True,
        timeout=10,
    )
    assert child.returncode == 2
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize(
    "label,expected",
    [
        ("available", "Available"),
        ("taken", "Taken"),
        ("reserved", "Reserved"),
        ("moderation", "Invalid"),
        ("invalid", "Invalid"),
    ]
    + [
        (label, "Unknown")
        for label in (
            "undocumented",
            "malformed",
            "duplicate_key",
            "contradiction",
            "ambiguous",
            "schema",
            "incomplete",
            "challenge",
            "rate_limit",
            "forbidden",
            "unauthorized",
            "timeout",
            "network_error",
            "retry_after_200",
            "html",
        )
    ],
)
def test_semantic_fixtures_preserve_all_classifications_and_unsafe_200s(label, expected):
    result = benchmark.result_for("Bench00000000", benchmark.semantic_fixtures()[label])
    assert result["classification"] == expected
    assert result["preliminary"] and result["independently_verified_classification"] == "Unknown"


def test_semantic_guard_blocks_even_loopback_and_restores_socket_methods():
    connect = socket.socket.connect
    with benchmark.no_network():
        with pytest.raises(AssertionError, match="forbids all"):
            socket.create_connection(("auth.roblox.com", 443))
        with socket.socket() as stream, pytest.raises(AssertionError, match="forbids all"):
            stream.connect_ex(("127.0.0.1", 1))
    assert socket.socket.connect is connect


def test_semantic_known_mix_measures_real_evidence_and_durable_commits(tmp_path):
    with benchmark.no_network():
        report = benchmark.semantic_case(tmp_path / "known", count=20)
    assert report["classifications"] == {"Available": 2, "Taken": 12, "Reserved": 2, "Invalid": 4}
    assert report["mock_dispatches"] == report["received_http_responses"] == report["http_200"] == 20
    assert report["valid_preliminary_api_indications"] == 20 and report["unknown"] == 0
    assert report["provider_requests"] == report["independently_verified"] == 0
    assert report["components"]["classification"]["count"] == 20
    assert report["sqlite_commit"]["count"] == report["sql_statements"]["COMMIT"]
    assert report["evidence_utf8_bytes"] > report["response_body_bytes"] > 0
    assert report["final_state"] == "Completed" and report["durability"] == "WAL/FULL"
    assert report["default_interval_seconds"] == 5 and report["temporary_interval_seconds"] == 0


def test_semantic_http_200_is_not_counted_as_valid_classification(tmp_path):
    with benchmark.no_network():
        report = benchmark.semantic_case(
            tmp_path / "unknown", count=3, sequence=("malformed", "undocumented", "taken")
        )
    assert report["http_200"] == 2 and report["json_bodies_decoded"] == 1
    assert report["valid_preliminary_api_indications"] == 0 and report["unknown"] == 2
    assert report["queue_counts"] == {"Done": 2, "Pending": 1} and report["control_blocked"]


@pytest.mark.parametrize("label", ["rate_limit", "challenge", "forbidden", "unauthorized", "retry_after_200"])
def test_semantic_restrictions_stop_real_runner_without_extra_dispatch(tmp_path, label):
    with benchmark.no_network():
        report = benchmark.semantic_case(tmp_path / label, count=3, sequence=("taken", label, "taken"))
    assert report["mock_dispatches"] == 2 and report["valid_preliminary_api_indications"] == 1
    assert report["unknown"] == 1 and report["queue_counts"]["Pending"] == 1
    assert report["final_state"] == "Restricted"


@pytest.mark.parametrize("uncertain", [False, True])
def test_semantic_cancellation_and_recovery_do_not_double_count_or_replay(tmp_path, uncertain):
    with benchmark.no_network():
        report = benchmark.semantic_case(
            tmp_path / "resume", count=4, uncertain=uncertain, cancel_after=None if uncertain else 1
        )
    assert report["final_state"] == "Completed" and report["recovery_seconds"] is not None
    assert report["mock_dispatches"] == report["unique_mock_names"] == 4 - int(uncertain)
    assert report["unknown"] == int(uncertain)
    assert report["valid_preliminary_api_indications"] == 4 - int(uncertain)
    if not uncertain:
        assert report["initial_state"] == "Paused" and report["cancellation_cleanup_seconds"] is not None


def test_semantic_benchmark_never_bypasses_shared_historical_block(tmp_path, monkeypatch):
    from username_finder.roblox_activity import RobloxActivity

    run = benchmark.RobloxRunner.run

    def blocked(runner, identity):
        RobloxActivity(runner.repository.store.path).block.write_bytes(b"http_429")
        return run(runner, identity)

    monkeypatch.setattr(benchmark.RobloxRunner, "run", blocked)
    with benchmark.no_network(), pytest.raises(ValueError):
        benchmark.semantic_case(tmp_path / "blocked", count=4)
    assert RobloxActivity(tmp_path / "blocked" / "semantic.db").block.read_bytes() == b"http_429"


def test_semantic_capacity_scenarios_are_explicitly_hypothetical():
    model = benchmark.individual_capacity(0.001)
    assert model["required_valid_indications_per_second"] == pytest.approx(2_000_000 / 86400)
    rate = next(
        r
        for r in model["hypothetical_submission_rates"]
        if r["requests_per_second"] == 25 and r["unknown_fraction"] == 0.05
    )
    assert rate["unknown_names_after_two_million_submissions"] == 100000
    assert rate["hours_for_two_million_valid_indications_if_same_yield_persists"] > 24
    assert {r["hypothetical_concurrency"] for r in model["assumed_latency_and_concurrency"]} == {1, 4, 8, 16}


def test_semantic_cli_preserves_prior_reports_and_uses_no_network(tmp_path):
    args = [
        sys.executable,
        str(benchmark.ROOT / "tools/roblox_pipeline_benchmark.py"),
        "--semantic-offline",
        "--label",
        "semantic",
        "--case",
        "semantic_plain",
        "--count",
        "4",
        "--queue",
        "4",
        "--output",
        str(tmp_path),
    ]
    result = subprocess.run(args, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    path = tmp_path / "semantic.json"
    before = path.read_bytes()
    report = json.loads(before)
    assert report["cases"]["semantic_plain"]["provider_requests"] == 0
    assert (
        report["contract"]["Context"] == 0 and report["contract"]["Birthday"] == benchmark.protocol.BIRTHDAY
    )
    assert (
        subprocess.run(
            args, capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW
        ).returncode
        == 2
    )
    assert path.read_bytes() == before
