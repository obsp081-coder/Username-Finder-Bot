"""Evidence-scoped review tests. No provider traffic or production data."""

import hashlib
import json
import sqlite3

import pytest

from username_finder import rovalid
from username_finder import rovalid_review as review
from username_finder.roblox_activity import RobloxActivity
from username_finder.storage import Store


@pytest.fixture
def db(tmp_path):
    store = Store(tmp_path / "review.db")
    yield store.path
    store.close()


def decide(db, decision="authorize", mode="validator", connection="direct", **changes):
    status = rovalid.permission(db, mode=mode, connection=connection)
    values = dict(
        mode=mode,
        connection=connection,
        decision=decision,
        reviewer="Offline reviewer",
        evidence="Synthetic evidence reference, not a provider validation",
        fingerprint=status.get("fingerprint", ""),
    )
    return review.record_review(db, **(values | changes))


def test_default_review_required_and_independent_mode_and_route_authorizations(db):
    status = rovalid.permission(db, mode="validator")
    assert not status["allowed"] and status["state"] == "ReviewRequired"
    assert "غير معروفة" in status["reason"]
    first = decide(db, "review")
    assert not rovalid.permission(db, mode="validator")["allowed"]
    second = decide(db)
    assert rovalid.permission(db, mode="validator")["allowed"]
    assert not rovalid.permission(db, mode="two_stage")["allowed"]
    assert not rovalid.permission(db, mode="validator", connection="private")["allowed"]
    assert not rovalid.permission(db, mode="two_stage", connection="sources")["allowed"]
    assert second["previous"] == first["hash"]
    decide(db, mode="two_stage")
    assert rovalid.permission(db, mode="two_stage")["allowed"]
    decide(db, "deny", mode="two_stage")
    assert not rovalid.permission(db, mode="two_stage")["allowed"]
    assert rovalid.permission(db, mode="validator")["allowed"]
    assert review.journal(db)[:2] == [first, second]


@pytest.mark.parametrize("kind", ["bulk429", "new429", "pending", "cooldown", "sql", "inflight", "deadline"])
def test_existing_authorization_never_bypasses_new_or_historical_guards(db, kind):
    decide(db)
    guard = RobloxActivity(db)
    if kind in ("bulk429", "new429"):
        guard.block.write_bytes(b"http_429" if kind == "bulk429" else b"rovalid_review_required")
    elif kind == "pending":
        guard.pending.write_bytes(b"Pending:uncertain\n")
    elif kind == "cooldown":
        guard.cooldown.write_text(
            json.dumps(dict(version=1, owner="a" * 64 + ":" + "b" * 64, restriction="c" * 64, state="Hold"))
            + "\n"
        )
    elif kind == "inflight":
        from username_finder.roblox_storage import RobloxStore

        with rovalid.readonly_repo(db) as repo:
            assert isinstance(repo, RobloxStore)
        # Use a real temporary Store solely to build an uncertain queue fixture.
        store = Store(db)
        repo = RobloxStore(store)
        identity = repo.create_run(["Example"], skip=False)
        with store.db:
            store.db.execute("UPDATE roblox_queue SET state='InFlight' WHERE run_id=?", (identity,))
        store.close()
    else:
        value = dict(blocked="http_429") if kind == "sql" else dict(not_before="2099-01-01T00:00:00+00:00")
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE roblox_control SET value=?", (json.dumps(value),))
    originals = {p: p.read_bytes() for p in (guard.block, guard.pending, guard.cooldown) if p.exists()}
    audit = review.journal_path(db).read_bytes()
    for mode in review.MODES:
        status = rovalid.permission(db, mode=mode)
        assert not status["allowed"]
        with pytest.raises(ValueError):
            decide(db, mode=mode)
    # Review-only is useful even while the operational guard prevents authorization.
    record = decide(db, "review")
    assert record["snapshot"]["markers"]
    assert review.journal_path(db).read_bytes().startswith(audit)
    assert originals == {p: p.read_bytes() for p in originals}


@pytest.mark.parametrize("damage", ["partial", "hash", "missing", "scope", "reorder", "garbage"])
def test_missing_corrupt_conflicting_audit_is_fail_closed(db, damage):
    decide(db)
    decide(db, "review", mode="two_stage")
    path = review.journal_path(db)
    data = path.read_bytes()
    if damage == "partial":
        path.write_bytes(data[:-1])
    elif damage == "missing":
        path.unlink()
    elif damage == "reorder":
        path.write_bytes(b"".join(reversed(data.splitlines(keepends=True))))
    elif damage == "garbage":
        path.write_bytes(b"[]\n")
    else:
        lines = data.decode().splitlines()
        row = json.loads(lines[0])
        row["hash" if damage == "hash" else "scope"] = "bad"
        path.write_text(json.dumps(row) + "\n" + lines[1] + "\n", encoding="utf8")
    assert not rovalid.permission(db, mode="validator")["allowed"]


def test_stale_decision_missing_evidence_and_concurrent_lock_are_rejected(db):
    status = rovalid.permission(db, mode="validator")
    RobloxActivity(db).pending.write_bytes(b"Clear\n")
    with pytest.raises(ValueError, match="changed"):
        decide(db, fingerprint=status["fingerprint"])
    with pytest.raises(ValueError):
        decide(db, evidence="")
    with RobloxActivity(db):
        assert not rovalid.permission(db, mode="validator")["allowed"]
        with pytest.raises(ValueError):
            decide(db)
    decide(db)
    with review._audit_lock(db), pytest.raises((OSError, ValueError)):
        decide(db, "deny")
    with review._audit_lock(db):
        assert not rovalid.permission(db, mode="validator")["allowed"]


def test_malformed_cooldown_and_unknown_block_cannot_be_scoped_away(db):
    guard = RobloxActivity(db)
    guard.cooldown.write_bytes(b"bad checkpoint")
    assert not rovalid.permission(db, mode="validator")["allowed"]
    before = hashlib.sha256(guard.cooldown.read_bytes()).hexdigest()
    decide(db, "review")
    assert hashlib.sha256(guard.cooldown.read_bytes()).hexdigest() == before
    guard.block.write_bytes(b"unknown source and endpoint")
    assert not rovalid.permission(db, mode="validator")["allowed"]
    assert not rovalid.permission(db, mode="two_stage")["allowed"]


def test_review_is_durable_and_scoped_to_database_identity(db, tmp_path):
    decide(db)
    assert rovalid.permission(db, mode="validator")["allowed"]
    other = Store(tmp_path / "other.db")
    review.journal_path(other.path).write_bytes(review.journal_path(db).read_bytes())
    assert not rovalid.permission(other.path, mode="validator")["allowed"]
    other.close()


def test_v1_review_history_is_preserved_but_native_policy_needs_explicit_review(db):
    row = decide(db)
    row["policy"] = review.LEGACY_POLICY
    row["snapshot"]["policy"] = review.LEGACY_POLICY
    row["fingerprint"] = review.digest(row["snapshot"])
    row["hash"] = review.digest({k: v for k, v in row.items() if k != "hash"})
    path = review.journal_path(db)
    path.write_text(json.dumps(row) + "\n", encoding="utf8")
    original = path.read_bytes()
    status = rovalid.permission(db, mode="validator")
    assert not status["allowed"] and status["state"] == "BlockedAfterReview"
    assert "سياسة ربط أقدم" in status["reason"]
    assert review.journal(db) == [row] and path.read_bytes() == original
    current = decide(db)
    assert current["policy"] == review.POLICY and current["previous"] == row["hash"]
    assert path.read_bytes().startswith(original)
    assert rovalid.permission(db, mode="validator")["allowed"]
