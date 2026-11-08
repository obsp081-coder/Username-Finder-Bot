import json
import sqlite3
from types import SimpleNamespace

import pytest
from test_overnight import execute
from test_overnight import rigs as rigs

from username_finder.overnight_preflight import JobIntent, check_job, probe_storage


def assess(rig, **kwargs):
    return check_job(rig.repo, rig.night, JobIntent(rig.platform, rig.identity), **kwargs)


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_local_readiness_never_mutates_or_dispatches(rigs, platform):
    rig = rigs(platform)
    before = list(rig.store.db.iterdump())
    result = assess(rig)
    assert not result.errors and result.verdict == ("جاهزة" if platform == "Roblox" else "جاهزة مع تنبيه")
    assert "5" in result.text() and rig.identity in result.text()
    assert not rig.calls
    assert list(rig.store.db.iterdump()) == before
    assert not rig.night.get(rig.identity)["auto_resume"]


@pytest.mark.parametrize("platform", ["Roblox", "X"])
@pytest.mark.parametrize(
    "case", ["missing", "disabled", "stopped", "completed", "restricted", "active", "empty"]
)
def test_unsafe_intents_are_blocked(rigs, platform, case):
    rig = rigs(platform)
    if case == "missing":
        rig.identity = None
    elif case == "disabled":
        rig.night.enable(rig.identity, False)
    elif case == "stopped":
        rig.night.stop(rig.identity)
    elif case == "completed":
        execute(rig)
        rig.calls.clear()
    elif case == "restricted":
        with rig.store.transaction() as db:
            prefix = rig.night.prefix
            db.execute(f"UPDATE {prefix}_control SET value=?", (json.dumps({"blocked": "test_429"}),))
    elif case == "active":
        rig.repo.start(rig.identity)
    else:
        with rig.store.transaction() as db:
            db.execute(f"DELETE FROM {rig.night.prefix}_queue WHERE run_id=?", (rig.identity,))
    assert assess(rig).errors
    assert not rig.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempts", 13),
        ("attempts", True),
        ("candidate_retries", 4),
        ("retries_performed", -1),
        ("retry_at", "broken"),
        ("retry_at", "2026-11-08T00:00:00"),
        ("state", "unexpected"),
    ],
)
def test_corrupt_recovery_metadata_is_not_ready(rigs, field, value):
    rig = rigs("Roblox")
    data = rig.night.get(rig.identity)
    data[field] = value
    with rig.store.transaction() as db:
        db.execute("UPDATE overnight_jobs SET data=?", (json.dumps(data),))
    assert assess(rig).errors


def test_storage_lock_blocks_without_changing_evidence_and_recovers(rigs):
    rig = rigs("Roblox")
    before = list(rig.store.db.iterdump())
    with sqlite3.connect(rig.store.path) as external:
        external.execute("BEGIN IMMEDIATE")
        assert assess(rig).errors
        external.rollback()
    assert not assess(rig).errors
    assert list(rig.store.db.iterdump()) == before


@pytest.mark.parametrize(
    "free,blocked,warned", [(1, True, False), (32 * 1024**2, False, True), (1024**3, False, False)]
)
def test_storage_capacity_thresholds_are_explicit(rigs, free, blocked, warned):
    rig = rigs("Roblox")
    errors, warnings = probe_storage(rig.store.path, disk_usage=lambda _: SimpleNamespace(free=free))
    assert bool(errors) is blocked and bool(warnings) is warned


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    assert probe_storage(path)[0] and not path.exists()


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_saved_waits_warn_without_clearing_them(rigs, platform):
    rig = rigs(platform)
    prefix = rig.night.prefix
    with rig.store.transaction() as db:
        db.execute(
            f"UPDATE {prefix}_control SET value=?", (json.dumps({"not_before": "2099-01-01T00:00:00+00:00"}),)
        )
    result = assess(rig)
    assert not result.errors and any("2099" in warning for warning in result.warnings)
    assert rig.repo.control()["not_before"].startswith("2099")


def test_x_post_release_gate_cannot_be_bypassed_by_preflight(rigs):
    rig = rigs("X")
    with rig.store.transaction() as db:
        db.execute(
            "UPDATE x_control SET value=?",
            (
                json.dumps(
                    {
                        "recovery_gate": {
                            "last_run_rowid": 999,
                            "connection": {"mode": "direct", "proxy_url": ""},
                        }
                    }
                ),
            ),
        )
    assert assess(rig).errors


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_invalid_pacing_and_malformed_deadlines_are_blocked(rigs, platform):
    rig = rigs(platform)
    with rig.store.transaction() as db:
        db.execute(f"UPDATE {rig.night.prefix}_runs SET interval_seconds=0.3")
    assert assess(rig).errors


def test_ui_busy_snapshot_blocks_an_as_yet_unstarted_worker(rigs):
    rig = rigs("Roblox")
    intent = JobIntent("Roblox", rig.identity, busy=True)
    assert check_job(rig.repo, rig.night, intent).errors
