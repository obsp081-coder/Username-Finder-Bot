"""Offline advisory states only. No test may authorize a blocked production request."""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_roblox_bulk import response
from test_roblox_prefilter import execute
from test_roblox_prefilter import job as job
from test_roblox_prefilter import no_live as no_live
from test_roblox_prefilter_review import mark_old_policy, table_rows

from username_finder.roblox_activity import RobloxActivity
from username_finder.roblox_cooldown import MAX_WAIT_SECONDS, advisory, retry_after, wait_offline
from username_finder.roblox_prefilter import Prefilter, settings
from username_finder.roblox_prefilter_review import inspect_operation, record_review

AT = datetime(2026, 11, 8, tzinfo=UTC)


def evidence(value="60", **changes):
    result = {
        "request_id": 1,
        "status": 429,
        "response_at": AT.isoformat(),
        "body_sha256": "a" * 64,
        "body_integrity_verified": True,
        "response_complete": True,
        "transport_error": "",
        "challenge": False,
        "headers": {"retry-after": value},
    }
    result.update(changes)
    return result


@pytest.mark.parametrize(
    "value,seconds,source",
    [
        ("60", 60, "retry_after_seconds"),
        ("0", 0, "retry_after_seconds"),
        (" 120 ", 120, "retry_after_seconds"),
        ("Sun, 08 Nov 2026 00:02:00 GMT", 120, "retry_after_http_date"),
        ("Sat, 07 Nov 2026 00:00:00 GMT", 0, "retry_after_http_date"),
        (None, 300, "fallback_missing"),
        ("bad", 300, "fallback_malformed"),
        ("-1", 300, "fallback_malformed"),
        ("1.5", 300, "fallback_malformed"),
        ("+10", 300, "fallback_malformed"),
        ("", 300, "fallback_malformed"),
        ("Sun, 08 Nov 2026 00:02:00", 300, "fallback_malformed"),
    ],
)
def test_deadlines_are_anchored_to_response_and_not_review_time(value, seconds, source):
    result = retry_after(value, AT)
    assert result["not_before"] == (AT + timedelta(seconds=seconds)).isoformat()
    assert result["source"] == source and result["wait_supported"]


@pytest.mark.parametrize(
    "value", ["86401", "99999999999999999999", "1" * 1000, "Wed, 01 Jan 2031 00:00:00 GMT"]
)
def test_excessive_waits_never_become_a_shorter_automatic_delay(value):
    result = advisory(evidence(value), now=AT, review_recorded=True)
    assert result["state"] == "ExcessiveWaitReview" and not result["wait_supported"]
    assert not result["dispatch_authorized"]
    if value == "86401":
        assert result["not_before"] == (AT + timedelta(seconds=86401)).isoformat()
    assert (
        wait_offline(result, wall=lambda: AT, clock=lambda: 0, wait=lambda _: pytest.fail("No wait"))
        == "manual_review_required"
    )


def test_first_explicit_review_expiry_and_repeated_429_are_not_authorization():
    original = evidence()
    initial = advisory(original, now=AT, interval=5)
    assert initial["state"] == "CooldownPendingReview" and initial["proposed_interval_seconds"] == 20
    reviewed = advisory(original, now=AT, interval=5, review_recorded=True)
    assert reviewed["state"] == "CooldownReviewed"
    expired = advisory(original, now=AT + timedelta(seconds=61), review_recorded=True)
    assert expired["state"] == "AwaitingProviderAuthorization"
    repeated = advisory(
        evidence("1"),
        now=AT,
        interval=5,
        streak=2,
        previous_not_before=(AT + timedelta(seconds=120)).isoformat(),
        review_recorded=True,
    )
    assert repeated["state"] == "RepeatedRateLimitReview" and not repeated["wait_supported"]
    assert repeated["not_before"] == (AT + timedelta(seconds=120)).isoformat()
    for plan in (initial, reviewed, expired, repeated):
        assert (
            not plan["dispatch_authorized"] and not plan["restriction_cleared"] and not plan["pacing_applied"]
        )
    assert original == evidence()


@pytest.mark.parametrize(
    "interval,streak,expected", [(10, 1, 20), (10, 2, 40), (10, 1000000, 300), (200, 1, 300), (1000, 3, 1000)]
)
def test_proposed_pacing_is_bounded_and_never_shortens_existing_setting(interval, streak, expected):
    assert (
        advisory(evidence(), now=AT, interval=interval, streak=streak)["proposed_interval_seconds"]
        == expected
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": 401},
        {"status": 403},
        {"challenge": True},
        {"response_complete": False},
        {"body_integrity_verified": False},
        {"transport_error": "incomplete_body"},
    ],
)
def test_other_safety_failures_are_not_temporary_rate_limit_authorization(changes):
    result = advisory(evidence(**changes), now=AT, blockers=["shared_provider_restriction_preserved"])
    assert not result["wait_supported"] and not result["dispatch_authorized"]
    assert result["state"] in {"RestrictedPendingReview", "UnsafeEvidenceReview"}


def test_fallback_reset_hints_bad_timestamps_and_unsupported_policy_are_safe():
    item = evidence(None)
    item["headers"]["x-ratelimit-reset"] = "999999999999"
    fallback = advisory(item, now=AT)
    assert fallback["state"] == "FallbackReviewRequired"
    assert fallback["not_before"] == (AT + timedelta(seconds=300)).isoformat()
    assert advisory(evidence(response_at="bad"), now=AT)["state"] == "UnsafeEvidenceReview"
    assert advisory(evidence(), now=AT, pending=True)["state"] == "UnfinishedDeliveryReview"
    assert not advisory(evidence(), now=AT, blockers=["unverified_batch_size_dispatch_disabled"])[
        "wait_supported"
    ]
    assert advisory(None, now=AT)["state"] == "ReadyForSeparateAuthorization"
    assert not advisory(None, now=AT)["dispatch_authorized"]


class Clock:
    def __init__(self):
        self.at = 0.0
        self.skew = 0
        self.calls = 0

    def wall(self):
        return AT + timedelta(seconds=self.at + self.skew)

    def clock(self):
        return self.at

    def wait(self, seconds):
        assert 0 < seconds <= 0.25
        self.calls += 1
        self.at += seconds
        return False


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("normal", "elapsed_review_required"),
        ("cancel", "cancelled"),
        ("backward", "manual_review_required"),
        ("forward", "elapsed_review_required"),
    ],
)
def test_offline_monotonic_wait_cancellation_and_clock_jumps(mode, expected):
    clock = Clock()
    plan = advisory(evidence("1"), now=AT, review_recorded=True)

    def wait(seconds):
        clock.wait(seconds)
        if mode == "cancel":
            return True
        if mode == "backward":
            clock.skew = -3600
        if mode == "forward":
            clock.skew = 3600
        return False

    before = copy.deepcopy(plan)
    assert wait_offline(plan, wall=clock.wall, clock=clock.clock, wait=wait) == expected
    assert plan == before and not plan["dispatch_authorized"]
    if mode == "forward":
        assert clock.at == 1  # A wall-clock jump cannot skip the monotonic lower bound.
    assert clock.calls <= 4


def test_timer_rejects_unreviewed_state_and_nonprogressing_clock():
    plan = advisory(evidence("1"), now=AT)
    assert (
        wait_offline(plan, wall=lambda: AT, clock=lambda: 0, wait=lambda _: False) == "manual_review_required"
    )
    plan = advisory(evidence("1"), now=AT, review_recorded=True)
    assert (
        wait_offline(plan, wall=lambda: AT, clock=lambda: 0, wait=lambda _: False) == "manual_review_required"
    )


@pytest.mark.parametrize("header", ["60", None, "bad", "86401"])
def test_saved_cooldown_audit_restart_never_changes_existing_restriction(job, header):
    job.prepare(job.test_source)
    job.transport = lambda _: response(status=429, headers={} if header is None else {"retry-after": header})
    execute(job)
    before = table_rows(job)
    manifest = job.manifest_path.read_bytes()
    marker = RobloxActivity(job.database).block
    marker_bytes = marker.read_bytes()
    digest = hashlib.sha256(job.path.read_bytes()).hexdigest()
    first = inspect_operation(job)["restriction_review"]["rate_limit_advisory"]
    assert first["state"] == (
        "CooldownPendingReview"
        if header == "60"
        else "ExcessiveWaitReview"
        if header == "86401"
        else "FallbackReviewRequired"
    )
    assert hashlib.sha256(job.path.read_bytes()).hexdigest() == digest
    reviewed = record_review(job, job.manifest()["fingerprint"])["restriction_review"]
    assert reviewed["rate_limit_advisory"]["review_recorded"]
    saved = json.loads(reviewed["recent_reviews"][0]["data"])["rate_limit_advisory"]
    assert saved["not_before"] == first["not_before"] and saved["review_recorded"]
    restored = Prefilter(
        job.directory, job.database, wall=job.test_clock.wall, transport=lambda _: pytest.fail("No replay")
    )
    job.test_clock.at += MAX_WAIT_SECONDS * 10
    after = inspect_operation(restored)["restriction_review"]
    assert after["rate_limit_advisory"]["not_before"] == first["not_before"]
    assert after["recovery_decision"] == "Blocked" and not after["dispatch_authorized"]
    with pytest.raises(ValueError):
        execute(restored)
    with pytest.raises(ValueError):
        restored.review_uncertain(restored.manifest()["fingerprint"])
    with pytest.raises(ValueError):
        with RobloxActivity(job.database):
            pass
    assert table_rows(job) == before and marker.read_bytes() == marker_bytes
    assert job.manifest_path.read_bytes() == manifest and not job.database.exists()
    assert settings()["batch_size"] == 20


def test_legacy_marker_old_review_and_policy_mismatch_cannot_unlock(job):
    job.prepare(job.test_source)
    job.transport = lambda _: response(status=429)
    execute(job)
    # Simulate an old acknowledgement that never assessed a cooldown.
    with job.connect() as db:
        last = inspect_operation(job)["restriction_review"]["last_response_evidence"]
        job.audit(db, "restriction_review", {"last_response_evidence": last})
    result = inspect_operation(job)["restriction_review"]
    assert not result["rate_limit_advisory"]["review_recorded"]
    mark_old_policy(job)
    before = job.path.read_bytes(), job.manifest_path.read_bytes()
    assert (
        inspect_operation(job)["restriction_review"]["rate_limit_advisory"]["state"] == "UnsafeEvidenceReview"
    )
    job.recover()
    assert before == (job.path.read_bytes(), job.manifest_path.read_bytes())


def test_marker_without_matching_429_evidence_never_invents_a_cooldown(job):
    job.prepare(job.test_source)
    marker = RobloxActivity(job.database).block
    marker.write_bytes(b"http_429")
    before = marker.read_bytes()
    result = record_review(job, job.manifest()["fingerprint"])["restriction_review"]
    assert result["rate_limit_advisory"]["state"] == "RestrictedPendingReview"
    assert result["rate_limit_advisory"]["not_before"] is None
    job.test_clock.at += 10**8
    with pytest.raises(ValueError):
        execute(job)
    assert marker.read_bytes() == before and not job.test_calls


def test_consecutive_saved_429_snapshots_keep_longest_reviewed_deadline(job):
    job.prepare(job.test_source)
    job.transport = lambda _: response(status=429, headers={"retry-after": "600"})
    execute(job)
    first = record_review(job, job.manifest()["fingerprint"])["restriction_review"]["rate_limit_advisory"]
    # An isolated legacy-format fixture; the actual blocked runner cannot dispatch this second request.
    with job.connect() as db:
        previous = db.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()
        raw = json.loads(previous["evidence"])
        raw["headers"]["retry-after"] = "1"
        db.execute(
            "INSERT INTO requests(phase,state,names,request_at,response_at,evidence,latency,stop) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                previous["phase"],
                previous["state"],
                previous["names"],
                previous["request_at"],
                previous["response_at"],
                json.dumps(raw),
                previous["latency"],
                previous["stop"],
            ),
        )
    rows = table_rows(job)
    result = inspect_operation(job)["restriction_review"]["rate_limit_advisory"]
    assert result["state"] == "RepeatedRateLimitReview" and result["consecutive_429"] == 2
    assert result["not_before"] == first["not_before"]
    assert not result["review_recorded"] and not result["wait_supported"]
    assert table_rows(job) == rows


def test_legacy_response_without_transport_flags_is_readable_but_never_assumed_safe(job):
    job.prepare(job.test_source)
    job.transport = lambda _: response(status=429, headers={"retry-after": "1"})
    execute(job)
    with job.connect() as db:
        row = db.execute("SELECT id,evidence FROM requests ORDER BY id DESC LIMIT 1").fetchone()
        raw = json.loads(row["evidence"])
        for key in ("complete", "error", "challenge"):
            raw.pop(key)
        db.execute("UPDATE requests SET evidence=? WHERE id=?", (json.dumps(raw), row["id"]))
    before = job.path.read_bytes()
    report = inspect_operation(job)["restriction_review"]
    assert report["last_response_evidence"]["body_integrity_verified"]
    assert report["rate_limit_advisory"]["state"] == "UnsafeEvidenceReview"
    assert not report["rate_limit_advisory"]["wait_supported"] and not report["dispatch_authorized"]
    assert job.path.read_bytes() == before
