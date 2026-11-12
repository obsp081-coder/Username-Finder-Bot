"""Individual concurrency with fabricated inputs, real FULL/WAL SQL and no network."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from email.message import Message
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from test_roblox import response

from username_finder import roblox_concurrent as concurrent
from username_finder.overnight import OvernightStore
from username_finder.roblox_activity import RobloxActivity
from username_finder.roblox_runner import RobloxRunner
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("Network forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def repo(tmp_path):
    store = Store(tmp_path / "isolated.db")
    repository = RobloxStore(store)
    repository.set_preferred_interval(0)
    yield repository
    store.close()


def prepare(repo, names=None, *, workers=3, rate=5):
    identity = repo.create_run(names or ["Alpha", "Bravo", "Charlie"], skip=False)
    concurrent.configure_execution(repo, identity, workers=workers, rate=rate)
    return identity


@pytest.fixture
def accelerated(monkeypatch):
    original = concurrent.ConcurrentExecution

    def execution(*a, **kw):
        started, wall = time.monotonic(), datetime.now(UTC)
        value = original(
            *a,
            **kw,
            monotonic=lambda: (time.monotonic() - started) * 100,
            wall=lambda: wall + timedelta(seconds=(time.monotonic() - started) * 100),
        )
        value.wait = lambda delay: value.halt.wait(delay / 100)
        return value

    monkeypatch.setattr(concurrent, "ConcurrentExecution", execution)


def start(runner, identity):
    errors = []

    def run():
        try:
            runner.run(identity)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    return worker, errors


def settle(worker, runner):
    worker.join(5)
    if worker.is_alive():
        runner.cancel.set()
        worker.join(2)
    assert not worker.is_alive()


@pytest.mark.parametrize("rate,saved_delay", [(2, 0), (0.25, 0), (5, 0), (2, 2)])
def test_central_admission_has_no_burst_and_durable_intent_deadline(repo, rate, saved_delay):
    identity = prepare(repo, rate=rate)
    parent = RobloxRunner(repo, fetcher=lambda _: response())
    at = [0.0]
    wall = datetime(2030, 1, 1, tzinfo=UTC)
    gap = 1 / rate
    if saved_delay:
        with repo.store.transaction() as db:
            control = repo._control(db)
            control["not_before"] = (wall + timedelta(seconds=saved_delay)).isoformat()
            repo._save_control(db, control)
    waits = []

    def wait(delay):
        waits.append(delay)
        at[0] += delay

    with RobloxActivity(repo.store.path) as activity:
        repo.start(identity)
        execution = concurrent.ConcurrentExecution(
            parent,
            identity,
            concurrent.execution_policy(repo, identity),
            activity,
            monotonic=lambda: at[0],
            wall=lambda: wall + timedelta(seconds=at[0]),
            wait=wait,
        )
        starts = []
        for _ in range(3):
            task = execution.admit()
            assert task is not None
            starts.append(at[0])
            with repo.store.lock:
                assert (
                    repo.store.db.execute(
                        "SELECT state FROM roblox_queue WHERE run_id=? AND position=?",
                        (identity, task["position"]),
                    ).fetchone()[0]
                    == "InFlight"
                )
            assert datetime.fromisoformat(repo.control()["not_before"]) >= wall + timedelta(
                seconds=at[0] + gap
            )
        first = max(gap, saved_delay)
        assert starts == pytest.approx([first, first + gap, first + 2 * gap])
        assert all(0 < delay <= 0.05 for delay in waits)


def test_bounded_workers_out_of_order_mapping_durable_counts_and_export(repo, accelerated, tmp_path):
    identity = prepare(repo)
    entered = {name: threading.Event() for name in ("Alpha", "Bravo", "Charlie")}
    release = {name: threading.Event() for name in entered}
    committed = {name: threading.Event() for name in entered}
    codes = dict(Alpha=0, Bravo=1, Charlie=12)

    def fetch(url):
        name = parse_qs(urlsplit(url).query)["Username"][0]
        entered[name].set()
        assert release[name].wait(3)
        return response(codes[name])

    def updated():
        for row in repo.results()[0]:
            committed[row["username"]].set()

    runner = RobloxRunner(repo, fetcher=fetch, updated=updated)
    worker, errors = start(runner, identity)
    try:
        assert all(event.wait(3) for event in entered.values())
        assert runner.execution.snapshot()["active"] == 3
        assert runner.execution.snapshot()["outstanding"] == 3
        for name in ("Charlie", "Bravo", "Alpha"):
            release[name].set()
            assert committed[name].wait(3)
    finally:
        for event in release.values():
            event.set()
        settle(worker, runner)
    assert not errors
    assert repo.progress(identity)["counts"] == {"Done": 3}
    for name, expected in (("Alpha", "Available"), ("Bravo", "Taken"), ("Charlie", "Reserved")):
        evidence = json.loads(repo.history(name)[0]["evidence"])
        assert evidence["username"] == name and evidence["classification"] == expected
        assert evidence["raw_sha256"] and evidence["raw_base64"]
    snapshot = runner.execution.snapshot()
    assert not snapshot["waiting"]
    assert snapshot["accepted"] == 3 and snapshot["active"] == snapshot["outstanding"] == 0
    assert snapshot["peak"] == 3 and snapshot["attempts"] == snapshot["http_responses"] == 3
    path = tmp_path / "fabricated.csv"
    repo.export_csv(path, history=True)
    assert all(name in path.read_text(encoding="utf-8-sig") for name in entered)
    with pytest.raises(ValueError):
        repo.finish_check(dict(run_id=identity, position=0), evidence)
    assert sum(repo.counts().values()) == 3


@pytest.mark.parametrize(
    "status,challenge,headers",
    [
        (401, False, {}),
        (403, False, {}),
        (429, False, {"retry-after": "60"}),
        (200, True, {}),
        (200, False, {"retry-after": "20"}),
    ],
)
def test_restriction_with_outstanding_requests_latches_and_never_retries(
    repo, accelerated, status, challenge, headers
):
    identity = prepare(repo, ["Alpha", "Bravo", "Charlie", "Delta", "Echo"])
    both = threading.Barrier(3)
    limited = threading.Event()
    calls, lock = [], threading.Lock()

    def fetch(url):
        name = parse_qs(urlsplit(url).query)["Username"][0]
        with lock:
            calls.append(name)
        both.wait(3)
        if name == "Alpha":
            value = response()
            value.status, value.challenge, value.headers = status, challenge, headers
            limited.set()
            return value
        assert limited.wait(3)
        # Hold the other responses until the first restriction is durable.
        deadline = time.monotonic() + 3
        while not repo.control().get("blocked") and time.monotonic() < deadline:
            time.sleep(0.001)
        return response(1)

    runner = RobloxRunner(repo, fetcher=fetch)
    runner.run(identity)
    assert len(calls) == len(set(calls)) == 3
    assert repo.control()["blocked"]
    assert repo.progress(identity)["state"] == "Restricted"
    assert repo.progress(identity)["counts"] == {"Done": 3, "Pending": 2}
    assert runner.execution.snapshot()["active"] == runner.execution.snapshot()["outstanding"] == 0


def test_header_stop_is_durable_before_reading_response_body(repo):
    identity = prepare(repo)
    parent = RobloxRunner(repo, fetcher=lambda _: response())
    headers = Message()
    headers["Retry-After"] = "60"
    raw = SimpleNamespace(status=429, headers=headers)
    with RobloxActivity(repo.store.path) as activity:
        value = concurrent.ConcurrentExecution(
            parent, identity, concurrent.execution_policy(repo, identity), activity
        )
        connection = concurrent._ObservedConnection(
            SimpleNamespace(getresponse=lambda: raw), value.observe_headers
        )
        assert connection.getresponse() is raw
        assert value.halt.is_set() and repo.control()["blocked"]


def test_cancellation_settles_active_requests_and_preserves_pending(repo, accelerated):
    identity = prepare(repo, [f"Synthetic{i}" for i in range(20)])
    entered = threading.Barrier(4)
    release = threading.Event()
    calls = []

    def fetch(url):
        calls.append(url)
        entered.wait(3)
        assert release.wait(3)
        return response(1)

    runner = RobloxRunner(repo, fetcher=fetch)
    worker, errors = start(runner, identity)
    entered.wait(3)
    runner.cancel.set()
    release.set()
    settle(worker, runner)
    assert not errors and len(calls) == 3
    assert repo.progress(identity)["counts"] == {"Done": 3, "Pending": 17}
    with RobloxActivity(repo.store.path):
        pass


def test_cancel_during_initial_pacing_sends_nothing(repo):
    identity = prepare(repo, rate=0.25)
    runner = RobloxRunner(repo, fetcher=lambda _: pytest.fail("No dispatch"))
    worker, errors = start(runner, identity)
    runner.cancel.set()
    settle(worker, runner)
    assert not errors and repo.progress(identity)["counts"] == {"Pending": 3}


def test_persistence_failure_surfaces_and_recovers_only_uncertain_intents(repo, accelerated, monkeypatch):
    identity = prepare(repo)
    original = repo._insert

    def insert(db, result, source, key, *a, **kw):
        if source == "roblox-api" and result["username"] == "Bravo":
            raise OSError("synthetic commit failure")
        return original(db, result, source, key, *a, **kw)

    monkeypatch.setattr(repo, "_insert", insert)
    runner = RobloxRunner(repo, fetcher=lambda _: response(1))
    with pytest.raises(OSError, match="synthetic commit failure"):
        runner.run(identity)
    assert repo.progress(identity)["state"] == "Error"
    assert repo.history("Bravo")[0]["classification"] == "Unknown"
    assert not repo.history("Bravo")[0]["completed"]
    assert runner.execution.snapshot()["outstanding"] == 0
    repo.recover()
    assert len(repo.history("Bravo")) == 1


@pytest.mark.parametrize(
    "marker,content",
    [
        ("block", b"http_429"),
        ("pending", b"Pending:historical\n"),
        ("cooldown", b"invalid historical evidence\n"),
    ],
)
def test_historical_markers_remain_authoritative_and_byte_identical(repo, marker, content):
    identity = prepare(repo)
    path = getattr(RobloxActivity(repo.store.path), marker)
    path.write_bytes(content)
    runner = RobloxRunner(repo, fetcher=lambda _: pytest.fail("Blocked"))
    with pytest.raises(ValueError):
        runner.run(identity)
    assert path.read_bytes() == content and repo.progress(identity)["counts"] == {"Pending": 3}


def test_shared_lock_and_automatic_or_overnight_activation_are_rejected(repo):
    identity = prepare(repo)
    runner = RobloxRunner(repo, fetcher=lambda _: pytest.fail("No dispatch"))
    with RobloxActivity(repo.store.path), pytest.raises(ValueError):
        runner.run(identity)
    with pytest.raises(ValueError, match="manual"):
        runner.run(identity, automatic=True)
    OvernightStore(repo.store, "Roblox").enable(identity, True)
    with pytest.raises(ValueError, match="manual"):
        runner.run(identity)


@pytest.mark.parametrize(
    "changes",
    [
        dict(workers=True),
        dict(workers=9),
        dict(workers=0),
        dict(rate=0),
        dict(rate=6),
        dict(rate=float("nan")),
        dict(rate=True),
    ],
)
def test_policy_validation_cannot_weaken_bounds(repo, changes):
    identity = repo.create_run(["Alpha"])
    kwargs = dict(workers=2, rate=1)
    kwargs.update(changes)
    with pytest.raises(ValueError):
        concurrent.configure_execution(repo, identity, **kwargs)
    assert "individual_execution" not in repo.control()


def test_default_remains_sequential_and_run_policy_survives_reopen(repo, monkeypatch):
    identity = repo.create_run(["Alpha", "Bravo"])
    assert concurrent.execution_policy(repo, identity)["workers"] == 1
    monkeypatch.setattr(concurrent, "ConcurrentExecution", lambda *a, **kw: pytest.fail("Sequential default"))
    calls = []
    RobloxRunner(repo, fetcher=lambda url: (calls.append(url), response(1))[1]).run(identity)
    assert len(calls) == 2
    second = prepare(repo, ["Charlie"], workers=2, rate=0.5)
    store = Store(repo.store.path)
    try:
        assert concurrent.execution_policy(RobloxStore(store), second) == dict(version=1, workers=2, rate=0.5)
    finally:
        store.close()


@pytest.mark.parametrize("phase", ["before_dispatch", "inflight", "after_response", "after_commit"])
def test_multiple_durable_intents_survive_hard_process_exit_without_replay(repo, phase):
    identity = prepare(repo, [f"Synthetic{i}" for i in range(5)])
    path = repo.store.path
    child = r"""
import os, sys, socket, threading
from username_finder.storage import Store
from username_finder.roblox_storage import RobloxStore
from username_finder.roblox_activity import RobloxActivity
from username_finder.roblox_runner import RobloxRunner
from username_finder.roblox_protocol import Response
store=Store(sys.argv[1]); repo=RobloxStore(store)
def forbidden(*a, **kw): raise AssertionError("No network")
socket.socket.connect=forbidden
if sys.argv[3]=="before_dispatch":
    with RobloxActivity(store.path):
        repo.start(sys.argv[2])
        for _ in range(3): repo.claim_next(sys.argv[2], not_before="2030-01-01T00:00:00+00:00")
        os._exit(23)
barrier=threading.Barrier(3)
def fetch(url):
    barrier.wait(3)
    if sys.argv[3]=="inflight": os._exit(23)
    return Response(status=200,body=b'{"code":0,"message":"Username is valid"}',
                    headers={"content-type":"application/json"})
original=repo.finish_check
def finish(*a, **kw):
    if sys.argv[3]=="after_response": os._exit(23)
    original(*a, **kw)
    os._exit(23)
repo.finish_check=finish
RobloxRunner(repo,fetcher=fetch).run(sys.argv[2])
"""
    result = subprocess.run(
        [sys._base_executable, "-B", "-c", child, str(path), identity, phase],
        cwd=__import__("pathlib").Path(__file__).resolve().parents[1],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
        timeout=10,
        capture_output=True,
    )
    assert result.returncode == 23, result.stderr
    repo.recover()
    repo.recover()
    assert repo.progress(identity)["counts"] == {"Done": 3, "Pending": 2}
    with repo.store.lock:
        rows = repo.store.db.execute("SELECT classification,completed,evidence FROM roblox_checks").fetchall()
    assert len(rows) == 3
    known = 1 if phase == "after_commit" else 0
    assert sum(r[0] == "Available" and r[1] for r in rows) == known
    assert sum(r[0] == "Unknown" and not r[1] for r in rows) == 3 - known
    assert all(json.loads(r[2])["delivery_uncertain"] for r in rows if not r[1])
    with RobloxActivity(path):
        pass


def test_failure_after_commit_does_not_duplicate_or_hide_a_saved_result(repo, accelerated, monkeypatch):
    identity = prepare(repo, ["Alpha"])
    finish = repo.finish_check

    def committed_then_failed(*a, **kw):
        finish(*a, **kw)
        raise OSError("synthetic failure after commit")

    monkeypatch.setattr(repo, "finish_check", committed_then_failed)
    runner = RobloxRunner(repo, fetcher=lambda _: response())
    with pytest.raises(OSError, match="after commit"):
        runner.run(identity)
    assert len(repo.history("Alpha")) == 1
    assert repo.history("Alpha")[0]["classification"] == "Available"
    assert runner.execution.snapshot()["accepted"] == 1
    assert runner.execution.snapshot()["counts"] == {"Available": 1}


def test_intent_commit_failure_prevents_dispatch(repo, accelerated, monkeypatch):
    identity = prepare(repo)

    def failed_claim(*a, **kw):
        raise OSError("synthetic intent failure")

    monkeypatch.setattr(repo, "claim_next", failed_claim)
    runner = RobloxRunner(repo, fetcher=lambda _: pytest.fail("Intent must commit first"))
    with pytest.raises(OSError, match="intent"):
        runner.run(identity)
    assert repo.progress(identity)["counts"] == {"Pending": 3}


@pytest.mark.parametrize("fixture", ["malformed", "undocumented", "incomplete", "timeout", "network_error"])
def test_ambiguous_and_transport_failures_are_unknown_without_replay(repo, accelerated, fixture):
    from tools.roblox_pipeline_benchmark import semantic_fixtures

    identity = prepare(repo)
    calls = []

    def fetch(url):
        calls.append(url)
        return semantic_fixtures()[fixture]

    runner = RobloxRunner(repo, fetcher=fetch)
    runner.run(identity)
    assert len(calls) == len(set(calls))
    assert repo.counts() == {"Unknown": len(calls)}
    assert repo.control()["blocked"] and runner.execution.snapshot()["accepted"] == 0


def test_each_worker_owns_and_closes_one_transport(repo, accelerated, monkeypatch):
    identity = prepare(repo)
    transports = []
    barrier = threading.Barrier(3)

    class FakeTransport:
        def __init__(self):
            self.connection_factory = lambda: None
            self.owner = None
            self.closed = 0
            transports.append(self)

        def fetch(self, url):
            self.owner = threading.get_ident()
            barrier.wait(3)
            return response(1)

        def close(self):
            assert self.owner == threading.get_ident()
            self.closed += 1

    monkeypatch.setattr(concurrent.protocol, "PersistentTransport", FakeTransport)
    RobloxRunner(repo).run(identity)
    assert len(transports) == 3 and len({t.owner for t in transports}) == 3
    assert all(t.closed == 1 for t in transports)


@pytest.mark.parametrize(
    "status,headers", [(302, {}), (200, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "60"})]
)
def test_exhausted_budget_and_redirect_headers_stop_before_body(repo, status, headers):
    identity = prepare(repo)
    with RobloxActivity(repo.store.path) as activity:
        value = concurrent.ConcurrentExecution(
            RobloxRunner(repo), identity, concurrent.execution_policy(repo, identity), activity
        )
        value.observe_headers(concurrent.protocol.Response(status=status, headers=headers))
        assert value.halt.is_set() and repo.control()["blocked"]
    assert RobloxActivity(repo.store.path).block.read_bytes() == b"individual_review_required"


def test_header_persistence_error_cannot_be_hidden_as_transport_failure(repo, accelerated, monkeypatch):
    identity = prepare(repo, ["Alpha"])
    save = repo._save_control

    def failed_save(db, control):
        if control.get("blocked"):
            raise OSError("synthetic header persistence failure")
        return save(db, control)

    monkeypatch.setattr(repo, "_save_control", failed_save)
    runner = RobloxRunner(repo)

    class FakeTransport:
        def connection_factory(self):
            return None

        def fetch(self, url):
            try:
                runner.execution.observe_headers(concurrent.protocol.Response(status=429))
            except OSError:
                return concurrent.protocol.Response(error="network_error", complete=False)

        def close(self):
            pass

    monkeypatch.setattr(concurrent.protocol, "PersistentTransport", FakeTransport)
    with pytest.raises(OSError, match="header persistence"):
        runner.run(identity)
    assert repo.history("Alpha")[0]["classification"] == "Unknown"
    # SQL failure cannot remove the independent durable shared safety latch.
    assert RobloxActivity(repo.store.path).block.read_bytes() == b"individual_review_required"
    with pytest.raises(ValueError):
        runner.run(identity)


def test_partial_executor_start_failure_stops_and_releases_shared_lock(repo, monkeypatch):
    identity = prepare(repo)
    original = concurrent.ThreadPoolExecutor.submit
    submitted = 0

    def submit(pool, *a, **kw):
        nonlocal submitted
        submitted += 1
        if submitted == 2:
            raise RuntimeError("synthetic worker startup failure")
        return original(pool, *a, **kw)

    monkeypatch.setattr(concurrent.ThreadPoolExecutor, "submit", submit)
    runner = RobloxRunner(repo, fetcher=lambda _: pytest.fail("Stopped during initial pacing"))
    with pytest.raises(RuntimeError, match="startup failure"):
        runner.run(identity)
    assert repo.progress(identity)["counts"] == {"Pending": 3}
    with RobloxActivity(repo.store.path):
        pass


def test_http_send_gate_prevents_bursts_after_tls_workers_become_ready_together(repo):
    identity = prepare(repo, rate=2)
    parent = RobloxRunner(repo, fetcher=lambda _: response())
    at = [0.0]
    wall = datetime(2030, 1, 1, tzinfo=UTC)
    starts = []

    def wait(delay):
        at[0] += delay

    with RobloxActivity(repo.store.path) as activity:
        repo.start(identity)
        value = concurrent.ConcurrentExecution(
            parent,
            identity,
            concurrent.execution_policy(repo, identity),
            activity,
            monotonic=lambda: at[0],
            wall=lambda: wall + timedelta(seconds=at[0]),
            wait=wait,
        )
        value.admit()
        value.admit()
        at[0] += 3  # Both simulated TLS connections become ready now.
        connection = concurrent._ObservedConnection(
            SimpleNamespace(request=lambda *a, **kw: starts.append(at[0])), lambda _: None, value.transmit
        )
        connection.request("GET", "/fabricated")
        connection.request("GET", "/fabricated")
        assert starts[1] - starts[0] == pytest.approx(0.5)
        parent.cancel.set()
        with pytest.raises(concurrent._NotDispatched):
            connection.request("GET", "/never")
        assert len(starts) == 2


def test_cancellation_after_tls_before_http_keeps_known_unsent_intent_pending(repo, accelerated, monkeypatch):
    identity = prepare(repo, ["Alpha"])
    runner = RobloxRunner(repo)

    class FakeTransport:
        def connection_factory(self):
            return SimpleNamespace(request=lambda *a, **kw: pytest.fail("Canceled before HTTP"))

        def fetch(self, url):
            runner.cancel.set()
            self.connection_factory().request("GET", "/fabricated")

        def close(self):
            pass

    monkeypatch.setattr(concurrent.protocol, "PersistentTransport", FakeTransport)
    runner.run(identity)
    assert repo.progress(identity)["counts"] == {"Pending": 1}
    assert not repo.history("Alpha")


def test_failed_shared_marker_write_preserves_sql_block_and_surfaces_error(repo, accelerated, monkeypatch):
    identity = prepare(repo, ["Alpha"])
    runner = RobloxRunner(repo)

    def failed_marker(*a, **kw):
        raise OSError("synthetic marker failure")

    class FakeTransport:
        def connection_factory(self):
            return None

        def fetch(self, url):
            try:
                runner.execution.observe_headers(concurrent.protocol.Response(status=429))
            except OSError:
                return concurrent.protocol.Response(error="network_error", complete=False)

        def close(self):
            pass

    monkeypatch.setattr(RobloxActivity, "restrict", failed_marker)
    monkeypatch.setattr(concurrent.protocol, "PersistentTransport", FakeTransport)
    with pytest.raises(OSError, match="marker failure"):
        runner.run(identity)
    assert repo.control()["blocked"]
    assert repo.history("Alpha")[0]["classification"] == "Unknown"
    with pytest.raises(ValueError):
        runner.run(identity)
