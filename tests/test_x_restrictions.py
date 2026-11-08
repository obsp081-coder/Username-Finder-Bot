import json
from datetime import timedelta

import pytest
from test_x import Clock, response, run
from test_x import repo as repo

from username_finder import x
from username_finder.storage import Store
from username_finder.x_storage import XStore


@pytest.mark.parametrize(
    "headers,seconds,sources,invalid",
    [
        ({}, None, [], []),
        ({"date": "Sat, 07 Nov 2026 19:03:48 GMT"}, None, [], []),
        ({"retry-after": "120"}, 120, ["retry-after"], []),
        ({"retry-after": "0"}, 0, ["retry-after"], []),
        ({"retry-after": "Sat, 07 Nov 2026 00:02:00 GMT"}, 120, ["retry-after"], []),
        ({"retry-after": "unknown"}, None, [], ["retry-after"]),
        ({"retry-after": "-1"}, None, [], ["retry-after"]),
        ({"retry-after": "1.5"}, None, [], ["retry-after"]),
        ({"retry-after": "9999999999999999999999"}, None, [], ["retry-after"]),
        ({"x-rate-limit-remaining": "0"}, None, [], ["x-rate-limit-reset"]),
        ({"x-rate-limit-remaining": "1", "x-rate-limit-reset": "1794009780"}, None, [], []),
        ({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "nan"}, None, [], ["x-ratelimit-reset"]),
    ],
)
def test_server_wait_evidence_never_invents_restriction_expiry(headers, seconds, sources, invalid):
    at = Clock().now()
    info = x.restriction_wait(headers, at.isoformat())
    expected = (at + timedelta(seconds=seconds)).isoformat() if seconds is not None else None
    assert info["minimum_retry_at"] == expected
    assert info["wait_sources"] == sources
    assert info["invalid_wait_headers"] == invalid
    assert not info["expiry_known"]


@pytest.mark.parametrize("prefix", ["x-rate-limit-", "x-ratelimit-"])
def test_all_valid_server_waits_are_combined_conservatively(prefix):
    at = Clock().now()
    headers = {
        "retry-after": "60",
        prefix + "remaining": "0",
        prefix + "reset": str(int((at + timedelta(seconds=300)).timestamp())),
    }
    info = x.restriction_wait(headers, at.isoformat())
    assert info["minimum_retry_at"] == (at + timedelta(seconds=300)).isoformat()
    assert len(info["wait_sources"]) == 2


def test_invalid_observation_does_not_invent_relative_server_deadline():
    assert x.restriction_wait({"retry-after": "120"}, None)["minimum_retry_at"] is None
    assert x.restriction_wait({"retry-after": "120"}, "2026-11-07T00:00:00")["minimum_retry_at"] is None


@pytest.mark.parametrize("legacy", [False, True], ids=["new-reference", "legacy-evidence"])
@pytest.mark.parametrize("headers", [{}, {"retry-after": "120"}])
def test_readonly_review_and_restart_preserve_stop_history_and_wait(repo, headers, legacy):
    clock = Clock()
    identity = repo.prepare(x.input_spec("first_name\nsecond_name"))
    run(repo, identity=identity, clock=clock, fetcher=lambda *a: x.protocol.Response(429, b"", headers))
    if legacy:
        with repo.store.transaction() as db:
            control = repo.control()
            control.pop("restriction_check_id", None)
            db.execute("UPDATE x_control SET value=? WHERE id=1", (json.dumps(control),))
    history = repo.history("first_name")
    original = repo.progress(identity)
    control = repo.control()
    info = repo.restriction_info()
    assert info["check_id"] == history[0]["id"]
    assert info["run_id"] == identity and info["reason"] == "unexpected_http_status:429"
    assert info["headers"] == headers and not info["expiry_known"]
    expected = (clock.now() + timedelta(seconds=120)).isoformat() if headers else None
    assert info["minimum_retry_at"] == expected
    assert repo.control() == control
    database = repo.store.path
    repo.store.close()
    reopened_store = Store(database)
    try:
        reopened = XStore(reopened_store)
        reopened.recover()
        assert reopened.restriction_info() == info
        clock.seconds += 86400
        assert reopened.restriction_info() == info
        assert reopened.control() == control
        with pytest.raises(ValueError):
            run(reopened, identity=identity, clock=clock, fetcher=lambda *a: pytest.fail("No probe or retry"))
        assert reopened.history("first_name") == history
        assert reopened.progress(identity) == original
    finally:
        reopened_store.close()


def test_local_deadline_and_unrelated_evidence_cannot_authorize_recovery(repo):
    with repo.store.transaction() as db:
        repo._control(db, blocked="unexpected_http_status:429", not_before="2099-01-01T00:00:00+00:00")
    info = repo.restriction_info()
    assert info["minimum_retry_at"] is None and info["check_id"] is None
    assert info["local_not_before"].startswith("2099")
    assert repo.control()["blocked"]


def test_preparation_counts_distinguish_all_filters_and_do_not_recheck(repo):
    run(repo, x.input_spec("prior_name"), fetcher=lambda *a: response(False, "taken"))
    identity = repo.prepare(x.input_spec("prior_name\nPRIOR_NAME\negge\nvalid_name\n\n"))
    info = repo.progress(identity)
    assert info["input_source"] == "manual" and info["requested_candidates"] is None
    assert info["generated_candidates"] == 0 and info["cursor"] == 5
    assert info["duplicates"] == info["invalid"] == info["skipped"] == info["blank"] == 1
    assert info["total"] == info["pending"] == 1
    assert repo.preview(identity) == ["valid_name"]
    assert len(repo.history("prior_name")) == 1


def test_fixed_generation_skips_history_without_replacement_or_mutation(repo):
    config = x.generation_range_spec(8, 8, 10)
    spec = {"generation": config}
    first, clock = run(repo, spec)
    original = repo.progress(first)
    skipped = repo.prepare(spec)
    info = repo.progress(skipped)
    assert info["requested_candidates"] == info["generated_candidates"] == info["skipped"] == 10
    assert info["invalid"] == info["duplicates"] == info["total"] == 0
    assert len(clock.calls) == 10
    assert repo.progress(first) == original
    assert json.loads(info["spec"])["generation"] == config
