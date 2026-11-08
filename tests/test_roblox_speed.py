import threading
from datetime import UTC, datetime, timedelta

import pytest
from test_roblox import response

from username_finder.roblox import result_for
from username_finder.roblox_runner import RobloxRunner
from username_finder.roblox_storage import CHECK_INTERVALS, RobloxStore
from username_finder.storage import Store


class Clock:
    def __init__(self):
        self.at = datetime(2030, 1, 1, tzinfo=UTC)
        self.waits = []
        self.requests = []
        self.responses = []
        self.active = False

    def now(self):
        return self.at

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)

    def wait(self, seconds):
        self.waits.append(seconds)
        self.advance(seconds)
        return False

    def fetch(self, url):
        assert not self.active
        self.active = True
        self.requests.append((url, self.at))
        self.advance(0.125)
        self.responses.append(self.at)
        self.active = False
        return response()

    def run(self, repo, identity, **kwargs):
        runner = RobloxRunner(repo, fetcher=self.fetch, wait=self.wait, clock=self.now, **kwargs)
        runner.run(identity)
        return runner


@pytest.fixture
def repo(tmp_path):
    store = Store(tmp_path / "speed.db")
    yield RobloxStore(store)
    store.close()


@pytest.mark.parametrize("seconds", CHECK_INTERVALS)
def test_each_speed_measures_from_response_completion_sequentially(repo, seconds):
    repo.set_preferred_interval(seconds)
    identity = repo.create_run(["FirstName", "SecondName", "ThirdName"])
    clock = Clock()
    # Work after a response consumes part of the interval, not an additional interval.
    clock.run(repo, identity, updated=lambda: clock.advance(0.1))
    expected = max(seconds, 0.1)
    assert [
        (request[1] - finished).total_seconds()
        for request, finished in zip(clock.requests[1:], clock.responses[:-1], strict=True)
    ] == [expected, expected]
    assert clock.waits == pytest.approx([seconds - 0.2] * 2 if seconds else [])
    assert repo.progress(identity)["state"] == "Completed"
    assert repo.progress(identity)["interval_seconds"] == seconds
    assert len({url for url, _ in clock.requests}) == 3


def test_default_and_saved_preference_survive_restart(tmp_path):
    path = tmp_path / "saved.db"
    store = Store(path)
    repo = RobloxStore(store)
    assert repo.preferred_interval() == 5
    original = repo.create_run(["OldName"])
    repo.set_preferred_interval(0.5)
    store.close()
    reopened = Store(path)
    try:
        repo = RobloxStore(reopened)
        assert repo.preferred_interval() == 0.5
        assert repo.progress(original)["interval_seconds"] == 5
        assert repo.progress(repo.create_run(["NewName"]))["interval_seconds"] == 0.5
    finally:
        reopened.close()


@pytest.mark.parametrize("value", [True, None, "0", -1, 0.1, 10, float("nan"), float("inf")])
def test_unsupported_preferences_cannot_change_pacing(repo, value):
    with pytest.raises(ValueError):
        repo.set_preferred_interval(value)
    assert repo.preferred_interval() == 5


def test_legacy_runs_migrate_to_five_seconds_without_losing_queue(repo):
    identity = repo.create_run(["OldName", "NextName"])
    with repo.store.transaction() as db:
        db.execute("ALTER TABLE roblox_runs DROP COLUMN interval_seconds")
    path = repo.store.path
    repo.store.close()
    reopened = Store(path)
    try:
        saved = RobloxStore(reopened).progress(identity)
        assert saved["interval_seconds"] == 5
        assert saved["counts"] == {"Pending": 2}
        assert saved["state"] == "Ready"
    finally:
        reopened.close()


def test_cancel_during_delay_and_resume_keep_operation_speed(repo):
    clock = Clock()
    repo.set_preferred_interval(2)
    identity = repo.create_run(["FirstName", "SecondName"])
    runner = RobloxRunner(repo, fetcher=clock.fetch, clock=clock.now)

    def cancel_wait(seconds):
        assert seconds == 2
        clock.advance(0.5)
        runner.cancel.set()
        return True

    runner.wait = cancel_wait
    runner.run(identity)
    assert len(clock.requests) == 1
    assert repo.progress(identity)["state"] == "Paused"
    repo.set_preferred_interval(0)
    repo.recover()
    clock.run(repo, identity)
    assert clock.waits == [1.5]
    assert repo.progress(identity)["interval_seconds"] == 2
    assert repo.progress(identity)["state"] == "Completed"
    assert len({url for url, _ in clock.requests}) == 2
    assert (clock.requests[1][1] - clock.responses[0]).total_seconds() == 2


def test_default_event_wait_is_responsive_to_stop(repo):
    identity = repo.create_run(["FirstName", "SecondName"])
    entered = threading.Event()
    calls = []
    runner = RobloxRunner(repo, fetcher=lambda url: (calls.append(url), response())[1])

    def wait(seconds):
        entered.set()
        return runner.cancel.wait(seconds)

    runner.wait = wait
    worker = threading.Thread(target=runner.run, args=(identity,))
    worker.start()
    try:
        assert entered.wait(2)
        runner.cancel.set()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert len(calls) == 1
        assert repo.progress(identity)["state"] == "Paused"
    finally:
        runner.cancel.set()
        worker.join(timeout=2)


@pytest.mark.parametrize("seconds", CHECK_INTERVALS)
def test_retry_after_overrides_all_speeds_and_stops_until_explicit_resume(repo, seconds):
    repo.set_preferred_interval(seconds)
    clock = Clock()
    identity = repo.create_run(["FirstName", "SecondName"])

    def restricted(url):
        clock.fetch(url)
        return response(status=429, headers={"retry-after": "120"})

    RobloxRunner(repo, fetcher=restricted, wait=clock.wait, clock=clock.now).run(identity)
    assert len(clock.requests) == 1 and not clock.waits
    assert repo.progress(identity)["state"] == "Restricted"
    clock.run(repo, identity)
    assert clock.waits == [120]
    assert len(clock.requests) == 2
    assert (clock.requests[1][1] - clock.responses[0]).total_seconds() == 120


def test_stored_longer_deadline_is_not_shortened_by_new_restriction(repo):
    repo.set_preferred_interval(0)
    clock = Clock()
    identity = repo.create_run(["SomeName"])
    repo.start(identity)
    task = repo.claim_next(identity)
    longer = (clock.now() + timedelta(seconds=300)).isoformat()
    with repo.store.transaction() as db:
        control = repo._control(db)
        control["not_before"] = longer
        repo._save_control(db, control)
    repo.finish_check(
        task,
        result_for("SomeName", response()),
        response_finished_at=clock.now(),
        control_update={"not_before": (clock.now() + timedelta(seconds=120)).isoformat()},
    )
    assert repo.control()["not_before"] == longer


def test_exhausted_quota_wait_has_priority_at_zero_speed(repo):
    repo.set_preferred_interval(0)
    clock = Clock()
    identity = repo.create_run(["FirstName", "SecondName"])

    def fetch(url):
        clock.fetch(url)
        return response(headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "60"})

    RobloxRunner(repo, fetcher=fetch, wait=clock.wait, clock=clock.now).run(identity)
    assert clock.waits == [60]
    assert len(clock.requests) == 2


@pytest.mark.parametrize(
    "kwargs", [{"status": 401}, {"status": 403}, {"challenge": True}, {"body": b"malformed"}]
)
def test_zero_speed_preserves_existing_restriction_stops(repo, kwargs):
    repo.set_preferred_interval(0)
    clock = Clock()
    identity = repo.create_run(["FirstName", "SecondName", "ThirdName"])
    RobloxRunner(repo, fetcher=lambda _: response(**kwargs), clock=clock.now).run(identity)
    assert repo.progress(identity)["state"] == "Restricted"
    assert repo.control()["blocked"]
    assert repo.progress(identity)["counts"]["Done"] == (2 if "body" in kwargs else 1)
