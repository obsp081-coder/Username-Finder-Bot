"""Pure offline capacity arithmetic, not a test of provider throughput."""

import json
import subprocess
import sys

import pytest

from username_finder.roblox_prefilter_estimates import capacity_plan


@pytest.mark.parametrize(
    "batch,requests", [(20, 100000), (50, 40000), (100, 20000), (200, 10001), (500, 4001), (1000, 2001)]
)
def test_two_million_budget_includes_phase_boundary_overhead(batch, requests):
    plan = capacity_plan(2_000_000, batch)
    assert plan["planned_requests"] == requests
    assert plan["unpartitioned_requests"] == (2_000_000 + batch - 1) // batch
    assert plan["required_names_per_second"] == pytest.approx(23.148148148)
    assert plan["required_requests_per_second"] == pytest.approx(requests / 86400)
    assert plan["average_cycle_budget_seconds"] == pytest.approx(86400 / requests)
    assert plan["within_operational_batch_guard"] == (batch <= 20)
    assert not plan["provider_capacity_verified"] and not plan["dispatch_authorized"]


def test_cycle_latency_local_cost_final_gap_and_maximum_delay():
    plan = capacity_plan(2_000_000, 20, 0.5, latency=0.25, local_seconds_per_request=0.01)
    assert plan["modeled_elapsed_seconds"] == pytest.approx(75999.5)
    assert plan["maximum_post_response_delay_seconds"] == pytest.approx((86400 - 26000) / 99999)
    assert plan["time_budget_met_if_uninterrupted"]
    assert not capacity_plan(2_000_000)["time_budget_met_if_uninterrupted"]
    # The single request has no post-response wait, regardless of configured gap.
    assert capacity_plan(1, 20, 10, latency=0.5)["modeled_elapsed_seconds"] == 0.5
    assert capacity_plan(1, 20, 10)["maximum_post_response_delay_seconds"] is None


def test_partial_safe_outcomes_remain_unresolved_without_scheduling_retries():
    plan = capacity_plan(2_000_000, 20, 0.5, latency=0.25, safe_outcome_fraction=0.99)
    assert plan["modeled_safe_names"] == 1_980_000
    assert plan["modeled_unresolved_names"] == 20_000
    assert plan["modeled_safe_names_per_second"] < plan["modeled_names_per_second"]
    assert not plan["all_names_have_safe_outcomes_in_model"]
    assert plan["automatic_retries"] == 0 and not plan["dispatch_authorized"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"names": 0},
        {"names": True},
        {"names": 1.5},
        {"names": 2**63},
        {"batch_size": 1001},
        {"delay": 0},
        {"latency": -1},
        {"latency": float("nan")},
        {"local_seconds_per_request": -1},
        {"hours": 0},
        {"hours": float("inf")},
        {"hours": 1e-320},
        {"names": 1, "latency": 1e-320},
        {"safe_outcome_fraction": 1.01},
        {"safe_outcome_fraction": True},
    ],
)
def test_invalid_assumptions_fail_closed(kwargs):
    with pytest.raises(ValueError):
        capacity_plan(**dict({"names": 2_000_000}, **kwargs))


def test_planner_never_reads_sql_files_or_network(monkeypatch):
    import pathlib
    import socket
    import sqlite3

    def forbidden(*args, **kwargs):
        pytest.fail("Capacity arithmetic must not access external state")

    with monkeypatch.context() as guard:
        guard.setattr(pathlib.Path, "open", forbidden)
        guard.setattr(sqlite3, "connect", forbidden)
        guard.setattr(socket, "create_connection", forbidden)
        assert capacity_plan(10**15)["planned_requests"] == 50_000_000_000_000


def test_cli_exports_assumptions_and_bounded_payload_sizes_only():
    script = """
import runpy,socket
def forbidden(*args,**kwargs):raise AssertionError('No live connections in capacity CLI')
socket.socket.connect=socket.create_connection=forbidden
runpy.run_path('tools/roblox_bulk_capacity.py',run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["kind"] == "offline_assumptions_not_live_measurements"
    assert len(output["scenarios"]) == 18
    assert {r["settings"]["batch_size"] for r in output["scenarios"]} == {20, 50, 100, 200, 500, 1000}
    assert all(not r["dispatch_authorized"] for r in output["scenarios"])
    assert max(r["maximum_ascii_request_json_bytes"] for r in output["scenarios"]) < 25000
