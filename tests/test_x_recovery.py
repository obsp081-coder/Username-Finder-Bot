import json
import threading
from datetime import timedelta

import pytest
from test_x import Clock, response, run
from test_x import repo as repo

from username_finder import x
from username_finder.storage import Store
from username_finder.x_recovery import XRecovery
from username_finder.x_runner import XRunner
from username_finder.x_storage import XStore


def blocked(repo, *, headers=None, count=2, position=1):
    clock = Clock()
    repo.set_interval(0)
    identity = repo.prepare({"generation": x.generation_spec(8, count)})
    calls = []

    def fetch(*args):
        calls.append(args)
        return x.protocol.Response(429, b"", headers or {}) if len(calls) == position else response()

    run(repo, identity=identity, clock=clock, fetcher=fetch)
    return identity, clock, XRecovery(repo, clock=clock.now)


def test_no_request_or_release_without_explicit_acknowledgements(repo, monkeypatch):
    _, clock, recovery = blocked(repo)
    monkeypatch.setattr(x.protocol, "fetch", lambda *a, **k: pytest.fail("No automatic request"))
    assert recovery.status()["eligible"]
    assert recovery.events() == []
    with pytest.raises(ValueError):
        recovery.check()
    with pytest.raises(ValueError):
        recovery.release()
    with pytest.raises(ValueError):
        recovery.release(acknowledged=True)
    clock.seconds += 86400
    assert repo.control()["blocked"]
    assert recovery.events() == []


@pytest.mark.parametrize(
    "route",
    [
        {"mode": "direct", "proxy_url": ""},
        {"mode": "proxy", "proxy_url": "https://localhost:8443"},
    ],
)
def test_one_request_uses_original_route_despite_changed_preferences(repo, monkeypatch, route):
    repo.set_connection(route)
    identity, clock, recovery = blocked(repo)
    before = repo.progress(identity)
    history = repo.results(history=True)
    repo.set_connection({"mode": "proxy", "proxy_url": "http://different.example:8080"})
    calls = []

    def fetch(name, timeout, *, connection):
        calls.append((name, timeout, connection))
        return response(False, "taken")

    monkeypatch.setattr(x.protocol, "fetch", fetch)
    recovery.check(confirmed=True)
    assert calls == [("OpenAI", 15, route)]
    event = recovery.events()[0]
    assert event["state"] == "Succeeded" and event["released_at"] is None
    assert event["restriction"]["run_id"] == identity
    assert event["evidence"]["http_status"] == 200 and event["evidence"]["classification"] == "Taken"
    assert event["connection"] == route
    assert repo.control()["blocked"]
    assert repo.progress(identity) == before and repo.results(history=True) == history
    with pytest.raises(ValueError):
        recovery.release(acknowledged=True)
    clock.seconds += 5
    assert recovery.status()["release_eligible"]
    with pytest.raises(ValueError):
        recovery.check(confirmed=True)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "sample",
    [
        x.protocol.Response(429, b"", {"retry-after": "120"}),
        x.protocol.Response(401),
        x.protocol.Response(403),
        x.protocol.Response(500),
        x.protocol.Response(200, b"malformed", {"content-type": "application/json"}),
        x.protocol.Response(error="TimeoutError", complete=False),
        x.protocol.Response(error="ConnectionError", complete=False),
        x.protocol.Response(200, response().body, {"challenge-indicator": "present"}),
        response(True, "taken"),
        response(False, "custom_message"),
    ],
)
def test_failed_probe_preserves_original_latch_and_never_retries(repo, sample):
    identity, clock, recovery = blocked(repo)
    original = repo.progress(identity)
    calls = []
    recovery.check(confirmed=True, fetcher=lambda *args: (calls.append(args), sample)[1])
    clock.seconds += 86400
    assert len(calls) == 1 and len(recovery.events()) == 1
    assert recovery.events()[0]["state"] == "Failed"
    assert recovery.events()[0]["evidence"]["classification"] == "Unknown"
    assert repo.control()["blocked"] == "unexpected_http_status:429"
    with pytest.raises(ValueError):
        recovery.release(acknowledged=True)
    assert repo.progress(identity) == original


def test_server_wait_before_probe_and_failed_probe_wait_survive_restart(repo):
    identity, clock, recovery = blocked(repo, headers={"retry-after": "120"})
    with pytest.raises(ValueError):
        recovery.check(confirmed=True, fetcher=lambda *a: pytest.fail("Active wait"))
    clock.seconds += 120
    recovery.check(confirmed=True, fetcher=lambda *a: x.protocol.Response(429, b"", {"retry-after": "300"}))
    assert recovery.status()["state"] == "Waiting"
    old = repo.progress(identity)
    path = repo.store.path
    repo.store.close()
    store = Store(path)
    try:
        other = XStore(store)
        other.recover()
        recovered = XRecovery(other, clock=clock.now)
        clock.seconds += 299
        with pytest.raises(ValueError):
            recovered.check(confirmed=True, fetcher=lambda *a: pytest.fail("New server wait"))
        assert recovered.status()["wait_until"] == (clock.now() + timedelta(seconds=1)).isoformat()
        clock.seconds += 1
        assert recovered.status()["eligible"] and other.control()["blocked"]
        assert len(recovered.events()) == 1 and other.progress(identity) == old
    finally:
        store.close()


def test_malformed_wait_requires_review_instead_of_assumed_expiry(repo):
    _, clock, recovery = blocked(repo)
    recovery.check(
        confirmed=True, fetcher=lambda *a: x.protocol.Response(429, b"", {"retry-after": "unknown"})
    )
    clock.seconds += 86400
    assert recovery.status()["state"] == "ReviewRequired"
    assert not recovery.status()["eligible"]


def test_uncertain_probe_retains_late_response_and_server_wait(repo):
    _, clock, recovery = blocked(repo)

    def fetch(*args):
        repo.recover()
        return x.protocol.Response(429, b"", {"retry-after": "120"})

    recovery.check(confirmed=True, fetcher=fetch)
    event = recovery.events()[0]
    assert event["state"] == "Uncertain" and event["evidence"]["http_status"] == 429
    assert recovery.status()["wait_until"] == (clock.now() + timedelta(seconds=120)).isoformat()
    assert not recovery.status()["eligible"] and repo.control()["blocked"]


def test_legacy_direct_evidence_and_job_without_route_never_use_preferred_proxy(repo, monkeypatch):
    identity, clock, recovery = blocked(repo)
    with repo.store.transaction() as db:
        spec = json.loads(repo.progress(identity)["spec"])
        spec.pop("connection")
        db.execute("UPDATE x_runs SET spec=? WHERE id=?", (json.dumps(spec), identity))
        record = repo.results(history=True)[0][0]
        evidence = json.loads(record["evidence"])
        evidence.pop("connection")
        db.execute("UPDATE x_checks SET evidence=? WHERE id=?", (json.dumps(evidence), record["id"]))
    repo.set_connection({"mode": "proxy", "proxy_url": "http://localhost:8080"})
    calls = []
    monkeypatch.setattr(x.protocol, "fetch", lambda *a, **kw: (calls.append(kw), response(False, "taken"))[1])
    recovery.check(confirmed=True)
    assert calls == [{"connection": {"mode": "direct", "proxy_url": ""}}]


@pytest.mark.parametrize("status", [401, 403])
def test_non_429_original_restrictions_cannot_use_manual_recovery(repo, status):
    clock = Clock()
    identity = repo.prepare(x.input_spec("valid_name"))
    run(repo, identity=identity, clock=clock, fetcher=lambda *a: x.protocol.Response(status))
    recovery = XRecovery(repo, clock=clock.now)
    assert recovery.status()["state"] == "ReviewRequired"
    with pytest.raises(ValueError):
        recovery.check(confirmed=True, fetcher=lambda *a: pytest.fail("Not a recoverable 429"))
    assert recovery.events() == []


def test_interrupted_probe_is_durable_uncertain_and_is_not_repeated(repo):
    identity, clock, recovery = blocked(repo)
    recovery.begin(confirmed=True)
    assert recovery.status()["state"] == "InFlight"
    with pytest.raises(ValueError):
        recovery.begin(confirmed=True)
    repo.recover()
    assert recovery.events()[0]["state"] == "Uncertain"
    assert not recovery.status()["release_eligible"]
    assert repo.progress(identity)["completed"] == 1 and repo.control()["blocked"]


def test_cancellation_preserves_success_evidence_without_authorizing_release(repo):
    _, clock, recovery = blocked(repo)
    cancel = threading.Event()
    calls = []

    def fetch(*args):
        calls.append(args)
        cancel.set()
        return response(False, "taken")

    recovery.check(confirmed=True, fetcher=fetch, cancel=cancel)
    assert len(calls) == 1 and recovery.events()[0]["state"] == "Cancelled"
    clock.seconds += 5
    assert not recovery.status()["release_eligible"]
    assert recovery.events()[0]["evidence"]["http_status"] == 200


def test_explicit_release_new_conservative_job_and_new_429_preserve_81_old_results(repo):
    old, clock, recovery = blocked(repo, count=100, position=81)
    before = repo.progress(old)
    history = repo.results(history=True)
    old_ready = repo.prepare(x.input_spec("old_ready"))
    recovery.check(confirmed=True, fetcher=lambda *a: response(False, "taken"))
    clock.seconds += 5
    recovery.release(acknowledged=True)
    assert not repo.control()["blocked"] and repo.preferred_interval() == 5
    assert recovery.events()[0]["released_at"] == clock.now().isoformat()
    assert repo.progress(old) == before and repo.results(history=True) == history
    assert before["interval_seconds"] == 0 and before["completed"] == 81 and before["pending"] == 19
    with pytest.raises(ValueError):
        repo.start(old_ready)
    repo.set_interval(0)
    fast = repo.prepare(x.input_spec("new_fast"))
    with pytest.raises(ValueError):
        repo.start(fast)
    repo.set_interval(5)
    repo.set_connection({"mode": "proxy", "proxy_url": "http://localhost:8080"})
    proxy_job = repo.prepare(x.input_spec("new_proxy"))
    with pytest.raises(ValueError):
        repo.start(proxy_job)
    repo.set_connection({"mode": "direct", "proxy_url": ""})
    good = repo.prepare(x.input_spec("new_good\nnew_next"))
    calls = []
    run(repo, identity=good, clock=clock, fetcher=lambda *a: (calls.append(a), x.protocol.Response(429))[1])
    assert len(calls) == 1 and repo.control()["blocked"] == "unexpected_http_status:429"
    assert repo.progress(good)["completed"] == repo.progress(good)["pending"] == 1
    assert repo.progress(old) == before
    assert len(repo.results(history=True)[0]) == 82
    assert not recovery.status()["release_eligible"]


def test_recognized_probe_release_restart_and_new_job_complete_once(repo):
    old, clock, recovery = blocked(repo)
    recovery.check(confirmed=True, fetcher=lambda *a: response(False, "taken"))
    clock.seconds += 5
    path = repo.store.path
    repo.store.close()
    store = Store(path)
    try:
        other = XStore(store)
        other.recover()
        review = XRecovery(other, clock=clock.now)
        assert review.status()["release_eligible"] and other.control()["blocked"]
        review.release(acknowledged=True)
        new = other.prepare(x.input_spec("new_one\nNEW_ONE\nnew_two"))
        XRunner(other, clock=clock.now, fetcher=clock.fetch, wait=clock.wait).run(new)
        assert other.progress(new)["state"] == "Completed" and len(clock.calls) == 2
        assert other.progress(new)["duplicates"] == 1 and other.progress(new)["interval_seconds"] == 5
        assert other.progress(old)["state"] == "Restricted"
        assert json.loads(other.history("new_one")[0]["evidence"])["preliminary"]
    finally:
        store.close()
