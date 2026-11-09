"""Offline operational recovery, fault boundaries and one-shot authorization."""

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import timedelta

import pytest
from test_roblox_bulk import response
from test_roblox_prefilter import execute
from test_roblox_prefilter import job as job
from test_roblox_prefilter import no_live as no_live
from test_roblox_prefilter_review import mark_old_policy, table_rows

from username_finder import roblox_prefilter_cooldown as recovery
from username_finder.roblox_activity import RobloxActivity
from username_finder.roblox_prefilter import Prefilter, settings


def restrict(job, *, header="2", pilot=45, size=20, **changes):
    job.prepare(job.test_source, pilot_names=pilot, batch_size=size)
    original = job.transport
    calls = []

    def transport(request):
        calls.append(request)
        if len(calls) == 1:
            job.test_clock.at += 0.25
            return response(
                status=429, headers={"content-type": "application/json", "retry-after": header}, **changes
            )
        return original(request)

    job.transport = transport
    result = execute(job)
    job.all_calls = calls
    return result


def review(job):
    snapshot = job.summary()
    return recovery.review(job, snapshot.get("cooldown", {}).get("restriction", ""), snapshot["revision"])


def approval(job):
    review(job)
    recovery.wait_reviewed(job)
    return recovery.authorize(job, recovery.authorization_review(job))


def test_complete_future_recovery_preserves_unknown_no_replay_and_one_use(job, tmp_path):
    stopped = restrict(job)
    assert stopped["phases"]["pilot"]["state"] == "Cooldown"
    assert stopped["counts"]["unknown"] == 20 and stopped["counts"]["processed"] == 20
    assert not RobloxActivity(job.database).block.exists()
    assert stopped["shared_cooldown"]["state"] == "Hold"
    rows = table_rows(job)
    manifest = job.manifest_path.read_bytes()
    with pytest.raises(ValueError):
        execute(job)
    with pytest.raises(ValueError):
        recovery.authorization_review(job)
    review(job)
    with pytest.raises(ValueError):
        recovery.authorization_review(job)
    recovery.wait_reviewed(job)
    assert len(job.all_calls) == 1  # Expiry and review never dispatch.
    token = recovery.authorize(job, recovery.authorization_review(job))
    done = job.run("pilot", token)
    assert done["counts"]["attempts"] == done["counts"]["completed_requests"] == 3
    assert done["counts"]["processed"] == 45 and done["counts"]["unknown"] == 20
    assert done["counts"]["excluded"] == 25 and done["counts"]["failed_requests"] == 1
    assert done["phases"]["pilot"]["state"] == "Completed"
    assert done["shared_cooldown"]["state"] == "Released"
    names = [n for r in job.all_calls for n in r["usernames"]]
    assert len(names) == len(set(names)) == 45
    assert table_rows(job)["requests"][0] == rows["requests"][0]
    assert table_rows(job)["names"][:20] == rows["names"][:20]
    assert job.manifest_path.read_bytes() == manifest and not job.database.exists()
    with pytest.raises(ValueError):
        job.run("pilot", token)
    with job.connect() as db:
        events = [r[0] for r in db.execute("SELECT event FROM audit")]
    assert events.count("cooldown_checkpoint") == events.count("cooldown_review") == 1
    assert events.count("cooldown_authorization") == events.count("cooldown_authorization_consumed") == 1
    job.export(tmp_path / "export")
    assert token not in (tmp_path / "export" / "audit.json").read_text()
    exported = json.loads((tmp_path / "export" / "unresolved.json").read_text())
    assert len(exported) == 20 and all(r["state"] == "Unknown" for r in exported)


def test_restart_cancellation_and_separate_stage_boundaries(job):
    restrict(job, pilot=3, size=2)
    review(job)
    job.wait = lambda _: True
    recovery.wait_reviewed(job)
    assert len(job.all_calls) == 1 and job.summary()["cooldown"]["stage"] == "Reviewed"
    restored = Prefilter(
        job.directory,
        job.database,
        transport=job.transport,
        clock=job.clock,
        wall=job.wall,
        wait=job.test_clock.wait,
    )
    restored.recover()
    with pytest.raises(ValueError):
        recovery.authorization_review(restored)
    recovery.wait_reviewed(restored)
    token = recovery.authorize(restored, recovery.authorization_review(restored))
    done = restored.run("pilot", token)
    assert len(job.all_calls) == 2 and done["counts"]["processed"] == 3
    assert "full" not in done["phases"] and done["phases"]["pilot"]["state"] == "Completed"
    assert done["counts"]["unknown"] == 2


def test_429_at_final_pilot_batch_can_finish_review_without_dispatching_next_stage(job):
    restrict(job, pilot=2, size=2)
    token = approval(job)
    done = job.run("pilot", token)
    assert len(job.all_calls) == 1 and done["phases"]["pilot"]["state"] == "Completed"
    assert "full" not in done["phases"]


def test_second_429_escalates_across_successes_and_cannot_be_authorized(job):
    restrict(job, pilot=21, size=2)
    token = approval(job)
    previous = job.transport
    calls = 0

    def transport(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            return response(status=429, headers={"content-type": "application/json", "retry-after": "2"})
        return previous(request)

    job.transport = transport
    stopped = job.run("pilot", token)
    assert stopped["phases"]["pilot"]["state"] == "Restricted"
    assert stopped["cooldown"]["stage"] == "Escalated" and stopped["counts"]["unknown"] == 4
    assert RobloxActivity(job.database).block.read_bytes() == b"http_429"
    with pytest.raises(ValueError):
        review(job)
    with pytest.raises(ValueError):
        execute(job)


@pytest.mark.parametrize("header", [None, "bad", "-1", "1.5", "86401", "9" * 200])
def test_invalid_or_unsupported_retry_after_stays_persistently_restricted(job, header):
    result = restrict(job, header=header)
    assert result["phases"]["pilot"]["state"] == "Restricted"
    assert not result.get("cooldown") and RobloxActivity(job.database).block.exists()
    with pytest.raises(ValueError):
        review(job)


@pytest.mark.parametrize(
    "changes",
    [
        {"challenge": True},
        {"complete": False},
        {"error": "timeout"},
        {"body": b"bad"},
        {"body": b'{"message":"CAPTCHA required"}'},
    ],
)
def test_unsafe_429_never_enters_recoverable_cooldown(job, changes):
    result = restrict(job, **changes)
    assert result["phases"]["pilot"]["state"] == "Restricted" and not result.get("cooldown")
    assert RobloxActivity(job.database).block.exists()


@pytest.mark.parametrize("status", [401, 403])
def test_other_restrictions_after_authorized_continuation_remain_persistent(job, status):
    restrict(job)
    token = approval(job)
    job.transport = lambda _: response(status=status)
    stopped = job.run("pilot", token)
    assert stopped["phases"]["pilot"]["state"] == "Restricted"
    assert RobloxActivity(job.database).block.read_text() == f"http_{status}"


def test_stale_forged_wrong_phase_and_changed_control_authorizations(job):
    restrict(job)
    review(job)
    recovery.wait_reviewed(job)
    stale = recovery.authorization_review(job)
    review(job)
    with pytest.raises(ValueError):
        recovery.authorize(job, stale)
    with pytest.raises(ValueError):
        recovery.authorize(job, "forged")
    token = recovery.authorize(job, recovery.authorization_review(job))
    with pytest.raises(ValueError):
        job.run("full", token)
    with pytest.raises(ValueError):
        job.run("pilot", "forged")
    job.control = lambda: {"not_before": (job.wall() + timedelta(hours=1)).isoformat()}
    with pytest.raises(ValueError):
        job.run("pilot", token)
    assert len(job.all_calls) == 1


@pytest.mark.parametrize(
    "mode", ["historical", "policy", "uncertain", "changed_evidence", "fake_review", "fake_authorization"]
)
def test_independent_blockers_never_allow_recovery(job, mode):
    restrict(job)
    if mode == "historical":
        RobloxActivity(job.database).block.write_bytes(b"http_429")
    elif mode == "policy":
        mark_old_policy(job)
    else:
        with job.connect() as db:
            if mode == "uncertain":
                db.execute("UPDATE requests SET state='Uncertain'")
            elif mode == "changed_evidence":
                db.execute("UPDATE requests SET evidence='{}'")
            else:
                meta = job.meta(db)
                meta["cooldown"].update(
                    stage="Reviewed" if mode == "fake_review" else "Authorized", authorization_hash="fake"
                )
                job.save(db, meta)
    with pytest.raises(ValueError):
        review(job)
    assert len(job.all_calls) == 1


@pytest.mark.parametrize("offset", [-3600, 3600])
def test_observed_clock_changes_never_authorize_early_continuation(job, offset):
    restrict(job)
    review(job)
    original = job.wall
    job.wall = lambda: original() + timedelta(seconds=offset)
    with pytest.raises(ValueError):
        recovery.wait_reviewed(job)
    with pytest.raises(ValueError):
        recovery.authorization_review(job)
    assert len(job.all_calls) == 1


def test_shared_exclusion_persists_after_expiry_and_applies_to_other_operations(job, tmp_path):
    restrict(job)
    job.test_clock.at += 100
    for _ in range(2):
        with pytest.raises(ValueError):
            with RobloxActivity(job.database):
                pass
    other = Prefilter(tmp_path / "other", job.database, transport=lambda _: pytest.fail("No dispatch"))
    other.prepare(job.test_source)
    with pytest.raises(ValueError):
        execute(other)
    with recovery.shared(job):
        with pytest.raises(ValueError):
            review(job)
    assert len(job.all_calls) == 1


@pytest.mark.parametrize(
    "transition",
    ["cooldown_checkpoint", "cooldown_review", "cooldown_authorization", "cooldown_authorization_consumed"],
)
def test_sql_failures_rollback_critical_transitions_without_dispatch_or_loss(job, monkeypatch, transition):
    original = job.audit

    def audit(db, event, data):
        if event == transition:
            raise sqlite3.OperationalError("Injected checkpoint failure")
        original(db, event, data)

    if transition == "cooldown_checkpoint":
        monkeypatch.setattr(job, "audit", audit)
        result = restrict(job)
        assert result["counts"]["processed"] == 0
        monkeypatch.setattr(job, "audit", original)
        job.recover()
        assert job.summary()["counts"]["unknown"] == 20
        assert job.summary()["phases"]["pilot"]["state"] == "Cooldown"
    else:
        restrict(job)
        if transition != "cooldown_review":
            review(job)
            recovery.wait_reviewed(job)
        if transition == "cooldown_authorization_consumed":
            token = recovery.authorize(job, recovery.authorization_review(job))
        before = table_rows(job)
        monkeypatch.setattr(job, "audit", audit)
        with pytest.raises(sqlite3.OperationalError):
            if transition == "cooldown_review":
                review(job)
            elif transition == "cooldown_authorization":
                recovery.authorize(job, recovery.authorization_review(job))
            else:
                job.run("pilot", token)
        assert table_rows(job) == before
    assert len(job.all_calls) == 1 and not job.database.exists()


def test_live_guard_default_and_historical_manifest_capability_are_preserved(job):
    assert settings()["batch_size"] == 20 and settings()["delay"] == 10
    job.prepare(job.test_source, batch_size=21)
    with pytest.raises(ValueError):
        execute(job)
    assert not job.test_calls


def crash_at_checkpoint(job, point):
    job.prepare(job.test_source, batch_size=2, pilot_names=5)
    script = """
import os,socket,sys
from datetime import UTC,datetime,timedelta
from username_finder.roblox_prefilter import Prefilter
from username_finder.roblox_protocol import Response
from username_finder.roblox_activity import RobloxActivity
from username_finder import roblox_prefilter_cooldown as recovery
def deny(*a,**kw): raise AssertionError("No network")
socket.socket.connect=socket.create_connection=deny
tick=[0.0]
def wait(n): tick[0]+=n; return False
tool=Prefilter(sys.argv[1],sys.argv[2], clock=lambda:tick[0],
    wall=lambda:datetime(2026,11,8,tzinfo=UTC)+timedelta(seconds=tick[0]),wait=wait,
    transport=lambda _:Response(status=429,body=b'{"errors":[]}',complete=True,
        headers={"content-type":"application/json","retry-after":"2"}))
point=sys.argv[3]
if point=='raw': tool._finish=lambda *a:os._exit(77)
checkpoint=RobloxActivity.checkpoint_cooldown
def boundary(self,owner,restriction,state):
    if point=='decision' and state=='Hold': os._exit(77)
    if point=='consumed' and state=='Released': os._exit(77)
    checkpoint(self,owner,restriction,state)
    if point=='hold' and state=='Hold': os._exit(77)
    if point=='released' and state=='Released': os._exit(77)
RobloxActivity.checkpoint_cooldown=boundary
tool.run('pilot',tool.review('pilot'))
s=tool.summary(); recovery.review(tool,s['cooldown']['restriction'],s['revision'])
recovery.wait_reviewed(tool)
token=recovery.authorize(tool,recovery.authorization_review(tool))
tool.run('pilot',token)
raise AssertionError('Fault did not execute')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(job.directory), str(job.database), point],
        capture_output=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 77, result.stderr.decode(errors="replace")


@pytest.mark.parametrize("point", ["raw", "decision", "hold", "consumed", "released"])
def test_process_death_at_cross_store_checkpoints_recovers_without_dispatch(job, point):
    crash_at_checkpoint(job, point)
    before = table_rows(job)["requests"]
    recovered = job.recover()
    assert recovered["counts"]["unknown"] == 2 and recovered["counts"]["attempts"] == 1
    assert not job.test_calls and not job.database.exists()
    assert len(table_rows(job)["requests"]) == len(before) == 1
    if point in ("consumed", "released"):
        assert recovered["phases"]["pilot"]["state"] == "Paused"
        assert recovered["shared_cooldown"]["state"] == "Released"
        original = table_rows(job)
        pending = RobloxActivity(job.database).pending.read_bytes()
        job.recover()
        assert table_rows(job) == original and not job.test_calls
        assert RobloxActivity(job.database).pending.read_bytes() == pending
        with pytest.raises(ValueError):
            job.run("pilot", "already-consumed")
        completed = execute(job)
        assert completed["phases"]["pilot"]["state"] == "Completed"
        assert completed["counts"]["unknown"] == 2 and completed["counts"]["excluded"] == 3
        requests = table_rows(job)["requests"]
        assert requests[0] == original["requests"][0]
        names = [name for row in requests for name in json.loads(row[3])]
        assert len(names) == len(set(names)) == 5
    else:
        assert recovered["phases"]["pilot"]["state"] == "Cooldown"
        assert recovered["shared_cooldown"]["state"] == "Hold"
        with pytest.raises(ValueError):
            execute(job)


@pytest.mark.parametrize("transition", ["Hold", "Released"])
def test_cooldown_journal_io_failure_is_fail_closed(job, monkeypatch, transition):
    checkpoint = RobloxActivity.checkpoint_cooldown

    def fail(self, owner, restriction, state):
        if state == transition:
            raise OSError("Injected journal failure")
        checkpoint(self, owner, restriction, state)

    if transition == "Hold":
        monkeypatch.setattr(RobloxActivity, "checkpoint_cooldown", fail)
        stopped = restrict(job)
        assert stopped["phases"]["pilot"]["state"] == "ReviewRequired"
    else:
        restrict(job)
        token = approval(job)
        monkeypatch.setattr(RobloxActivity, "checkpoint_cooldown", fail)
        with pytest.raises(OSError):
            job.run("pilot", token)
    with pytest.raises(ValueError):
        with RobloxActivity(job.database):
            pass
    assert len(job.all_calls) == 1 and job.summary()["counts"]["unknown"] == 20


def test_legacy_capability_never_silently_enrolls_in_recovery(job):
    job.prepare(job.test_source)
    manifest = job.manifest()
    manifest.pop("fingerprint")
    manifest.pop("cooldown_recovery")
    from username_finder import roblox_bulk_protocol as bulk

    manifest["fingerprint"] = bulk.digest(bulk.encoded(manifest))
    job.manifest_path.write_bytes(bulk.encoded(manifest))
    with job.connect() as db:
        meta = job.meta(db)
        meta["fingerprint"] = manifest["fingerprint"]
        job.save(db, meta)
    job.transport = lambda _: response(
        status=429, headers={"content-type": "application/json", "retry-after": "2"}
    )
    before = job.manifest_path.read_bytes()
    stopped = execute(job)
    assert stopped["phases"]["pilot"]["state"] == "Restricted"
    assert not stopped.get("cooldown") and RobloxActivity(job.database).block.read_bytes() == b"http_429"
    assert job.manifest_path.read_bytes() == before


@pytest.mark.parametrize("header", ["0", "Sun, 08 Nov 2026 00:00:02 GMT"])
def test_operational_http_date_and_zero_deadlines_still_require_both_actions(job, header):
    restrict(job, header=header)
    with pytest.raises(ValueError):
        recovery.authorization_review(job)
    token = approval(job)
    assert len(job.all_calls) == 1
    assert job.run("pilot", token)["counts"]["unknown"] == 20


def test_issued_authorization_is_invalidated_by_any_later_revision(job):
    restrict(job)
    token = approval(job)
    with job.connect() as db:
        meta = job.meta(db)
        job.audit(db, "test_intervening_change", {})
        job.save(db, meta)
    with pytest.raises(ValueError):
        job.run("pilot", token)
    assert len(job.all_calls) == 1


@pytest.mark.parametrize("automatic", [False, True])
def test_durable_cooldown_blocks_individual_and_background_entrypoints(job, automatic):
    from username_finder.roblox_runner import RobloxRunner
    from username_finder.roblox_storage import RobloxStore
    from username_finder.storage import Store

    restrict(job)
    with closing(Store(job.database)) as store:
        repo = RobloxStore(store)
        identity = repo.create_run(["ValidName"])
        before = repo.progress(identity)
        runner = RobloxRunner(repo, fetcher=lambda _: pytest.fail("No individual or background request"))
        with pytest.raises(ValueError):
            runner.run(identity, automatic=automatic)
        assert repo.progress(identity) == before


@pytest.mark.parametrize("raw", [b"", b'{"state":"Released"}\n', b"[]\n", b"not-json\n"])
def test_partial_or_malformed_shared_cooldown_never_unlocks(job, raw):
    restrict(job)
    marker = RobloxActivity(job.database).cooldown
    marker.write_bytes(raw)
    with pytest.raises(ValueError):
        with RobloxActivity(job.database):
            pass
    assert marker.read_bytes() == raw


def test_small_clock_forward_drift_cannot_skip_monotonic_wait(job):
    restrict(job)
    review(job)
    original = job.wall
    job.test_clock.at += 1.75
    job.wall = lambda: original() + timedelta(seconds=0.5)
    with pytest.raises(ValueError):
        recovery.authorization_review(job)
    recovery.wait_reviewed(job)
    assert len(job.all_calls) == 1 and job.test_clock.at >= 2.25


def test_owned_pending_sentinel_also_blocks_older_checker_until_authorization_consumed(job):
    restrict(job)
    activity = RobloxActivity(job.database)
    pending = activity.pending.read_bytes().splitlines()[-1]
    assert pending.startswith(b"Pending:") and not job._can_clear_pending(pending)
    token = approval(job)
    assert activity.pending.read_bytes().splitlines()[-1] == pending
    job.run("pilot", token)
    assert activity.pending.read_bytes().endswith(b"Clear\n")


@pytest.fixture
def released_job(job):
    crash_at_checkpoint(job, "released")
    assert RobloxActivity(job.database).pending.read_bytes().splitlines()[-1].startswith(b"Pending:")
    return job


@pytest.mark.parametrize(
    "mismatch",
    [
        "operation",
        "request",
        "partial_pending",
        "crlf_pending",
        "control_pending",
        "malformed_pending",
        "unowned_pending",
        "restriction",
        "released_owner",
        "malformed_shared",
        "uncertain",
        "unconsumed",
        "missing_consumption",
        "original_authorization",
        "historical_block",
    ],
)
def test_released_reconciliation_rejects_unrelated_or_unproven_markers(released_job, mismatch):
    job = released_job
    guard = RobloxActivity(job.database)
    if mismatch in (
        "operation",
        "request",
        "partial_pending",
        "crlf_pending",
        "control_pending",
        "malformed_pending",
        "unowned_pending",
    ):
        expected = guard.pending.read_bytes().splitlines()[-1]
        raw = {
            "operation": b"Pending:" + b"b" * 64 + b":1\n",
            "request": b"Pending:" + job.manifest()["fingerprint"].encode() + b":2\n",
            "partial_pending": expected,
            "crlf_pending": expected + b"\r\n",
            "control_pending": b"broken\r" + expected + b"\n",
            "malformed_pending": expected + b"\nGarbage\n",
            "unowned_pending": b"Pending\n",
        }[mismatch]
        guard.pending.write_bytes(raw)
    elif mismatch in ("restriction", "released_owner", "malformed_shared"):
        checkpoint = guard.cooldown_state()
        if mismatch == "restriction":
            checkpoint["restriction"] = "b" * 64
        elif mismatch == "released_owner":
            checkpoint["owner"] = "b" * 64 + ":" + "c" * 64
        guard.cooldown.write_bytes(
            b"broken\n" if mismatch == "malformed_shared" else json.dumps(checkpoint).encode() + b"\n"
        )
    elif mismatch == "historical_block":
        guard.block.write_bytes(b"http_429")
    else:
        with job.connect() as db:
            if mismatch == "uncertain":
                db.execute("UPDATE requests SET state='Uncertain'")
            elif mismatch == "missing_consumption":
                db.execute("DELETE FROM audit WHERE event='cooldown_authorization_consumed'")
            elif mismatch == "original_authorization":
                row = db.execute("SELECT id,data FROM audit WHERE event='cooldown_authorization'").fetchone()
                data = json.loads(row["data"])
                data["authorization_hash"] = "b" * 64
                db.execute("UPDATE audit SET data=? WHERE id=?", (json.dumps(data), row["id"]))
            else:
                meta = job.meta(db)
                meta["cooldown"]["stage"] = "Authorized"
                job.save(db, meta)
    pending, checkpoint, evidence = guard.pending.read_bytes(), guard.cooldown.read_bytes(), table_rows(job)
    with job.connect(read_only=True) as db:
        audit = list(db.execute("SELECT * FROM audit"))
    with pytest.raises(ValueError):
        job.recover()
    assert guard.pending.read_bytes() == pending and guard.cooldown.read_bytes() == checkpoint
    assert table_rows(job) == evidence and not job.test_calls and not job.database.exists()
    with job.connect(read_only=True) as db:
        assert list(db.execute("SELECT * FROM audit")) == audit
    if mismatch == "historical_block":
        assert guard.block.read_bytes() == b"http_429"


def test_released_reconciliation_preserves_old_policy_without_mutation(released_job):
    job = released_job
    mark_old_policy(job)
    guard = RobloxActivity(job.database)
    before = (
        job.path.read_bytes(),
        job.manifest_path.read_bytes(),
        guard.pending.read_bytes(),
        guard.cooldown.read_bytes(),
    )
    job.recover()
    assert before == (
        job.path.read_bytes(),
        job.manifest_path.read_bytes(),
        guard.pending.read_bytes(),
        guard.cooldown.read_bytes(),
    )
    assert not job.test_calls


@pytest.mark.parametrize("point", ["before_clear", "after_clear"])
def test_second_process_death_during_pending_reconciliation_is_idempotent(released_job, point):
    job = released_job
    before = table_rows(job)
    guard = RobloxActivity(job.database)
    checkpoint, manifest = guard.cooldown.read_bytes(), job.manifest_path.read_bytes()
    script = """
import os,socket,sys
from username_finder.roblox_prefilter import Prefilter
from username_finder.roblox_activity import RobloxActivity
def deny(*a,**kw): raise AssertionError("No dispatch during recovery")
socket.socket.connect=socket.create_connection=deny
clear=RobloxActivity.clear_pending
def boundary(self):
    if sys.argv[3]=='before_clear': os._exit(78)
    clear(self)
    os._exit(78)
RobloxActivity.clear_pending=boundary
Prefilter(sys.argv[1],sys.argv[2],transport=deny).recover()
raise AssertionError('Recovery fault did not execute')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(job.directory), str(job.database), point],
        capture_output=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 78, result.stderr.decode(errors="replace")
    job.recover()
    assert not job.test_calls and table_rows(job) == before
    assert guard.pending.read_bytes().endswith(b"Clear\n")
    pending, database = guard.pending.read_bytes(), job.path.read_bytes()
    job.recover()
    assert guard.pending.read_bytes() == pending and job.path.read_bytes() == database
    assert guard.cooldown.read_bytes() == checkpoint and job.manifest_path.read_bytes() == manifest
    done = execute(job)
    assert done["counts"]["unknown"] == 2 and done["counts"]["excluded"] == 3
    assert done["phases"]["pilot"]["state"] == "Completed"
    requests = table_rows(job)["requests"]
    assert requests[0] == before["requests"][0]
    names = [name for row in requests for name in json.loads(row[3])]
    assert len(names) == len(set(names)) == 5


def test_newer_uncertain_request_is_not_cleared_as_the_released_429(released_job):
    job = released_job
    guard = RobloxActivity(job.database)
    with job.connect() as db:
        db.execute(
            "INSERT INTO requests(phase,state,names,request_at) VALUES('pilot','Intent',?,?)",
            (json.dumps(["User00002"]), job.wall().isoformat()),
        )
        db.execute("UPDATE names SET state='InFlight',request_id=2 WHERE username='User00002'")
    with guard.pending.open("ab") as stream:
        stream.write(("Pending:" + job.manifest()["fingerprint"] + ":2\n").encode())
    pending, first = guard.pending.read_bytes(), table_rows(job)["requests"][0]
    with pytest.raises(ValueError):
        job.recover()
    assert guard.pending.read_bytes() == pending and not job.test_calls
    requests = table_rows(job)["requests"]
    assert requests[0] == first and requests[-1][2] == "Uncertain"


def test_released_reconciliation_still_requires_the_shared_activity_lock(released_job):
    job = released_job
    guard = RobloxActivity(job.database)
    pending = guard.pending.read_bytes()
    with recovery.shared(job):
        with pytest.raises(ValueError):
            job.recover()
    assert guard.pending.read_bytes() == pending and not job.test_calls
