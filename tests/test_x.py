import ast
import csv
import importlib.util
import json
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from username_finder import x
from username_finder import x_protocol as protocol
from username_finder.roblox import result_for as roblox_result
from username_finder.roblox_protocol import Response as RobloxResponse
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store
from username_finder.x_runner import XRunner
from username_finder.x_storage import XStore


def response(valid=False, reason="taken", **fields):
    # Ordinary workload fixtures use a supported negative, not disputed availability.
    return protocol.Response(
        200,
        json.dumps(dict(valid=valid, reason=reason, **fields)).encode(),
        {"content-type": "application/json"},
    )


class Clock:
    def __init__(self):
        self.seconds = 0
        self.waits = []
        self.calls = []

    def now(self):
        return datetime(2026, 11, 7, tzinfo=UTC) + timedelta(seconds=self.seconds)

    def wait(self, seconds):
        self.waits.append(seconds)
        self.seconds += seconds
        return False

    def fetch(self, username, timeout):
        self.calls.append((username, self.seconds))
        self.seconds += 0.125
        return response()


@pytest.fixture
def repo(tmp_path):
    store = Store(tmp_path / "test.db")
    yield XStore(store)
    store.close()


def run(repo, spec=None, *, identity=None, clock=None, fetcher=None, wait=None):
    clock = clock or Clock()
    identity = identity or repo.prepare(spec)
    runner = XRunner(
        repo,
        clock=clock.now,
        monotonic=lambda: clock.seconds,
        fetcher=fetcher or clock.fetch,
        wait=wait or clock.wait,
    )
    runner.run(identity)
    return identity, clock


def test_protocol_is_independent_snapshot_and_matches_all_historical_evidence():
    root = Path(__file__).resolve().parents[1]
    source = root / "experiments/x-username-poc/x_poc.py"
    spec = importlib.util.spec_from_file_location("x_reference_poc", source)
    reference = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reference
    spec.loader.exec_module(reference)
    tree = ast.parse(source.read_text(encoding="utf-8"))
    integrated = ast.parse(Path(protocol.__file__).read_text(encoding="utf-8"))
    for node in integrated.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name not in ("fetch", "parse"):
            original = next(n for n in tree.body if isinstance(n, type(node)) and n.name == node.name)
            assert ast.dump(node, include_attributes=False) == ast.dump(original, include_attributes=False)
    # Immutable CSV is read only; no research DB is opened for writing or exported.
    with (source.parent / "output/results.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            sample = protocol.Response(
                int(row["http_status"]), row["raw_response_redacted"].encode(), json.loads(row["headers"])
            )
            current = protocol.parse(sample, row["username"])
            original = reference.parse(sample, row["username"])
            if original[0] == "Available":
                assert current[:2] == ("Unknown", "api_available_does_not_establish_claimability")
                assert current[2] == original[2]
            else:
                assert current == original


@pytest.mark.parametrize("length,valid", [(4, False), (5, True), (14, True), (15, True), (16, False)])
def test_syntax_boundaries(length, valid):
    assert protocol.validate_syntax("a" * length)["valid"] is valid


@pytest.mark.parametrize(
    "name,valid",
    [
        ("_name", True),
        ("name_", True),
        ("admin", True),
        ("xname", True),
        ("bad.name", False),
        ("bad name", False),
        ("bad-name", False),
        ("@name", False),
    ],
)
def test_syntax_characters_not_availability(name, valid):
    assert protocol.validate_syntax(name)["valid"] is valid
    assert protocol.parse(response(False, "custom_message"), name)[0] == "Unknown"


@pytest.mark.parametrize(
    "reason,name,expected,fields",
    [
        ("available", "valid_name", "Unknown", {}),
        ("taken", "valid_name", "Taken", {}),
        ("is_banned_word", "admin", "Reserved", {}),
        ("contains_banned_word", "xadmin", "Reserved", {}),
        ("improper_format", "bad.name", "Invalid", {}),
        (
            "invalid_username",
            "abcd",
            "Invalid",
            {"msg": "Username is too short", "desc": "Your username must be longer than 4 characters."},
        ),
        (
            "invalid_username",
            "a" * 16,
            "Invalid",
            {"msg": "Username is too long", "desc": "Your username must be shorter than 15 characters."},
        ),
        ("invalid_username", "valid_name", "Unknown", {}),
        ("custom_message", "valid_name", "Unknown", {}),
    ],
)
def test_classifications(reason, name, expected, fields):
    result = x.result_for(name, response(reason == "available", reason, **fields))
    assert result["classification"] == expected
    assert result["api_reason"] == reason
    assert result["preliminary"] and not result["independently_verified"]


def test_hzwu9_is_not_hardcoded_or_claimed_verified():
    original = response(True, "available", msg="Available!", desc="Available!")
    result = x.result_for("hzwu9", original)
    assert result["classification"] == "Unknown"
    assert result["response_data"] == original.body.decode()
    assert result["preliminary"] and not result["independently_verified"]
    assert "hzwu9" in result["warning"]
    assert "غير رسمية" in result["warning"]


def test_generation_import_dedup_skip_and_invalid_feedback(repo, tmp_path):
    config = x.generation_spec(5, 100)
    names = list(x.candidates({"generation": config}))
    assert len(set(names)) == 100
    assert all(protocol.validate_syntax(n)["valid"] for n in names)
    assert names == list(x.candidates({"generation": config}))
    txt = tmp_path / "names.txt"
    txt.write_text("valid_name\nVALID_NAME\ninvalid-name\n", encoding="utf-8-sig")
    csv_path = tmp_path / "names.csv"
    csv_path.write_text("username,notes\nfreshname,fixture\n", encoding="utf-8-sig")
    identity, clock = run(repo, x.input_spec("_name\n", [txt, csv_path]))
    assert len(clock.calls) == 3
    progress = repo.progress(identity)
    assert progress["duplicates"] == 1 and progress["invalid"] == 1
    assert progress["state"] == "Completed"
    identity = repo.prepare(x.input_spec("valid_name\nfreshname\n_name"))
    assert repo.progress(identity)["skipped"] == 3 and repo.progress(identity)["total"] == 0


def test_bad_csv_and_empty_files_create_no_requests(repo, tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text('username,notes\n"unterminated', encoding="utf-8")
    with pytest.raises(ValueError):
        repo.prepare(x.input_spec(files=[path]))
    assert repo.runs()[0]["state"] == "PreparationError"
    path.write_text("", encoding="utf-8")
    identity = repo.prepare(x.input_spec(files=[path]))
    assert repo.progress(identity)["total"] == 0


def test_platform_isolation_persistence_and_no_roblox_migration(repo):
    roblox = RobloxStore(repo.store)
    identity = roblox.create_run(["valid_name"])
    assert roblox.start(identity)
    task = roblox.claim_next(identity)
    roblox.finish_check(
        task,
        roblox_result(
            "valid_name",
            RobloxResponse(200, b'{"code":1,"message":"Taken"}', {"content-type": "application/json"}),
        ),
    )
    roblox.end(identity, "Completed")
    before = [tuple(r) for r in repo.store.db.execute("SELECT * FROM roblox_checks")]
    run(repo, x.input_spec("valid_name"))
    assert repo.results()[0][0]["classification"] == "Taken"
    assert roblox.results()[0][0]["classification"] == "Taken"
    assert [tuple(r) for r in repo.store.db.execute("SELECT * FROM roblox_checks")] == before
    path = repo.store.path
    repo.store.close()
    reopened = Store(path)
    repo.store = reopened
    assert XStore(reopened).history("VALID_NAME")[0]["classification"] == "Taken"
    assert RobloxStore(reopened).history("valid_name")[0]["classification"] == "Taken"


def test_selected_all_rechecks_changes_filter_and_exports(repo, tmp_path):
    run(repo, x.input_spec("valid_name\nfreshname"))
    identity = repo.prepare(x.input_spec("valid_name\nVALID_NAME"), skip=False)
    run(repo, identity=identity, fetcher=lambda *a: response(False, "is_banned_word"))
    rows, total = repo.results("Reserved", "valid")
    assert total == 1 and rows[0]["changed"] == 1
    assert rows[0]["previous_classification"] == "Taken"
    assert len(repo.history("valid_name")) == 2
    path = tmp_path / "results.csv"
    assert repo.export(path, "Taken") == 1
    with path.open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["preliminary"] == "True" and row["independently_verified"] == "False"
    assert "hzwu9" in row["warning"]
    assert repo.export(path, history=True) == 3
    with pytest.raises(ValueError):
        repo.export(repo.store.path)
    identity = repo.prepare(kind="recheck_all")
    assert repo.progress(identity)["total"] == 2
    run(repo, identity=identity)
    assert len(repo.history("valid_name")) == 3


def test_pacing_resume_preferences_and_cancellation(repo):
    repo.set_interval(10)
    identity = repo.prepare(x.input_spec("valid_name\nfreshname\nthirdname"))
    repo.set_interval(30)
    clock = Clock()
    run(repo, identity=identity, clock=clock, wait=lambda _: True)
    assert len(clock.calls) == 1 and repo.progress(identity)["state"] == "Paused"
    run(repo, identity=identity, clock=clock)
    assert clock.waits == [10, 10]
    assert len(clock.calls) == 3 and len({n for n, _ in clock.calls}) == 3
    assert repo.progress(identity)["interval_seconds"] == 10
    assert repo.preferred_interval() == 30
    for value in (-1, 0.25, 6, 31, True, "5", None, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            repo.set_interval(value)


@pytest.mark.parametrize("seconds", [30, 15, 10, 5, 4, 3, 2, 1.5, 1, 0.5, 0])
def test_each_speed_has_exact_post_response_timing_without_final_wait(repo, seconds):
    assert x.interval(seconds) == seconds
    repo.set_interval(seconds)
    identity = repo.prepare(x.input_spec("first_name\nsecond_name\nthird_name"))
    clock = Clock()
    active = False
    thread_ids = []

    def fetch(name, timeout):
        nonlocal active
        assert not active
        active = True
        thread_ids.append(threading.get_ident())
        try:
            return clock.fetch(name, timeout)
        finally:
            active = False

    run(repo, identity=identity, clock=clock, fetcher=fetch)
    assert clock.calls == [
        ("first_name", 0),
        ("second_name", seconds + 0.125),
        ("third_name", 2 * seconds + 0.25),
    ]
    assert clock.waits == ([seconds, seconds] if seconds else [])
    assert clock.seconds == 2 * seconds + 0.375
    assert thread_ids == [threading.get_ident()] * 3
    assert repo.progress(identity)["state"] == "Completed"


@pytest.mark.parametrize("seconds", [30, 15, 10, 5, 4, 3, 2, 1.5, 1, 0.5, 0])
def test_speed_and_frozen_job_survive_database_restart(repo, seconds):
    assert repo.preferred_interval() == 5
    old_job = repo.prepare(x.input_spec("old_name"))
    repo.set_interval(seconds)
    identity = repo.prepare(x.input_spec("first_name\nsecond_name"))
    clock = Clock()

    def fetch(name, timeout):
        result = clock.fetch(name, timeout)
        worker.cancel.set()
        return result

    worker = XRunner(repo, fetcher=fetch, clock=clock.now, monotonic=lambda: clock.seconds, wait=clock.wait)
    worker.run(identity)
    assert repo.progress(identity)["completed"] == 1
    original_history = repo.history("first_name")
    database = repo.store.path
    repo.store.close()
    repo.store = Store(database)
    reopened = XStore(repo.store)
    reopened.recover()
    assert reopened.preferred_interval() == seconds
    assert reopened.progress(identity)["interval_seconds"] == seconds
    assert reopened.progress(old_job)["interval_seconds"] == 5
    reopened.set_interval(30 if seconds != 30 else 5)
    run(reopened, identity=identity, clock=clock)
    assert reopened.progress(identity)["interval_seconds"] == seconds
    assert [name for name, _ in clock.calls] == ["first_name", "second_name"]
    assert clock.waits == ([seconds] if seconds else [])
    assert reopened.history("first_name") == original_history


@pytest.mark.parametrize("seconds", [4, 3, 2, 1.5, 1, 0.5])
def test_stop_during_new_pacing_wait_retains_pending_and_completed_results(repo, seconds):
    repo.set_interval(seconds)
    identity = repo.prepare(x.input_spec("first_name\nsecond_name"))
    clock = Clock()
    waits = []

    def cancelled_wait(delay):
        waits.append(delay)
        worker.cancel.set()
        return True

    worker = XRunner(
        repo, fetcher=clock.fetch, clock=clock.now, monotonic=lambda: clock.seconds, wait=cancelled_wait
    )
    worker.run(identity)
    assert waits == [seconds]
    assert len(clock.calls) == 1
    assert repo.progress(identity)["state"] == "Paused"
    assert repo.progress(identity)["completed"] == 1
    assert repo.progress(identity)["pending"] == 1
    assert len(repo.history("first_name")) == 1


@pytest.mark.parametrize("seconds", [0, 0.5, 1.5])
def test_fast_speeds_respect_prior_deadline_and_retry_after(repo, seconds):
    repo.set_interval(seconds)
    clock = Clock()
    with repo.store.transaction() as db:
        repo._control(db, not_before=(clock.now() + timedelta(seconds=120)).isoformat())
    identity = repo.prepare(x.input_spec("first_name\nsecond_name"))
    run(
        repo,
        identity=identity,
        clock=clock,
        fetcher=lambda *args: protocol.Response(429, b"{}", {"retry-after": "300"}),
    )
    assert clock.waits == [120]
    assert repo.control()["not_before"] == (clock.now() + timedelta(seconds=300)).isoformat()
    assert repo.control()["blocked"]
    assert repo.progress(identity)["completed"] == 1 and repo.progress(identity)["pending"] == 1


@pytest.mark.parametrize(
    "sample",
    [
        protocol.Response(401),
        protocol.Response(403),
        protocol.Response(429),
        protocol.Response(error="TimeoutError", complete=False),
        response(True, "taken"),
        response(False, "custom_message"),
        protocol.Response(200, b"invalid json"),
    ],
)
def test_zero_speed_still_stops_on_first_failure(repo, sample):
    repo.set_interval(0)
    identity = repo.prepare(x.input_spec("first_name\nsecond_name"))
    run(repo, identity=identity, fetcher=lambda *args: sample)
    assert repo.progress(identity)["completed"] == 1
    assert repo.progress(identity)["pending"] == 1
    assert repo.control()["blocked"]


@pytest.mark.parametrize(
    "sample",
    [
        protocol.Response(401),
        protocol.Response(403),
        protocol.Response(429),
        protocol.Response(500),
        protocol.Response(error="TimeoutError", complete=False),
        protocol.Response(200, b"malformed", {"content-type": "application/json"}),
        response(True, "taken"),
        response(False, "custom_message"),
        protocol.Response(200, response().body, {"content-type": "application/json", "retry-after": "120"}),
    ],
)
def test_immediate_durable_stop_no_automatic_retry(repo, sample):
    identity = repo.prepare(x.input_spec("valid_name\nfreshname"))
    run(repo, identity=identity, fetcher=lambda *a: sample)
    assert repo.progress(identity)["completed"] == 1
    assert repo.progress(identity)["pending"] == 1
    assert repo.control()["blocked"]
    with pytest.raises(ValueError):
        run(repo, identity=identity, fetcher=lambda *a: pytest.fail("Must not retry"))


def test_retry_after_and_existing_longer_deadline_take_precedence(repo):
    clock = Clock()
    identity = repo.prepare(x.input_spec("valid_name"))
    sample = protocol.Response(429, b"{}", {"retry-after": "120"})
    run(repo, identity=identity, clock=clock, fetcher=lambda *a: sample)
    assert repo.control()["not_before"] == (clock.now() + timedelta(seconds=120)).isoformat()


@pytest.mark.parametrize("headers", [{}, {"retry-after": "120"}])
def test_reproduce_429_latch_survives_restart_and_elapsed_local_deadline(repo, headers):
    clock = Clock()
    identity = repo.prepare(x.input_spec("valid_name\nfreshname"))
    run(repo, identity=identity, clock=clock, fetcher=lambda *a: protocol.Response(429, b"", headers))
    history = repo.history("valid_name")
    saved = repo.progress(identity)
    path = repo.store.path
    repo.store.close()
    reopened_store = Store(path)
    try:
        reopened = XStore(reopened_store)
        reopened.recover()
        clock.seconds += 86400
        new = reopened.prepare({"generation": x.generation_range_spec(8, 8, 10)})
        assert reopened.progress(new)["total"] == 10
        with pytest.raises(ValueError):
            run(reopened, identity=new, clock=clock, fetcher=lambda *a: pytest.fail("No retries"))
        assert reopened.control()["blocked"] == "unexpected_http_status:429"
        assert reopened.history("valid_name") == history
        assert reopened.progress(identity) == saved
    finally:
        reopened_store.close()


def test_inflight_crash_never_resends_and_blocks_provider(repo):
    identity = repo.prepare(x.input_spec("valid_name\nfreshname"))
    repo.start(identity)
    repo.claim(identity)
    repo.recover()
    assert repo.progress(identity)["completed"] == 1 and repo.progress(identity)["pending"] == 1
    evidence = json.loads(repo.history("valid_name")[0]["evidence"])
    assert evidence["delivery_uncertain"] and evidence["classification"] == "Unknown"
    with pytest.raises(ValueError):
        run(repo, identity=identity, fetcher=lambda *a: pytest.fail("Must not resend"))


def test_preparation_cancellation_resume_is_deduplicated(repo):
    cancel = threading.Event()
    cancel.set()
    identity = repo.prepare(x.input_spec("valid_name\nvalid_name\nfreshname"), cancel=cancel)
    assert repo.progress(identity)["state"] == "PreparationPaused"
    repo.prepare(run_id=identity)
    assert repo.progress(identity)["total"] == 2 and repo.progress(identity)["duplicates"] == 1


def test_default_and_preferences_survive_restart_with_frozen_run_interval(repo):
    assert repo.preferred_interval() == 5
    repo.set_interval(15)
    identity = repo.prepare(x.input_spec("valid_name"))
    reopened = XStore(repo.store)
    assert reopened.preferred_interval() == 15
    reopened.set_interval(30)
    assert reopened.progress(identity)["interval_seconds"] == 15


def test_prior_server_deadline_is_not_shortened_or_overridden(repo):
    clock = Clock()
    with repo.store.transaction() as db:
        repo._control(db, not_before=(clock.now() + timedelta(seconds=120)).isoformat())
    identity = repo.prepare(x.input_spec("valid_name\nfreshname"))
    run(repo, identity=identity, clock=clock)
    assert clock.waits == [120, 5]
    assert clock.calls[0][1] == 120


def test_x_initialization_does_not_change_existing_schema_or_rows(repo):
    db = repo.store.db
    before = [
        tuple(r)
        for r in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE name LIKE 'roblox_%' "
            "OR name IN('jobs','tasks','attempts') ORDER BY name"
        )
    ]
    version = db.execute("PRAGMA user_version").fetchone()[0]
    XStore(repo.store)
    assert [
        tuple(r)
        for r in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE name LIKE 'roblox_%' "
            "OR name IN('jobs','tasks','attempts') ORDER BY name"
        )
    ] == before
    assert db.execute("PRAGMA user_version").fetchone()[0] == version
