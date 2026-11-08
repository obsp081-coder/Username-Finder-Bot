import csv
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from username_finder import roblox_protocol as protocol
from username_finder.roblox import generate, prepare_inputs, result_for, retry_not_before, syntax_error
from username_finder.roblox_runner import RobloxRunner
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store


def response(code=0, *, status=200, headers=None, body=None, **kwargs):
    return protocol.Response(
        status,
        body
        if body is not None
        else json.dumps(
            {"code": code, "message": "Username is valid" if code == 0 else "Validation rejection"}
        ).encode(),
        headers or {"content-type": "application/json"},
        elapsed=0.125,
        **kwargs,
    )


@pytest.fixture
def repository(tmp_path):
    store = Store(tmp_path / "history.db")
    yield RobloxStore(store)
    store.close()


def run(repository, names=(), *, code=0, skip=True, kind="normal", fetcher=None, wait=None):
    identity = repository.create_run(names, skip=skip, kind=kind)
    runner = RobloxRunner(
        repository, fetcher=fetcher or (lambda _: response(code)), wait=wait or (lambda _: False)
    )
    runner.run(identity)
    return identity


def csv_file(path, rows):
    fields = [
        "username",
        "request_at_utc",
        "http_status",
        "response_data",
        "api_code",
        "preliminary_classification",
        "raw_complete",
        "attempted",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for name, code, at in rows:
            writer.writerow(
                {
                    "username": name,
                    "request_at_utc": at,
                    "http_status": 200,
                    "response_data": response(code).body.decode(),
                    "api_code": code,
                    "preliminary_classification": protocol.CODES[code][0],
                    "raw_complete": True,
                    "attempted": True,
                }
            )
    return path


@pytest.mark.parametrize(
    "code,expected",
    [
        (0, "Available"),
        (1, "Taken"),
        (2, "Invalid"),
        (3, "Invalid"),
        (4, "Invalid"),
        (5, "Invalid"),
        (6, "Invalid"),
        (7, "Invalid"),
        (10, "Invalid"),
        (12, "Reserved"),
    ],
)
def test_documented_classification_is_preliminary(code, expected):
    result = result_for("candidate", response(code))
    assert result["classification"] == expected
    assert result["api_code"] == code and result["preliminary"]
    assert result["independently_verified_classification"] == "Unknown"
    params = parse_qs(urlsplit(result["request_url"]).query)
    assert params == {"Username": ["candidate"], "Birthday": ["2000-01-01T00:00:00Z"], "Context": ["0"]}


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"<html>challenge</html>",
        b"[]",
        b"{}",
        b'{"code":true,"message":"Username is valid"}',
        b'{"code":"0","message":"Username is valid"}',
        b'{"code":0,"code":1,"message":"Username is valid"}',
        b'{"code":0,"message":"Username is already in use"}',
        b'{"code":0,"message":"string"}',
        b'{"code":99,"message":"Username is valid"}',
        b'{"errors":[{"code":0}]}',
        b'{"code":0,"message":"CAPTCHA required"}',
    ],
)
def test_ambiguous_responses_never_available(body):
    assert result_for("candidate", response(body=body))["classification"] == "Unknown"


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 408, 429, 500, 503])
def test_http_status_does_not_prove_availability(status):
    assert result_for("candidate", response(status=status))["classification"] == "Unknown"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"challenge": True},
        {"error": "timeout", "complete": False},
        {"complete": False},
        {"headers": {"content-type": "text/html"}},
        {"headers": {"content-type": "application/json", "retry-after": "60"}},
    ],
)
def test_error_or_challenge_is_unknown(kwargs):
    assert result_for("candidate", response(**kwargs))["classification"] == "Unknown"


@pytest.mark.parametrize("name", ["Abc", "123", "1abc", "a_b", "a" * 20])
def test_roblox_local_syntax(name):
    assert syntax_error(name) is None


@pytest.mark.parametrize("name", ["a", "ab", "a" * 21, "a-b", "a.b", "a b", "_abc", "abc_", "a_b_c", "أحمد"])
def test_local_invalid_names(name):
    assert syntax_error(name)


def test_generation_is_bounded_and_unique():
    names = tuple(generate(3, 10_000))
    assert len(names) == len(set(names)) == 10_000
    assert all(len(n) == 3 and not syntax_error(n) for n in names)
    for args in [(2, 1), (21, 1), (True, 1), (3, 0), (3, 26 * 36**2 + 1)]:
        with pytest.raises(ValueError):
            generate(*args)


def test_manual_txt_csv_casefold_dedup_and_safe_headers(tmp_path):
    txt = tmp_path / "names.txt"
    txt.write_text("\ufeffFirstName\nsecondName\n\ninvalid-name\n", encoding="utf-8")
    csv_path = tmp_path / "names.csv"
    csv_path.write_text('username,note\nFIRSTNAME,"quoted, field"\nthirdName,ok\n', encoding="utf-8")
    prepared = prepare_inputs("firstName\nFOURTHname", (str(txt), str(csv_path), str(txt)))
    assert prepared.names == ("firstName", "FOURTHname", "secondName", "thirdName")
    assert prepared.duplicates == 2 and len(prepared.invalid) == 1 and prepared.blank == 1
    csv_path.write_text("name,note\nhello,world\n", encoding="utf-8")
    with pytest.raises(ValueError, match="username"):
        prepare_inputs(files=[csv_path])
    csv_path.write_text('username\n"unfinished', encoding="utf-8")
    with pytest.raises(ValueError):
        prepare_inputs(files=[csv_path])


def test_empty_bad_encoding_and_size_fail_clearly(tmp_path, monkeypatch):
    path = tmp_path / "empty.txt"
    path.write_bytes(b"\n\n")
    assert not prepare_inputs(files=[path]).names
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        prepare_inputs(files=[path])
    monkeypatch.setattr("username_finder.roblox.MAX_RECORD_BYTES", 10)
    path.write_bytes(b"x" * 11)
    with pytest.raises(ValueError, match="MiB"):
        prepare_inputs(files=[path])


def test_persistence_skip_recheck_all_and_change_history(repository):
    calls = []
    run(repository, ["SomeName", "somename"], fetcher=lambda url: (calls.append(url), response(1))[1])
    assert len(calls) == 1
    identity = run(repository, ["SOMENAME"], fetcher=lambda _: pytest.fail("Previously checked must skip"))
    assert repository.progress(identity)["skipped"] == 1
    identity = run(repository, kind="recheck_all", fetcher=lambda url: (calls.append(url), response(0))[1])
    assert len(calls) == 2 and repository.progress(identity)["total"] == 1
    history = repository.history("somename")
    assert len(history) == 2
    assert history[0]["classification"] == "Available" and history[0]["previous_classification"] == "Taken"
    assert history[0]["changed"] == 1 and history[1]["classification"] == "Taken"
    assert repository.results()[0][0]["completed_checks"] == 2


def test_history_survives_reopening_and_separates_snapchat(tmp_path):
    path = tmp_path / "shared.db"
    store = Store(path)
    repo = RobloxStore(store)
    run(repo, ["historyName"])
    assert store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM username_state").fetchone()[0] == 0
    store.close()
    reopened = Store(path)
    try:
        rows, count = RobloxStore(reopened).results()
        assert count == 1 and rows[0]["completed_checks"] == 1
        assert reopened.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not reopened.db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        reopened.close()


def test_cancel_and_resume_without_duplicate_requests(repository):
    calls, waits = [], []
    identity = repository.create_run(["oneName", "twoName", "threeName"])
    runner = RobloxRunner(
        repository,
        fetcher=lambda url: (calls.append(url), response())[1],
        wait=lambda seconds: (waits.append(seconds), False)[1],
    )
    runner.updated = lambda: runner.cancel.set() if len(calls) == 1 else None
    runner.run(identity)
    assert len(calls) == 1 and repository.progress(identity)["state"] == "Paused"
    repository.recover()
    RobloxRunner(
        repository,
        fetcher=lambda url: (calls.append(url), response())[1],
        wait=lambda seconds: (waits.append(seconds), False)[1],
    ).run(identity)
    assert len(calls) == len(set(calls)) == 3
    assert all(4.5 < seconds <= 5 for seconds in waits)
    assert repository.progress(identity)["state"] == "Completed"


def test_crash_inflight_is_unknown_without_repeating_it(repository):
    identity = repository.create_run(["firstName", "nextName"])
    repository.start(identity)
    task = repository.claim_next(identity)
    repository.recover()
    rows = repository.history(task["username"])
    assert rows[0]["classification"] == "Unknown" and rows[0]["completed"] == 0
    assert repository.results()[0][0]["completed_checks"] == 0
    calls = []
    RobloxRunner(
        repository, fetcher=lambda url: (calls.append(url), response())[1], wait=lambda _: False
    ).run(identity)
    assert len(calls) == 1 and "nextName" in calls[0]


def test_cancel_before_request_and_single_worker_lease(repository):
    identity = repository.create_run(["someName"])
    runner = RobloxRunner(repository, fetcher=lambda _: pytest.fail("No request before start"))
    runner.cancel.set()
    runner.run(identity)
    assert repository.progress(identity)["counts"] == {"Pending": 1}
    assert repository.start(identity)
    other = repository.create_run(["otherName"])
    with pytest.raises(ValueError, match="نشطة"):
        repository.start(other)


@pytest.mark.parametrize("status", [200, 429, 503])
def test_retry_after_stops_and_persists_across_operations(repository, status):
    calls = []
    identity = run(
        repository,
        ["firstName", "secondName"],
        fetcher=lambda url: (
            calls.append(url),
            response(status=status, headers={"content-type": "application/json", "retry-after": "120"}),
        )[1],
    )
    assert len(calls) == 1
    assert repository.progress(identity)["state"] == "Restricted"
    assert repository.history("firstName")[0]["classification"] == "Unknown"
    assert datetime.fromisoformat(repository.control()["not_before"]) > datetime.now(UTC) + timedelta(
        seconds=115
    )
    waits = []
    other = repository.create_run(["thirdName"])
    RobloxRunner(
        repository, fetcher=lambda _: response(), wait=lambda seconds: (waits.append(seconds), False)[1]
    ).run(other)
    assert waits[0] > 115


@pytest.mark.parametrize("status,challenge", [(401, False), (403, False), (200, True)])
def test_access_and_challenges_fail_closed(repository, status, challenge):
    identity = run(
        repository,
        ["firstName", "secondName"],
        fetcher=lambda _: response(status=status, challenge=challenge),
    )
    assert repository.progress(identity)["counts"] == {"Done": 1, "Pending": 1}
    assert repository.control()["blocked"]
    with pytest.raises(ValueError):
        run(repository, ["thirdName"], fetcher=lambda _: pytest.fail("No bypass"))


def test_malformed_persistent_responses_stop_without_retry(repository):
    calls = []
    identity = run(
        repository,
        ["oneName", "twoName", "threeName"],
        fetcher=lambda url: (calls.append(url), response(body=b"{"))[1],
    )
    assert len(calls) == 2 and repository.control()["blocked"]
    assert repository.progress(identity)["state"] == "Restricted"
    assert repository.counts() == {"Unknown": 2}


def test_csv_import_idempotence_out_of_order_history_and_no_network(repository, tmp_path):
    newer = csv_file(tmp_path / "new.csv", [("SomeName", 0, "2026-11-07T06:00:00+00:00")])
    older = csv_file(tmp_path / "old.csv", [("SOMENAME", 1, "2026-11-07T05:00:00+00:00")])
    assert repository.import_csv(newer)["imported"] == 1
    assert repository.import_csv(older)["imported"] == 1
    assert repository.import_csv(newer)["already_imported"]
    changed_file = tmp_path / "renamed.csv"
    changed_file.write_text(newer.read_text(encoding="utf-8-sig") + "\n", encoding="utf-8")
    assert repository.import_csv(changed_file)["imported"] == 0
    latest = repository.results()[0][0]
    assert latest["classification"] == "Available" and latest["completed_checks"] == 2
    assert latest["changed"] and latest["previous_classification"] == "Taken"


def test_invalid_import_is_atomic_and_ambiguous_claims_are_unknown(repository, tmp_path):
    path = csv_file(
        tmp_path / "invalid.csv",
        [("ValidName", 0, "2026-11-07T06:00:00+00:00"), ("OtherName", 1, "bad-date")],
    )
    with pytest.raises(ValueError):
        repository.import_csv(path)
    assert repository.results()[1] == 0
    path = csv_file(path, [("ValidName", 0, "2026-11-07T06:00:00+00:00")])
    text = path.read_text(encoding="utf-8-sig").replace('"code"": 0', '"code"": 99')
    path.write_text(text, encoding="utf-8")
    repository.import_csv(path)
    assert repository.results()[0][0]["classification"] == "Unknown"


def test_filtered_export_and_full_history_round_trip(repository, tmp_path):
    run(repository, ["SomeName"], code=1)
    run(repository, ["SomeName"], code=0, skip=False)
    path = tmp_path / "export.csv"
    assert repository.export_csv(path, "Available") == 1
    assert repository.export_csv(path, history=True) == 2
    assert repository.import_csv(path)["imported"] == 0
    assert len(repository.history("SomeName")) == 2


def test_backup_restore_keeps_roblox_history(repository, tmp_path):
    run(repository, ["SavedName"], code=1)
    backup = tmp_path / "backup.db"
    repository.store.backup(backup)
    run(repository, ["SavedName"], code=0, skip=False)
    repository.store.restore(backup, tmp_path / "safety.db")
    assert repository.results()[0][0]["classification"] == "Taken"
    assert len(repository.history("SavedName")) == 1


def test_retry_after_http_date_and_invalid():
    at = datetime(2026, 11, 7, tzinfo=UTC)
    assert retry_not_before("Sat, 07 Nov 2026 00:02:00 GMT", at=at) == "2026-11-07T00:02:00+00:00"
    assert retry_not_before("bad", at=at) == "2026-11-07T00:05:00+00:00"


def test_cancellation_between_durable_claim_and_request_does_not_send(repository, monkeypatch):
    identity = repository.create_run(["SomeName"])
    runner = RobloxRunner(repository, fetcher=lambda _: pytest.fail("Cancelled before request"))
    actual = repository.claim_next

    def claim(run_id, **kwargs):
        task = actual(run_id, **kwargs)
        runner.cancel.set()
        return task

    monkeypatch.setattr(repository, "claim_next", claim)
    runner.run(identity)
    assert repository.progress(identity)["counts"] == {"Pending": 1}
    assert repository.results()[1] == 0


def test_crash_uncertainty_survives_export_import_without_counting_completed(repository, tmp_path):
    identity = repository.create_run(["SomeName"])
    repository.start(identity)
    repository.claim_next(identity)
    repository.recover()
    path = tmp_path / "uncertain.csv"
    repository.export_csv(path, history=True)
    other_store = Store(tmp_path / "other.db")
    try:
        other = RobloxStore(other_store)
        assert other.import_csv(path)["imported"] == 1
        assert other.results()[0][0]["completed_checks"] == 0
        assert other.results()[0][0]["classification"] == "Unknown"
    finally:
        other_store.close()


def test_unfinished_operation_remains_selectable_after_many_completed_operations(repository):
    pending = repository.create_run(["StillPending"])
    repository.end(pending, "Paused")
    with repository.store.transaction() as db:
        db.executemany(
            "INSERT INTO roblox_runs(id,created_at,updated_at,kind,state,skip_previous) VALUES(?,?,?,?,?,?)",
            [
                (
                    f"finished-{i}",
                    f"2027-01-01T00:00:{i:03d}Z",
                    "2027-01-01T00:00:00Z",
                    "normal",
                    "Completed",
                    1,
                )
                for i in range(110)
            ],
        )
    rows = repository.runs()
    assert rows[0]["id"] == pending
    assert len(rows) == 101
