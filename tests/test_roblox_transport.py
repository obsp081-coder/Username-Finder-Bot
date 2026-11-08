"""Verified loopback TLS and durable sequential-worker regressions; no provider access."""

import hashlib
import http.client
import json
import socket
import ssl
import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime

import pytest
from test_roblox import repository as repository
from test_roblox import response
from test_roblox_speed import Clock

from tools.roblox_pipeline_benchmark import local_server
from username_finder import roblox_protocol as protocol
from username_finder.overnight import OvernightStore, retryable_response
from username_finder.roblox import result_for
from username_finder.roblox_runner import RobloxRunner
from username_finder.roblox_storage import RobloxStore, fingerprint
from username_finder.storage import Store


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    original = socket.create_connection

    def connect(address, *args, **kwargs):
        assert address[0] in ("localhost", "127.0.0.1", "::1"), "Provider traffic is forbidden"
        return original(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", connect)


def session(server, context, *, host="localhost", timeout=1):
    return protocol.PersistentTransport(
        context=context,
        connection_factory=lambda: http.client.HTTPSConnection(
            host, server.server_port, context=context, timeout=timeout
        ),
    )


def url(index):
    return protocol.request_url(f"Bench{index:08d}")


@pytest.mark.parametrize("close_every,connections", [(0, 1), (1, 3), (2, 2)])
def test_verified_tls_reuses_connection_and_honors_explicit_server_close(close_every, connections):
    with local_server(close_every=close_every) as (server, context):
        transport = session(server, context)
        try:
            results = [transport.fetch(url(i)) for i in range(3)]
            assert [protocol.classify(r)[0] for r in results] == ["Available", "Taken", "Reserved"]
            assert all(r.complete and not r.error for r in results)
            assert server.requests == [0, 1, 2] and len(server.connect_times) == connections
        finally:
            transport.close()
        assert transport.connection is None


@pytest.mark.parametrize("kind", ["untrusted", "hostname"])
def test_tls_certificate_and_hostname_verification_cannot_be_bypassed(kind):
    with local_server() as (server, trusted):
        context = ssl.create_default_context() if kind == "untrusted" else trusted
        transport = session(server, context, host="127.0.0.1" if kind == "hostname" else "localhost")
        result = transport.fetch(url(0))
        assert result.error == "tls_error" and not result.complete
        assert not retryable_response(result) and server.requests == []
        assert transport.connection is None
    with pytest.raises(ValueError, match="Verified TLS"):
        protocol.PersistentTransport(context=ssl._create_unverified_context())


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500])
def test_http_errors_preserve_evidence_without_following_redirects_or_replaying(status):
    with local_server() as (server, context):
        server.status = status
        server.extra_headers = {"Retry-After": "60", "Location": "https://example.invalid/"}
        transport = session(server, context)
        result = transport.fetch(url(0))
        assert result.status == status and result.complete and result.body
        assert result.headers["retry-after"] == "60"
        assert protocol.classify(result)[0] == "Unknown"
        assert server.requests == [0] and not retryable_response(result)
        assert transport.connection is None


def test_challenge_presence_is_saved_without_opaque_session_values():
    with local_server() as (server, context):
        server.extra_headers = {"rblx-challenge-id": "opaque-test-value"}
        transport = session(server, context)
        result = transport.fetch(url(0))
        assert result.challenge and protocol.classify(result)[0] == "Unknown"
        assert "opaque-test-value" not in json.dumps(result_for("Bench00000000", result))
        assert transport.connection is None


@pytest.mark.parametrize("body", [b"{", b'{"code":0,"message":"Username is invalid"}', b'{"errors":[]}'])
def test_malformed_and_ambiguous_bodies_keep_existing_parser_semantics(body):
    with local_server() as (server, context):
        server.body = body
        transport = session(server, context)
        try:
            result = transport.fetch(url(0))
            assert protocol.classify(result)[0] == "Unknown" and result.body == body
        finally:
            transport.close()


def test_partial_body_is_not_complete_and_is_never_automatically_replayed():
    with local_server() as (server, context):
        server.truncate = 10
        transport = session(server, context)
        result = transport.fetch(url(0))
        assert result.status == 200 and result.body.startswith(b'{"code":0')
        assert not result.complete and result.error == "incomplete_body"
        assert protocol.classify(result)[0] == "Unknown" and not retryable_response(result)
        assert server.requests == [0] and transport.connection is None


def test_idle_server_close_is_retired_before_dispatch_without_duplicate_checks():
    with local_server() as (server, context):
        server.silent_close = True
        transport = session(server, context)
        try:
            assert not transport.fetch(url(0)).error
            assert server.closed.wait(2)
            assert not transport.fetch(url(1)).error
            assert server.requests == [0, 1] and len(server.connect_times) == 2
        finally:
            transport.close()


def test_chunked_responses_are_fully_consumed_before_connection_reuse():
    with local_server() as (server, context):
        server.chunked = True
        transport = session(server, context)
        try:
            results = [transport.fetch(url(i)) for i in range(3)]
            assert all(r.complete and not r.error for r in results)
            assert [protocol.classify(r)[0] for r in results] == ["Available", "Taken", "Reserved"]
            assert len(server.connect_times) == 1 and server.requests == [0, 1, 2]
        finally:
            transport.close()


def test_real_transport_restriction_stops_night_worker_after_one_committed_result(repository, monkeypatch):
    repository.set_preferred_interval(0)
    identity = repository.create_run(["Bench00000000", "Bench00000001"])
    night = OvernightStore(repository.store, "Roblox")
    night.enable(identity, True)
    with local_server() as (server, context):
        server.status, server.extra_headers = 429, {"Retry-After": "60"}
        transport = session(server, context)
        monkeypatch.setattr(protocol, "PersistentTransport", lambda: transport)
        RobloxRunner(repository).run(identity)
        assert server.requests == [0] and transport.connection is None
    assert repository.progress(identity)["state"] == "Restricted"
    assert repository.progress(identity)["counts"] == {"Done": 1, "Pending": 1}
    assert repository.history("Bench00000000")[0]["classification"] == "Unknown"
    assert night.get(identity)["state"] == "Review" and night.startup_candidates(repository) == []
    assert datetime.fromisoformat(repository.control()["not_before"]) > datetime.now(UTC)


def test_response_size_is_bounded_and_retained_as_incomplete_evidence():
    with local_server() as (server, context):
        server.body = b"a" * (protocol.MAX_BODY + 100)
        transport = session(server, context)
        result = transport.fetch(url(0))
        assert result.error == "response_too_large" and not result.complete
        assert len(result.body) == protocol.MAX_BODY + 1 and not retryable_response(result)
        assert transport.connection is None and server.requests == [0]


def test_timeout_after_headers_preserves_status_and_never_retries():
    with local_server() as (server, context):
        server.body_delay = 0.15
        transport = session(server, context, timeout=0.05)
        result = transport.fetch(url(0))
        assert result.status == 200 and result.error == "timeout" and not result.complete
        assert not retryable_response(result) and server.requests == [0]
        assert transport.connection is None


def test_disconnect_after_server_received_request_is_not_replayed():
    with local_server() as (server, context):
        server.drop_before_headers = True
        transport = session(server, context)
        result = transport.fetch(url(0))
        assert result.status is None and result.error == "network_error" and not result.complete
        assert not retryable_response(result) and server.requests == [0]
        server.drop_before_headers = False
        try:
            assert protocol.classify(transport.fetch(url(1)))[0] == "Taken"
            assert server.requests == [0, 1]
        finally:
            transport.close()


@pytest.mark.parametrize("phase", ["connect", "request", "response"])
def test_only_failure_before_dispatch_can_enter_existing_overnight_retry_budget(phase):
    attempts = []

    class Connection:
        sock = None

        def connect(self):
            if phase == "connect":
                raise TimeoutError()
            self.sock = object()

        def request(self, *args, **kwargs):
            attempts.append("dispatch")
            if phase == "request":
                raise ConnectionResetError()

        def getresponse(self):
            raise http.client.RemoteDisconnected()

        def close(self):
            self.sock = None

    transport = protocol.PersistentTransport(connection_factory=Connection)
    result = transport.fetch(url(0))
    assert len(attempts) == (0 if phase == "connect" else 1)
    assert retryable_response(result) == (phase == "connect")
    assert protocol.classify(result)[0] == "Unknown" and transport.connection is None


def test_worker_owns_one_session_and_commits_inflight_before_network(repository, monkeypatch):
    events = []

    class Transport:
        def fetch(self, target):
            events.append(target)
            assert (
                repository.store.db.execute(
                    "SELECT COUNT(*) FROM roblox_queue WHERE state='InFlight'"
                ).fetchone()[0]
                == 1
            )
            assert not repository.store.db.in_transaction
            return response(1)

        def close(self):
            events.append("closed")

    monkeypatch.setattr(protocol, "PersistentTransport", Transport)
    repository.set_preferred_interval(0)
    identity = repository.create_run(["FirstName", "SecondName"])
    RobloxRunner(repository).run(identity)
    assert events == [protocol.request_url("FirstName"), protocol.request_url("SecondName"), "closed"]
    assert repository.progress(identity)["state"] == "Completed"


def test_cancellation_during_request_commits_result_then_closes_without_next_request(repository, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls, closed = [], []

    class Transport:
        def fetch(self, target):
            calls.append(target)
            entered.set()
            assert release.wait(3)
            return response(1)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(protocol, "PersistentTransport", Transport)
    repository.set_preferred_interval(0)
    identity = repository.create_run(["FirstName", "SecondName"])
    runner = RobloxRunner(repository)
    thread = threading.Thread(target=runner.run, args=(identity,))
    thread.start()
    try:
        assert entered.wait(3)
        runner.cancel.set()
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and len(calls) == len(closed) == 1
    assert repository.progress(identity)["counts"] == {"Done": 1, "Pending": 1}
    assert repository.history("FirstName")[0]["classification"] == "Taken"


def test_atomic_overnight_metadata_uses_only_two_transactions_per_candidate(repository):
    repository.set_preferred_interval(0)
    identity = repository.create_run([f"Name{i:04d}" for i in range(20)])
    night = OvernightStore(repository.store, "Roblox")
    night.enable(identity, True)
    commits = []
    repository.store.db.set_trace_callback(lambda sql: commits.append(sql) if sql == "COMMIT" else None)
    try:
        RobloxRunner(repository, fetcher=lambda _: response(1)).run(identity)
    finally:
        repository.store.db.set_trace_callback(None)
    assert len(commits) == 2 * 20 + 4  # Start, night begin, run end and night end.
    assert night.get(identity)["state"] == "Completed"
    assert repository.store.db.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_chronological_fast_path_keeps_backdated_and_uncertain_history_correct(repository):
    records = [(2, 0, True), (3, 1, True), (1, 12, True), (3, 12, False), (4, 0, True)]
    with repository.store.transaction() as db:
        for index, (day, code, completed) in enumerate(records):
            result = result_for("HistoryName", response(code), checked_at=f"2026-01-{day:02d}T00:00:00+00:00")
            repository._insert(db, result, "test", str(index), completed=completed)
    rows = list(reversed(repository.history("HistoryName")))
    assert [r["classification"] for r in rows] == ["Reserved", "Available", "Taken", "Reserved", "Available"]
    assert [r["previous_classification"] for r in rows] == [
        None,
        "Reserved",
        "Available",
        "Taken",
        "Reserved",
    ]
    assert [r["changed"] for r in rows] == [0, 1, 1, 1, 1]
    assert repository.results()[0][0]["completed_checks"] == 4
    for row in rows:
        evidence = json.loads(row["evidence"])
        assert row["fingerprint"] == fingerprint(evidence)
        assert evidence["raw_sha256"] == hashlib.sha256(evidence["response_data"].encode()).hexdigest()


def test_night_metadata_failure_rolls_back_claim_and_completion_together(repository, monkeypatch):
    identity = repository.create_run(["FirstName"])
    repository.start(identity)
    night = OvernightStore(repository.store, "Roblox")
    night.enable(identity, True, active=True)

    def clock():
        return datetime.now(UTC)

    def fail(*args, **kwargs):
        raise OSError("synthetic persistence failure")

    monkeypatch.setattr(night, "requesting", fail)
    with pytest.raises(OSError):
        repository.claim_next(identity, night=night, clock=clock)
    assert repository.progress(identity)["counts"] == {"Pending": 1}
    task = repository.claim_next(identity)
    monkeypatch.setattr(night, "success", fail)
    with pytest.raises(OSError):
        repository.finish_check(task, result_for("FirstName", response(1)), night=night, clock=clock)
    assert repository.history("FirstName") == []
    assert repository.progress(identity)["counts"] == {"InFlight": 1}
    repository.recover()
    assert repository.history("FirstName")[0]["classification"] == "Unknown"


def test_cancellation_after_retry_claim_does_not_spend_an_unsent_retry(repository, monkeypatch):
    repository.set_preferred_interval(0)
    identity = repository.create_run(["FirstName", "OtherName"])
    night = OvernightStore(repository.store, "Roblox")
    night.enable(identity, True)
    clock = Clock()
    failure = protocol.Response(error="network_error", complete=False)
    failure.retryable = True
    calls = []
    runner = RobloxRunner(
        repository, fetcher=lambda _: (calls.append(True), failure)[1], clock=clock.now, wait=clock.wait
    )
    original = repository.claim_next
    claims = []

    def claim(*args, **kwargs):
        task = original(*args, **kwargs)
        claims.append(task)
        if len(claims) == 2:
            runner.manual_stop.set()
            runner.cancel.set()
        return task

    monkeypatch.setattr(repository, "claim_next", claim)
    runner.run(identity)
    assert len(calls) == 1 and len(claims) == 2
    data = night.get(identity)
    assert data["attempts"] == 1 and data["retries_performed"] == 0 and not data["retry_started"]
    assert data["state"] == "Stopped" and not data["auto_resume"]
    assert repository.progress(identity)["counts"] == {"Pending": 2} and repository.history("FirstName") == []


@pytest.mark.parametrize("phase", ["claim", "during_finish", "committed", "last_committed"])
def test_actual_worker_crash_boundaries_preserve_atomic_night_and_delivery_state(tmp_path, phase):
    path = tmp_path / "worker-crash.db"
    worker = textwrap.dedent("""\
        import os,sys
        from username_finder.storage import Store
        from username_finder.roblox_storage import RobloxStore
        from username_finder.roblox_runner import RobloxRunner
        from username_finder.roblox_protocol import Response
        from username_finder.overnight import OvernightStore
        store=Store(sys.argv[1]);phase=sys.argv[2];repo=RobloxStore(store)
        repo.set_preferred_interval(0)
        names=['FirstName'] if phase=='last_committed' else ['FirstName','OtherName']
        identity=repo.create_run(names)
        OvernightStore(store,'Roblox').enable(identity,True)
        def fetch(url):
            if phase=='claim':os._exit(23)
            return Response(200,b'{"code":1,"message":"Taken"}',{'content-type':'application/json'})
        def trace(sql):
            if phase=='during_finish' and "SET state='Done'" in sql:os._exit(23)
        def updated():
            os._exit(23)
        store.db.set_trace_callback(trace)
        RobloxRunner(repo,fetcher=fetch,updated=updated).run(identity)
        """)
    child = subprocess.run([sys.executable, "-c", worker, str(path), phase], capture_output=True, timeout=15)
    assert child.returncode == 23, child.stderr
    store = Store(path)
    try:
        repo = RobloxStore(store)
        repo.recover()
        identity = repo.runs()[0]["id"]
        night = OvernightStore(store, "Roblox")
        eligible = night.startup_candidates(repo)
        assert bool(eligible) == (phase == "committed")
        if eligible:
            calls = []
            RobloxRunner(repo, fetcher=lambda target: (calls.append(target), response(1))[1]).run(
                identity, automatic=True
            )
            assert calls == [protocol.request_url("OtherName")]
        first = repo.history("FirstName")
        assert len(first) == 1
        assert first[0]["classification"] == ("Unknown" if phase in ("claim", "during_finish") else "Taken")
        assert night.get(identity)["state"] == (
            "Review" if phase in ("claim", "during_finish") else "Completed"
        )
        repo.recover()
        assert len(repo.history("FirstName")) == 1
        assert not night.startup_candidates(repo)
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        store.close()
