import csv
import json
import socket
import ssl
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from email.message import Message

import pytest

from username_finder import roblox_bulk_protocol as bulk
from username_finder.roblox_activity import RobloxActivity
from username_finder.roblox_bulk import BulkExperiment
from username_finder.roblox_protocol import Response
from username_finder.roblox_runner import RobloxRunner
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Live networking is forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def response(rows=None, **changes):
    values = dict(
        status=200,
        body=json.dumps({"data": rows or []}).encode(),
        headers={"content-type": "application/json"},
        elapsed=0.25,
    )
    values.update(changes)
    return Response(**values)


def record(requested, current=None, uid=1):
    return {"requestedUsername": requested, "name": current or requested, "id": uid}


class Clock:
    def __init__(self):
        self.at = 0.0
        self.waits = []

    def now(self):
        return self.at

    def wall(self):
        return datetime(2026, 11, 8, tzinfo=UTC) + timedelta(seconds=self.at)

    def wait(self, seconds):
        self.waits.append(seconds)
        self.at += seconds
        return False


@pytest.fixture
def experiment(tmp_path):
    clock, calls = Clock(), []

    def transport(request):
        calls.append((clock.at, request))
        clock.at += 0.25
        if request["kind"] == "individual":
            return response(body=b'{"code":1,"message":"Already used"}')
        return response([record(n, uid=int(n, 36) + 1) for n in request["usernames"]])

    exp = BulkExperiment(
        tmp_path / "evidence",
        tmp_path / "existing.db",
        transport=transport,
        clock=clock.now,
        wall=clock.wall,
        wait=clock.wait,
    )
    exp.test_clock, exp.test_calls = clock, calls
    exp.prepare()
    return exp


def run(exp, phase="stage1"):
    return exp.run(phase, exp.review(phase))


def test_frozen_sample_partition_comparison_exclusions_and_no_database(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: pytest.fail("No production DB access"))
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"candidates": [{"requested_username": "aaaa"}]}))
    exp = BulkExperiment(tmp_path / "evidence", tmp_path / "untouched.db")
    manifest = exp.prepare([path])
    original = exp.manifest_path.read_bytes()
    assert exp.prepare([path]) == manifest
    assert original == exp.manifest_path.read_bytes()
    assert len(manifest["candidates"]) == len(set(manifest["candidates"])) == 1000
    assert "aaaa" not in manifest["candidates"]
    assert len(manifest["comparison_random"]) == 20
    assert set(manifest["comparison_random"]) <= set(manifest["candidates"][:200])
    plans = exp.plan("stage1") + exp.plan("stage2")
    assert len(plans) == 50 and all(len(p["usernames"]) == 20 for p in plans)
    assert [n for p in plans for n in p["usernames"]] == manifest["candidates"]
    assert not exp.database.exists() and not exp.events()
    assert manifest["Context"] == 0 and manifest["Birthday"] == "2000-01-01T00:00:00Z"


def test_stage_boundaries_separate_authorization_restart_and_no_replay(experiment):
    exp = experiment
    with pytest.raises(ValueError):
        exp.review("stage2")
    with pytest.raises(ValueError):
        exp.run("stage1", "")
    report = run(exp)
    assert len(exp.test_calls) == 10
    assert report["phases"]["stage1"]["state"] == "Completed"
    assert "stage2" not in report["phases"]
    assert report["completed_bulk_batches"] == 10
    assert report["counts"] == {"CurrentAccountMatchByAPI": 200}
    assert report["comparison_coverage"] == 0
    assert len(exp.test_clock.waits) == 9 and min(exp.test_clock.waits) == 10
    for (at, _), (next_at, _) in zip(exp.test_calls, exp.test_calls[1:], strict=False):
        assert next_at - at == 10.25
    restored = BulkExperiment(
        exp.directory, exp.database, transport=exp.transport, clock=exp.clock, wall=exp.wall, wait=exp.wait
    )
    with pytest.raises(ValueError):
        restored.review("stage1")
    report = run(restored, "stage2")
    assert len(exp.test_calls) == 50
    checked = [n for _, request in exp.test_calls for n in request["usernames"]]
    assert len(checked) == len(set(checked)) == 1000
    assert report["counts"] == {"CurrentAccountMatchByAPI": 1000}
    assert report["complete_http_responses"] == 50
    assert report["uncertain_or_missing_responses"] == 0
    assert report["mean_latency_seconds"] == report["p95_latency_seconds"] == 0.25
    assert report["total_elapsed_runtime_seconds"] == 502.5
    assert report["known_dispatch_elapsed_seconds"] == 12.5
    assert report["deliberate_pacing_seconds"] == 490
    assert report["phases"]["stage1"]["elapsed_seconds"] == 92.5
    assert len(list(exp.directory.glob("report-*.json"))) == 2


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([record("aBc1", "ABC1")], "CurrentAccountMatchByAPI"),
        ([record("abc1", "roblox_user_5533881471")], "HistoricalOrDifferentCurrentName"),
        ([], "NoMatchingBulkRecord"),
    ],
)
def test_separate_account_semantics(rows, expected):
    mapping, stop = bulk.interpret_bulk(["abc1"], response(rows))
    assert not stop and mapping["abc1"]["state"] == expected
    assert not any(v in ("Available", "Taken") for v in mapping["abc1"].values())


@pytest.mark.parametrize(
    "rows",
    [
        [record("abcd", uid=True)],
        [record("other")],
        [record("abcd"), record("ABCD")],
        [record("abcd", "a-b")],
        [record("abcd", "same", 1), record("efgh", "same", 2)],
        [record("abcd", "one", 1), record("efgh", "two", 1)],
    ],
)
def test_unsafe_records_keep_no_positive(rows):
    mapping, stop = bulk.interpret_bulk(["abcd", "efgh"], response(rows))
    assert stop and all(v["state"] == "NoMatchingBulkRecord" for v in mapping.values())


@pytest.mark.parametrize(
    "changes",
    [
        {"status": 401},
        {"status": 403},
        {"status": 429},
        {"status": 500},
        {"challenge": True},
        {"error": "timeout", "complete": False},
        {"headers": {"content-type": "application/json", "retry-after": "120"}},
        {"headers": {"content-type": "application/json", "x-ratelimit-remaining": "0"}},
        {"body": b"not json"},
        {"body": b'{"data":[],"unexpected":true}'},
        {"body": b'{"data":[],"data":[]}'},
        {"body": b'{"token":"must not persist"}'},
    ],
)
def test_immediate_stop_restrictions_preserved_no_replay(experiment, changes):
    calls = []
    experiment.transport = lambda request: calls.append(request) or response(**changes)
    report = run(experiment)
    assert len(calls) == 1
    assert report["phases"]["stage1"]["state"] == "Stopped"
    assert report["dispatch_intents"] == 1
    assert set(report["counts"]) <= {"Unknown", "UnsafeOrContradictoryResponse"}
    if b"token" in changes.get("body", b""):
        assert b"must not persist" not in experiment.journal_path.read_bytes()
    for phase in ("stage1", "stage2", "comparison"):
        with pytest.raises(ValueError):
            experiment.review(phase)
    with pytest.raises(ValueError):
        with RobloxActivity(experiment.database):
            pytest.fail("Restriction must persist beyond process lifetime")


def test_intent_before_dispatch_raw_before_parse_and_interruption(experiment, monkeypatch):
    def interrupted(request):
        assert experiment.events()[-1]["event"] == "intent"
        raise OSError("uncertain request")

    experiment.transport = interrupted
    report = run(experiment)
    assert report["uncertain_or_missing_responses"] == 1
    assert report["complete_http_responses"] == 0
    restored = BulkExperiment(experiment.directory, experiment.database)
    with pytest.raises(ValueError):
        restored.review("stage1")
    assert experiment.events()[1]["request"] == experiment.plan("stage1")[0]


def test_comparison_ig49_semantic_conflict_and_separate_evidence(experiment):
    run(experiment)
    before = experiment.report()["account_observations"]
    experiment.transport = lambda request: response(body=b'{"code":2,"message":"Username not appropriate"}')
    report = run(experiment, "comparison")
    assert report["comparison_coverage"] == report["observed_disagreements"] == 1
    assert report["account_observations"] == before
    name = report["manifest"]["comparison_random"][0]
    assert report["individual_observations"][name]["classification"] == "Invalid"
    assert report["individual_observations"][name]["api_code"] == 2
    with pytest.raises(ValueError):
        experiment.review("stage2")


def test_comparison_budget_exact_parameters_and_agreements(experiment):
    run(experiment)
    report = run(experiment, "comparison")
    assert len(experiment.test_calls) == 30
    assert report["comparison_coverage"] == report["observed_taken_agreements"] == 20
    assert not report["semantic_disagreements"]
    assert [r for _, r in experiment.test_calls[10:]] == experiment.plan("comparison")
    assert "stage2" not in report["phases"]
    assert experiment.review("stage2")


def test_cancel_during_pacing_saves_results_and_cannot_replay(experiment):
    def wait(seconds):
        experiment.cancel.set()
        return True

    experiment.wait = wait
    report = run(experiment)
    assert len(experiment.test_calls) == 1
    assert report["phases"]["stage1"]["stop_reason"] == "cancelled"
    assert len(report["account_observations"]) == 20
    assert experiment.journal_path.exists()
    with pytest.raises(ValueError):
        experiment.review("stage2")
    with RobloxActivity(experiment.database):
        pass  # Cancellation without an unsafe response is not a provider restriction.


@pytest.mark.parametrize(
    "control",
    [
        {"active_run": "other"},
        {"blocked": True},
        {"restriction_streak": 1},
        {"not_before": "2099-01-01T00:00:00+00:00"},
    ],
)
def test_production_guard_does_not_start_when_control_blocks(experiment, control):
    experiment.control = lambda: control
    with pytest.raises(ValueError):
        run(experiment)
    assert not experiment.test_calls and not experiment.events()


def test_shared_guard_blocks_bulk_and_individual_without_job_mutation(experiment):
    store = Store(experiment.database)
    repo = RobloxStore(store)
    identity = repo.create_run(["ValidName"])
    try:
        with RobloxActivity(experiment.database):
            with pytest.raises(ValueError):
                run(experiment)
            runner = RobloxRunner(repo, fetcher=lambda _: pytest.fail("No request"))
            with pytest.raises(ValueError):
                runner.run(identity)
        assert repo.progress(identity)["state"] == "Ready"
        assert not experiment.events()
    finally:
        store.close()


def test_pending_gate_survives_crash_and_blocks_other_checkers(experiment):
    with RobloxActivity(experiment.database) as guard:
        guard.mark_pending()
    with pytest.raises(ValueError):
        with RobloxActivity(experiment.database):
            pass


def test_process_lock_and_abrupt_delivery_uncertainty(experiment):
    script = """
import os, sys
from username_finder.roblox_bulk import BulkExperiment
exp = BulkExperiment(sys.argv[1], sys.argv[2], transport=lambda _: os._exit(7))
exp.run('stage1', exp.review('stage1'))
"""
    with RobloxActivity(experiment.database):
        child = subprocess.run(
            [sys.executable, "-c", script, str(experiment.directory), str(experiment.database)],
            capture_output=True,
            timeout=10,
        )
    assert child.returncode != 0 and not experiment.events()
    child = subprocess.run(
        [sys.executable, "-c", script, str(experiment.directory), str(experiment.database)],
        capture_output=True,
        timeout=10,
    )
    assert child.returncode == 7
    assert [e["event"] for e in experiment.events()] == ["begin", "intent"]
    assert experiment.report()["phases"]["stage1"]["state"] == "Interrupted"
    assert experiment.report()["uncertain_or_missing_responses"] == 1
    with pytest.raises(ValueError):
        experiment.review("stage2")
    with pytest.raises(ValueError):
        with RobloxActivity(experiment.database):
            pass


def test_raw_is_durable_before_parser(experiment, monkeypatch):
    actual = bulk.interpret_bulk

    def parse(names, response):
        event = experiment.events()[-1]
        assert event["event"] == "response" and event["response"]["body_base64"]
        return actual(names, response)

    monkeypatch.setattr(bulk, "interpret_bulk", parse)
    run(experiment)


def test_bulk_keeps_existing_database_rows_and_configuration_unchanged(experiment):
    store = Store(experiment.database)
    repo = RobloxStore(store)
    try:
        repo.set_preferred_interval(0.5)
        identity = repo.create_run(["ExistingOne", "ExistingTwo"])
        before = list(store.db.iterdump())
        experiment.control = repo.control
        run(experiment)
        assert list(store.db.iterdump()) == before
        assert repo.progress(identity)["interval_seconds"] == 0.5
    finally:
        store.close()


@pytest.mark.parametrize(
    "payload,expected,conflict",
    [
        (b'{"code":0,"message":"Username is valid"}', "Available", 1),
        (b'{"code":12,"message":"Reserved"}', "Reserved", 1),
        (b'{"code":99,"message":"Unexpected"}', "Unknown", 0),
    ],
)
def test_individual_semantics_separate_from_account_evidence(experiment, payload, expected, conflict):
    run(experiment)
    experiment.transport = lambda _: response(body=payload)
    report = run(experiment, "comparison")
    assert report["comparison_coverage"] == 1
    assert report["observed_disagreements"] == conflict
    assert next(iter(report["individual_observations"].values()))["classification"] == expected
    assert report["counts"] == {"CurrentAccountMatchByAPI": 200}


def test_cross_stage_authorization_cannot_be_reused_after_comparison(experiment):
    run(experiment)
    token = experiment.review("stage2")
    run(experiment, "comparison")
    with pytest.raises(ValueError):
        experiment.run("stage2", token)
    assert len(experiment.test_calls) == 30


def test_cross_batch_conflict_revokes_prior_derived_match(experiment):
    count = 0

    def transport(request):
        nonlocal count
        count += 1
        return response([record(request["usernames"][0], uid=1)])

    experiment.transport = transport
    report = run(experiment)
    assert count == 2
    first = experiment.manifest()["candidates"][0]
    assert report["account_observations"][first]["state"] == "UnsafeOrContradictoryResponse"
    assert (
        report["account_observations"][first]["previous_observation"]["state"] == "CurrentAccountMatchByAPI"
    )


def test_exports_durable_raw_evidence_and_original_manifest(experiment, tmp_path):
    original = experiment.manifest_path.read_bytes()
    run(experiment)
    experiment.export(tmp_path / "export.json", "json")
    experiment.export(tmp_path / "export.csv", "csv")
    exported = json.loads((tmp_path / "export.json").read_bytes())
    assert exported["evidence"][2]["event"] == "response"
    assert exported["evidence"][2]["response"]["body_base64"]
    with (tmp_path / "export.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1000
    assert sum(r["account_evidence_state"] == "NotAttempted" for r in rows) == 800
    assert all(r["manual_confirmation"] == "Not performed" for r in rows)
    assert experiment.manifest_path.read_bytes() == original
    with pytest.raises(FileExistsError):
        experiment.export(tmp_path / "export.json", "json")


def test_partial_and_tampered_journal_fail_closed(experiment):
    run(experiment)
    with experiment.journal_path.open("ab") as stream:
        stream.write(b'{"event":')
    original = experiment.journal_path.read_bytes()
    with pytest.raises(ValueError, match="Interrupted journal"):
        experiment.review("stage2")
    report = experiment.report()
    assert report["journal_integrity_warning"] and report["completed_bulk_batches"] == 10
    experiment.export(experiment.directory / "partial-report.json", "json")
    assert experiment.journal_path.read_bytes() == original


@pytest.mark.parametrize("failure", [None, "timeout", "length", "duplicate_header", "framing", "challenge"])
def test_bulk_transport_reused_contract_and_cleanup(monkeypatch, failure):
    payload = b'{"data":[]}'
    requests, sockets = [], []

    class Stream:
        status = 200

        def __init__(self):
            self.headers = Message()
            self.headers.add_header("Content-Type", "application/json")
            self.headers.add_header("Content-Length", "500" if failure == "length" else str(len(payload)))
            self.headers.add_header("Set-Cookie", "never-store-this")
            if failure == "duplicate_header":
                self.headers.add_header("Content-Type", "text/html")
            if failure == "framing":
                self.headers.add_header("Transfer-Encoding", "chunked")
            if failure == "challenge":
                self.headers.add_header("rblx-challenge-id", "opaque-never-store")
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read1(self, size):
            if failure == "timeout":
                raise TimeoutError
            result = b"" if self.sent else payload
            self.sent = True
            return result

    class Connection:
        def __init__(self, host, *, timeout, context):
            assert host == "users.roblox.com" and timeout == 10
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            self.sock, self.closed = self, False
            sockets.append(self)

        def connect(self):
            pass

        def settimeout(self, seconds):
            assert 0 < seconds <= 10

        def request(self, method, path, *, body, headers):
            requests.append((method, path, json.loads(body)))
            assert not any(k.lower() in ("cookie", "authorization") for k in headers)

        def getresponse(self):
            return Stream()

        def close(self):
            self.closed = True

    monkeypatch.setattr(bulk.http.client, "HTTPSConnection", Connection)
    observed = bulk.Transport()(
        {"kind": "bulk", "method": "POST", "url": bulk.BULK_URL, "usernames": ["abcd"]}
    )
    assert requests == [("POST", "/v1/usernames/users", {"usernames": ["abcd"], "excludeBannedUsers": False})]
    assert len(sockets) == 1 and sockets[0].closed
    assert "set-cookie" not in observed.headers and "rblx-challenge-id" not in observed.headers
    if failure:
        assert bulk.response_stop(observed)
    else:
        assert observed.body == payload and not bulk.response_stop(observed)


@pytest.mark.parametrize(
    "url",
    [
        "http://users.roblox.com/v1/usernames/users",
        "https://example.invalid/v1/usernames/users",
        "https://users.roblox.com/other",
    ],
)
def test_transport_rejects_other_destinations_without_network(url):
    with pytest.raises(ValueError):
        bulk.Transport()({"kind": "bulk", "method": "POST", "url": url, "usernames": ["abcd"]})
