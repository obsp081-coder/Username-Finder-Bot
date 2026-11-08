import errno
import json
import socket
import ssl
import subprocess
import sys
import urllib.error
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from test_roblox import response as roblox_response
from test_x import Clock
from test_x import response as x_response

from username_finder import roblox_protocol, x, x_protocol
from username_finder.overnight import (
    EVENT_LIMIT,
    OvernightStore,
    retryable_response,
    transient_exception,
    transport_failure,
)
from username_finder.roblox import result_for
from username_finder.roblox_runner import RobloxRunner
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store
from username_finder.x_runner import XRunner
from username_finder.x_storage import XStore


@pytest.fixture
def rigs(tmp_path):
    stores = []

    def create(platform, names=None, *, enabled=True, path=None):
        store = Store(path or tmp_path / f"{platform}-{len(stores)}.db")
        stores.append(store)
        repo = RobloxStore(store) if platform == "Roblox" else XStore(store)
        names = names or ["NightOne", "NightTwo"]
        identity = (
            repo.create_run(names) if platform == "Roblox" else repo.prepare(x.input_spec("\n".join(names)))
        )
        night = OvernightStore(store, platform)
        if enabled:
            night.enable(identity, True)
        return SimpleNamespace(
            store=store, repo=repo, night=night, identity=identity, platform=platform, clock=Clock(), calls=[]
        )

    yield create
    for store in stores:
        store.close()


def execute(rig, outcomes=(), *, wait=None, automatic=False, updated=lambda: None):
    outcomes = iter(outcomes)

    def fetch(value, *args):
        name = parse_qs(urlsplit(value).query)["Username"][0] if rig.platform == "Roblox" else value
        rig.calls.append((name, rig.clock.seconds))
        outcome = next(outcomes, None)
        rig.clock.seconds += 0.25
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome or (roblox_response() if rig.platform == "Roblox" else x_response())

    runner_type = RobloxRunner if rig.platform == "Roblox" else XRunner
    runner = runner_type(
        rig.repo, fetcher=fetch, clock=rig.clock.now, wait=wait or rig.clock.wait, updated=updated
    )
    runner.run(rig.identity, automatic=automatic)
    return runner


@pytest.mark.parametrize("platform", ["Roblox", "X"])
@pytest.mark.parametrize("enabled", [False, True])
def test_optional_default_and_extended_sequential_checking(rigs, platform, enabled):
    rig = rigs(platform, [f"NightName{i:03}" for i in range(100)], enabled=enabled)
    if not enabled:
        assert rig.night.get(rig.identity) is None
    execute(rig)
    assert len(rig.calls) == len({name for name, _ in rig.calls}) == 100
    assert rig.repo.progress(rig.identity)["state"] == "Completed"
    assert rig.repo.results()[1] == 100
    if enabled:
        assert rig.night.get(rig.identity)["state"] == "Completed"
    else:
        assert rig.night.get(rig.identity) is None
    assert all(second[1] - first[1] == 5.25 for first, second in zip(rig.calls, rig.calls[1:], strict=False))


@pytest.mark.parametrize("platform", ["Roblox", "X"])
@pytest.mark.parametrize(
    "failure", [TimeoutError(), ConnectionResetError(), socket.gaierror(socket.EAI_AGAIN, "dns")]
)
def test_short_outage_recovers_original_candidate_without_duplicate_results(rigs, platform, failure):
    rig = rigs(platform)
    execute(rig, [failure])
    assert [name for name, _ in rig.calls] == ["NightOne", "NightOne", "NightTwo"]
    assert rig.calls[1][1] - rig.calls[0][1] == 30.25
    assert rig.calls[2][1] - rig.calls[1][1] == 5.25
    assert rig.repo.progress(rig.identity)["state"] == "Completed"
    assert rig.repo.results()[1] == 2 and len(rig.repo.history("NightOne")) == 1
    data = rig.night.get(rig.identity)
    assert data["attempts"] == data["retries_performed"] == 1 and not data["auto_resume"]
    assert {e["event"] for e in data["events"]} >= {"retry_scheduled", "connectivity_recovered", "completed"}
    assert not rig.repo.control().get("blocked")


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_persistent_failure_exhaustion_is_bounded_and_unknown(rigs, platform):
    rig = rigs(platform)
    execute(rig, [TimeoutError()] * 10)
    assert len(rig.calls) == 4
    assert rig.clock.waits == [30, 60, 120]
    assert rig.repo.results()[1] == 1 and rig.repo.results()[0][0]["classification"] == "Unknown"
    assert rig.night.get(rig.identity)["state"] == "Review"
    assert not rig.night.startup_candidates(rig.repo)
    assert "retry_budget_exhausted" in {e["event"] for e in rig.night.get(rig.identity)["events"]}


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_session_retry_budget_does_not_reset_between_candidates(rigs, platform):
    rig = rigs(platform, [f"Name{i:03}" for i in range(30)])
    valid = roblox_response() if platform == "Roblox" else x_response()
    outcomes = [value for _ in range(20) for value in (TimeoutError(), valid)]
    execute(rig, outcomes)
    assert len(rig.calls) == 25
    assert rig.night.get(rig.identity)["attempts"] == 12
    assert rig.night.get(rig.identity)["retries_performed"] == 12
    assert rig.repo.results()[1] == 13


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_manual_stop_while_waiting_cannot_automatically_restart(rigs, platform):
    rig = rigs(platform)

    def stop(_):
        rig.night.stop(rig.identity)
        return True

    execute(rig, [TimeoutError()], wait=stop)
    assert len(rig.calls) == 1 and rig.repo.results()[1] == 0
    assert rig.night.get(rig.identity)["state"] == "Stopped"
    assert rig.night.startup_candidates(rig.repo) == []
    assert rig.repo.progress(rig.identity)["state"] == "Paused"


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_known_failed_transport_checkpoint_survives_reopen(rigs, platform):
    rig = rigs(platform)
    # A normal shutdown cancels the wait, without setting the manual-stop latch.
    execute(rig, [TimeoutError()], wait=lambda _: True)
    assert rig.night.get(rig.identity)["state"] == "Interrupted"
    reopened = Store(rig.store.path)
    try:
        repo = RobloxStore(reopened) if platform == "Roblox" else XStore(reopened)
        repo.recover()
        night = OvernightStore(reopened, platform)
        assert night.startup_candidates(repo) == [rig.identity]
        rig.store, previous = reopened, rig.store
        rig.repo, rig.night = repo, night
        execute(rig, automatic=True)
        assert len(rig.calls) == 3
        assert rig.calls[1][1] == 30.25
        assert repo.progress(rig.identity)["state"] == "Completed"
        assert repo.results()[1] == 2
        assert "automatically_resumed" in {e["event"] for e in night.get(rig.identity)["events"]}
        rig.store = previous
    finally:
        reopened.close()


@pytest.mark.parametrize("platform", ["Roblox", "X"])
@pytest.mark.parametrize("committed", [False, True])
def test_crash_between_request_and_commit_requires_review_when_uncertain(rigs, platform, committed):
    rig = rigs(platform)
    rig.repo.start(rig.identity)
    rig.night.begin(rig.identity, clock=rig.clock.now)
    task = rig.repo.claim_next(rig.identity) if platform == "Roblox" else rig.repo.claim(rig.identity)
    if committed:
        if platform == "Roblox":
            rig.repo.finish_check(
                task,
                result_for(task["username"], roblox_response(), checked_at=rig.clock.now().isoformat()),
                response_finished_at=rig.clock.now(),
            )
        else:
            result = x.result_for(task["username"], x_response(), checked_at=rig.clock.now().isoformat())
            rig.repo.finish(task, result)
    rig.repo.recover()
    if committed:
        assert rig.night.startup_candidates(rig.repo) == [rig.identity]
        execute(rig, automatic=True)
        assert [name for name, _ in rig.calls] == ["NightTwo"]
    else:
        assert rig.night.startup_candidates(rig.repo) == []
        assert rig.night.get(rig.identity)["state"] == "Review"
        assert rig.repo.results()[0][0]["classification"] == "Unknown"
    assert len(rig.repo.history("NightOne")) == 1


@pytest.mark.parametrize("platform", ["Roblox", "X"])
@pytest.mark.parametrize("status,headers", [(429, {"retry-after": "600"}), (429, {}), (403, {}), (401, {})])
def test_restrictions_never_automatically_release_or_retry(rigs, platform, status, headers):
    rig = rigs(platform)
    protocol = roblox_protocol if platform == "Roblox" else x_protocol
    execute(rig, [protocol.Response(status, b"", headers)])
    assert len(rig.calls) == 1
    assert rig.night.get(rig.identity)["state"] == "Review"
    assert rig.night.startup_candidates(rig.repo) == []
    if headers:
        assert rig.repo.control()["not_before"] >= (rig.clock.now() + timedelta(seconds=600)).isoformat()
    if platform == "X":
        assert rig.repo.control()["blocked"]


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_longer_persisted_server_deadline_overrides_retry_interval(rigs, platform):
    rig = rigs(platform)

    def failed_then_deadline(value, *args):
        if not rig.calls:
            with rig.store.transaction() as db:
                control = rig.repo.control()
                control["not_before"] = (rig.clock.now() + timedelta(seconds=600)).isoformat()
                db.execute(
                    f"UPDATE {'roblox' if platform == 'Roblox' else 'x'}_control SET value=? WHERE id=1",
                    (json.dumps(control),),
                )
        rig.calls.append(rig.clock.seconds)
        if len(rig.calls) == 1:
            raise TimeoutError
        return roblox_response() if platform == "Roblox" else x_response()

    cls = RobloxRunner if platform == "Roblox" else XRunner
    cls(rig.repo, fetcher=failed_then_deadline, clock=rig.clock.now, wait=rig.clock.wait).run(rig.identity)
    assert rig.calls[1] >= 600


def test_transport_evidence_is_narrow_and_diagnostics_are_bounded(rigs):
    assert transient_exception(urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, "temporary")))
    assert transient_exception(OSError(errno.ENETUNREACH, "unreachable"))
    assert not transient_exception(ssl.SSLCertVerificationError("certificate"))
    assert not transient_exception(urllib.error.URLError("opaque"))
    assert not transient_exception(ValueError("programming error"))
    assert not transient_exception(PermissionError("configuration"))
    for response in (
        transport_failure(x_protocol.Response(429, error="timeout"), TimeoutError()),
        transport_failure(x_protocol.Response(body=b"partial", error="timeout"), TimeoutError()),
        transport_failure(
            x_protocol.Response(headers={"retry-after": "60"}, error="timeout"), TimeoutError()
        ),
    ):
        assert not retryable_response(response)
    rig = rigs("X")
    for i in range(EVENT_LIMIT + 10):
        rig.night.enable(rig.identity, bool(i % 2))
    assert len(rig.night.get(rig.identity)["events"]) == EVENT_LIMIT
    assert rig.night.get(rig.identity)["event_count"] == EVENT_LIMIT + 11


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_explicit_other_job_supersedes_abandoned_automatic_resume(rigs, platform):
    rig = rigs(platform)
    execute(rig, [TimeoutError()], wait=lambda _: True)
    first = rig.identity
    assert rig.night.get(first)["auto_resume"]
    rig.identity = (
        rig.repo.create_run(["AnotherNight"])
        if platform == "Roblox"
        else rig.repo.prepare(x.input_spec("AnotherNight"))
    )
    rig.night.enable(rig.identity, True)
    execute(rig)
    assert rig.night.get(first)["state"] == "Superseded"
    assert not rig.night.get(first)["auto_resume"]
    assert not rig.night.startup_candidates(rig.repo)


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_enabling_historical_job_alone_does_not_make_it_startup_eligible(rigs, platform):
    rig = rigs(platform)
    assert rig.night.startup_candidates(rig.repo) == []
    rig.night.enable(rig.identity, False)
    execute(rig, [TimeoutError()])
    assert len(rig.calls) == (2 if platform == "Roblox" else 1)
    assert not rig.night.startup_candidates(rig.repo)


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_actual_process_termination_recovers_uncertain_check_without_resending(tmp_path, platform):
    path = tmp_path / "crash.db"
    script = """
import os,sys
from username_finder.storage import Store
from username_finder.roblox_storage import RobloxStore
from username_finder.x_storage import XStore
from username_finder.overnight import OvernightStore
from username_finder import x
store=Store(sys.argv[1]);platform=sys.argv[2]
repo=RobloxStore(store) if platform=='Roblox' else XStore(store)
names=['NightOne','NightTwo']
identity=repo.create_run(names) if platform=='Roblox' else repo.prepare(x.input_spec('\\n'.join(names)))
night=OvernightStore(store,platform);night.enable(identity,True)
repo.start(identity);night.begin(identity)
task=repo.claim_next(identity) if platform=='Roblox' else repo.claim(identity)
print(identity,flush=True)
os._exit(17)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), platform], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 17, result.stderr
    identity = result.stdout.strip()
    store = Store(path)
    try:
        repo = RobloxStore(store) if platform == "Roblox" else XStore(store)
        repo.recover()
        night = OvernightStore(store, platform)
        assert not night.startup_candidates(repo)
        assert night.get(identity)["state"] == "Review"
        assert repo.results()[0][0]["classification"] == "Unknown"
        assert len(repo.history("NightOne")) == 1
    finally:
        store.close()


@pytest.mark.parametrize("platform", ["Roblox", "X"])
@pytest.mark.parametrize("partial", [False, True])
def test_transport_marks_only_pre_response_failures_for_automatic_retry(monkeypatch, platform, partial):
    class Stream:
        status = code = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            raise ConnectionResetError

        read1 = read

    class Opener:
        def open(self, *args, **kwargs):
            if partial:
                return Stream()
            raise urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, "temporary dns"))

    protocol = roblox_protocol if platform == "Roblox" else x_protocol
    monkeypatch.setattr(protocol.urllib.request, "build_opener", lambda *args: Opener())
    response = (
        protocol.fetch(protocol.request_url("NightOne"))
        if platform == "Roblox"
        else protocol.fetch("NightOne", 15)
    )
    assert retryable_response(response) is not partial
