"""Mock-only restriction inspection and explicit single-request diagnostics."""

import base64
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from test_roblox import repository as repository
from test_roblox import response

from username_finder import roblox_protocol as protocol
from username_finder.roblox_activity import RobloxActivity
from username_finder.roblox_restriction import (
    RestrictionDiagnostic,
    diagnostic_summary,
    restriction_status,
)


def control(repo, **changes):
    with repo.store.transaction() as db:
        value = repo._control(db)
        value.update(changes)
        repo._save_control(db, value)


def checkpoint(guard, state="Hold"):
    guard.cooldown.write_text(
        json.dumps(
            {"version": 1, "owner": "a" * 64 + ":" + "b" * 64, "restriction": "c" * 64, "state": state}
        )
        + "\n",
        encoding="utf-8",
    )


def test_allowed_status_is_read_only_and_never_constructs_transport(repository, monkeypatch):
    def forbidden():
        pytest.fail("Displaying status must not construct a transport")

    monkeypatch.setattr(protocol, "PersistentTransport", forbidden)
    before = repository.store.db.total_changes
    for _ in range(3):
        status = restriction_status(repository)
        assert status["allowed"] and "غير معروفة" in status["text"]
    assert repository.store.db.total_changes == before
    assert not repository.runs() and not repository.known_count()


@pytest.mark.parametrize(
    "kind",
    [
        "historical",
        "pending",
        "empty_pending",
        "malformed_clear",
        "cooldown",
        "malformed_cooldown",
        "blocked",
        "active",
        "deadline",
        "bad_deadline",
        "bad_counter",
        "unknown_threshold",
        "inflight",
        "released_pending",
    ],
)
def test_every_blocked_state_preserves_markers_and_prevents_diagnostic(repository, kind):
    guard = RobloxActivity(repository.store.path)
    if kind == "historical":
        guard.block.write_bytes(b"http_429")
    elif kind == "empty_pending":
        guard.pending.write_bytes(b"")
    elif kind == "malformed_clear":
        guard.pending.write_bytes(b"malformedClear\n")
    elif kind in ("pending", "released_pending"):
        guard.pending.write_bytes(b"Pending:uncertain\n")
        if kind == "released_pending":
            checkpoint(guard, "Released")
    elif kind == "cooldown":
        checkpoint(guard)
    elif kind == "malformed_cooldown":
        guard.cooldown.write_bytes(b"partial")
    elif kind == "blocked":
        control(repository, blocked=True)
    elif kind == "active":
        control(repository, active_run="existing")
    elif kind == "deadline":
        control(repository, not_before=(datetime.now(UTC) + timedelta(minutes=2)).isoformat())
    elif kind == "bad_deadline":
        control(repository, not_before="invalid")
    elif kind == "bad_counter":
        control(repository, unknown_streak="invalid")
    elif kind == "unknown_threshold":
        control(repository, unknown_streak=2)
    else:
        identity = repository.create_run(["ExistingName"])
        repository.start(identity)
        repository.claim_next(identity)
        control(repository, active_run=None)
    paths = (guard.block, guard.pending, guard.cooldown)
    before = {p: p.read_bytes() for p in paths if p.exists()}
    count = len(repository.runs())
    calls = []
    for _ in range(2):
        status = restriction_status(repository)
        assert not status["allowed"]
        diagnostic = RestrictionDiagnostic(repository, fetcher=lambda url: calls.append(url))
        with pytest.raises(ValueError):
            diagnostic.run("DiagnosticName")
    assert not calls and len(repository.runs()) == count
    assert before == {p: p.read_bytes() for p in paths if p.exists()}
    if kind == "historical":
        assert "تاريخي" in status["text"] and "HTTP 429" in status["text"]


@pytest.mark.parametrize("kind", ["ok", "forbidden", "rate_limit", "timeout", "malformed", "challenge"])
def test_one_request_has_durable_evidence_and_existing_restriction_handling(repository, kind):
    replies = {
        "ok": response(),
        "forbidden": response(status=403),
        "rate_limit": response(status=429, headers={"content-type": "application/json", "retry-after": "60"}),
        "timeout": protocol.Response(error="timeout", complete=False),
        "malformed": response(body=b"{"),
        "challenge": response(challenge=True),
    }
    calls = []

    def fetch(url):
        calls.append(url)
        assert (
            repository.store.db.execute(
                "SELECT COUNT(*) FROM roblox_queue WHERE state='InFlight'"
            ).fetchone()[0]
            == 1
        )
        return replies[kind]

    worker = RestrictionDiagnostic(repository, fetcher=fetch)
    identity = worker.run("DiagnosticName")
    assert len(calls) == 1
    with pytest.raises(ValueError):
        worker.run("DiagnosticName")
    assert len(calls) == 1
    saved = repository.history("DiagnosticName")[0]
    evidence = json.loads(saved["evidence"])
    assert base64.b64decode(evidence["raw_base64"]) == replies[kind].body
    assert evidence["raw_sha256"] == hashlib.sha256(replies[kind].body).hexdigest()
    assert saved["classification"] == ("Available" if kind == "ok" else "Unknown")
    assert repository.progress(identity)["kind"] == "restriction_diagnostic"
    assert repository.progress(identity)["counts"] == {"Done": 1}
    assert not repository.control().get("active_run")
    assert worker.night.get(identity) is None
    text = diagnostic_summary(repository, identity)
    assert "لا يثبت" in text and "لا إعادة تلقائية" in text
    if kind == "forbidden":
        assert repository.control()["blocked"] and "403" in text
    elif kind == "rate_limit":
        assert "429" in text and repository.control()["not_before"] > datetime.now(UTC).isoformat()
    elif kind == "ok":
        assert "HTTP 200" in text
    assert not RobloxActivity(repository.store.path).block.exists()


def test_cross_process_guard_prevents_overlapping_diagnostics_and_regular_entry(repository):
    entered, release = threading.Event(), threading.Event()
    calls, failures = [], []

    def fetch(url):
        calls.append(url)
        entered.set()
        assert release.wait(5)
        return response()

    worker = RestrictionDiagnostic(repository, fetcher=fetch)

    def run():
        try:
            worker.run("FirstDiagnostic")
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(3)
        assert not restriction_status(repository)["allowed"]
        with pytest.raises(ValueError):
            RestrictionDiagnostic(repository, fetcher=fetch).run("SecondDiagnostic")
        with pytest.raises(ValueError):
            with RobloxActivity(repository.store.path):
                pytest.fail("Shared guard must remain held")
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not failures and len(calls) == 1


def test_explicit_cancel_before_dispatch_sends_nothing(repository):
    worker = RestrictionDiagnostic(repository, fetcher=lambda _: pytest.fail("Cancelled"))
    worker.cancel.set()
    assert worker.run("DiagnosticName") is None
    assert not repository.runs()


def test_default_transport_is_owned_closed_and_never_retried(repository, monkeypatch):
    events = []

    class Transport:
        def __init__(self):
            events.append("created")

        def fetch(self, url):
            events.append(url)
            result = protocol.Response(error="timeout", complete=False)
            result.retryable = True  # Even an eligible pre-dispatch failure must not retry.
            return result

        def close(self):
            events.append("closed")

    monkeypatch.setattr(protocol, "PersistentTransport", Transport)
    identity = RestrictionDiagnostic(repository).run("DiagnosticName")
    assert events == ["created", protocol.request_url("DiagnosticName"), "closed"]
    assert repository.progress(identity)["counts"] == {"Done": 1}


def test_status_does_not_display_sensitive_messages_or_raw_unknown_markers(repository):
    worker = RestrictionDiagnostic(
        repository, fetcher=lambda _: response(body=b'{"code":0,"message":"private-test-message"}')
    )
    worker.run("DiagnosticName")
    guard = RobloxActivity(repository.store.path)
    guard.block.write_bytes(b"private-test-secret")
    text = restriction_status(repository)["text"]
    assert "private-test" not in text and "HTTP 200" in text and "تاريخي" in text


def test_status_rechecks_shared_markers_after_preview(repository):
    assert restriction_status(repository)["allowed"]
    RobloxActivity(repository.store.path).block.write_bytes(b"http_429")
    with pytest.raises(ValueError):
        RestrictionDiagnostic(repository, fetcher=lambda _: pytest.fail("Must not dispatch")).run(
            "DiagnosticName"
        )
