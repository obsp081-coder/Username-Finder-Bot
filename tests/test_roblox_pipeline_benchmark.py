"""Small offline checks for the reproducible performance harness."""

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
