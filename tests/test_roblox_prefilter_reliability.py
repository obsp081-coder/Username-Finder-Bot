"""Offline restriction, crash timing, and disk failure regressions."""

import base64
import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_roblox_bulk import record, response
from test_roblox_prefilter import execute
from test_roblox_prefilter import job as job
from test_roblox_prefilter import no_live as no_live

from username_finder.roblox_activity import RobloxActivity
from username_finder.roblox_prefilter import Prefilter


def test_reported_429_pattern_is_durable_and_blocks_restart(job, tmp_path):
    job.test_source.write_text("\n".join(f"User{i:05}" for i in range(500)), encoding="utf8")
    job.prepare(job.test_source, batch_size=20, delay=0.001, pilot_names=100)
    calls = []
    raw = b'{"errors":[{"code":0,"message":"Too many requests"}]}'

    def fetch(request):
        calls.append(request)
        job.test_clock.at += 0.25
        if len(calls) == 22:
            return response(status=429, body=raw, headers={"retry-after": "10"})
        rows = [record(n, uid=int(n[4:]) + 1) for n in request["usernames"]]
        return response(rows[:-1] if len(calls) == 21 else rows)

    job.transport = fetch
    assert execute(job)["phases"]["pilot"]["state"] == "Completed"
    result = execute(job, "full")
    assert len(calls) == 22
    counts = result["counts"]
    assert (counts["processed"], counts["excluded"], counts["unknown"]) == (440, 419, 20)
    assert counts["failed_requests"] == 1
    assert sum(q["safe_responses"] for q in result["quality"].values()) == 21
    assert result["phases"]["full"]["stop_reason"] == "http_429"
    assert result["phases"]["full"]["state"] == "Restricted"
    with job.connect() as db:
        row = db.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()
        evidence = json.loads(row["evidence"])
        assert base64.b64decode(evidence["body_base64"]) == raw
        assert evidence["headers"]["retry-after"] == "10"
        assert db.execute("SELECT count(*) FROM names WHERE reason='http_429'").fetchone()[0] == 20
        before = [tuple(r) for r in db.execute("SELECT * FROM requests ORDER BY id")]
    marker = RobloxActivity(job.database).block
    marker_bytes = marker.read_bytes()
    restarted = Prefilter(job.directory, job.database, transport=lambda _: pytest.fail("No replay"))
    restored = restarted.recover()
    assert restored["counts"] == counts
    for phase in ("pilot", "full"):
        with pytest.raises(ValueError):
            restarted.review(phase)
    fresh = Prefilter(tmp_path / "new-run", job.database, transport=lambda _: pytest.fail("No bypass"))
    fresh.prepare(job.test_source)
    with pytest.raises(ValueError, match="Bulk"):
        execute(fresh)
    with pytest.raises(ValueError):
        restarted.review_uncertain(restarted.manifest()["fingerprint"])
    with restarted.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM requests ORDER BY id")] == before
    assert marker.read_bytes() == marker_bytes
    exported = restarted.export(tmp_path / "restricted-export")
    assert json.loads((exported / "export-state.json").read_bytes())["complete"]
    assert len((exported / "individual-candidates.txt").read_text().splitlines()) == 81


@pytest.mark.parametrize("restricted", [False, True])
def test_crash_after_durable_response_never_claims_complete_timing(job, restricted):
    job.prepare(job.test_source, batch_size=2, pilot_names=4)
    script = """
import os,socket,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_roblox_bulk import Clock,record,response
from username_finder.roblox_prefilter import Prefilter
socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('No live'))
clock=Clock();calls=[]
def fetch(request):
    calls.append(request);clock.at+=0.25
    if len(calls)==2 and sys.argv[3]=='True':return response(status=429)
    return response([record(n,uid=int(n[4:])+1) for n in request['usernames']])
tool=Prefilter(sys.argv[1],sys.argv[2],transport=fetch,clock=clock.now,wall=clock.wall,wait=clock.wait)
finish=tool._finish
def stop(db,request,response,meta):
    if request['id']==2:os._exit(73)
    return finish(db,request,response,meta)
tool._finish=stop
tool.run('pilot',tool.review('pilot'))
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(job.directory), str(job.database), str(restricted)],
        capture_output=True,
        timeout=20,
    )
    assert child.returncode == 73, child.stderr.decode(errors="replace")
    with job.connect() as db:
        before = db.execute("SELECT evidence FROM requests WHERE id=2").fetchone()[0]
    restored = job.recover()
    assert restored["phases"]["pilot"]["state"] == ("Restricted" if restricted else "Completed")
    assert restored["phases"]["pilot"].get("elapsed_incomplete") is True
    assert restored["quality"]["pilot"]["names_per_minute"] is None
    with job.connect() as db:
        assert db.execute("SELECT evidence FROM requests WHERE id=2").fetchone()[0] == before
    assert not job.test_calls


@pytest.mark.parametrize("sqlite_rolled_back", [False, True])
def test_import_sql_failure_rolls_back_counts_to_last_durable_checkpoint(
    job, monkeypatch, sqlite_rolled_back
):
    job.test_source.write_text("\n".join(f"User{i:05}" for i in range(2500)), encoding="utf8")
    connect = job.connect
    insertions = 0

    class FailingConnection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, sql, args=()):
            nonlocal insertions
            if sql.startswith("INSERT OR IGNORE INTO names"):
                insertions += 1
                if insertions == 1501:
                    if sqlite_rolled_back:
                        self.db.rollback()
                    raise sqlite3.OperationalError("database or disk is full (injected)")
            return self.db.execute(sql, args)

    @contextmanager
    def fail(*args, **kwargs):
        with connect(*args, **kwargs) as db:
            yield FailingConnection(db)

    with monkeypatch.context() as guard:
        guard.setattr(job, "connect", fail)
        interrupted = job.prepare(job.test_source)
    assert interrupted["import_state"] == "ImportError"
    assert "disk is full" in interrupted["import_error"]
    assert interrupted["cursor"] == interrupted["counts"]["imported"] == 1000
    with job.connect() as db:
        assert db.execute("SELECT count(*) FROM names").fetchone()[0] == 1000
    manifest = job.manifest_path.read_bytes()
    restored = job.prepare(job.test_source)
    assert restored["import_state"] == "Ready"
    assert restored["counts"]["imported"] == restored["counts"]["unique"] == restored["cursor"] == 2500
    assert restored["counts"]["duplicates"] == 0
    assert job.manifest_path.read_bytes() == manifest and not job.test_calls


def test_disk_full_during_export_never_marks_partial_file_complete(job, tmp_path, monkeypatch):
    job.prepare(job.test_source)
    output = tmp_path / "disk-failure-export"
    open_path = Path.open

    def fail(path, *args, **kwargs):
        if path.parent == output and path.name.startswith("candidates.csv."):
            raise OSError("No space left on device (injected)")
        return open_path(path, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(Path, "open", fail)
        with pytest.raises(OSError, match="space"):
            job.export(output)
    state = json.loads((output / "export-state.json").read_bytes())
    assert not state["complete"] and "candidates.csv" not in state["files"]
    completed = {p.name: p.read_bytes() for p in output.iterdir() if p.name in state["files"]}
    job.export(output)
    assert json.loads((output / "export-state.json").read_bytes())["complete"]
    assert all((output / name).read_bytes() == raw for name, raw in completed.items())
    assert not job.test_calls


def test_offline_benchmark_small_run_is_reproducible_and_network_free(tmp_path):
    output = tmp_path / "measurement"
    child = subprocess.run(
        [
            sys.executable,
            "tools/roblox_prefilter_offline_benchmark.py",
            "--rows",
            "100",
            "--output",
            str(output),
        ],
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr.decode(errors="replace")
    report = json.loads((output / "benchmark.json").read_bytes())
    assert report["rows"] == 100 and report["status"] == "complete"
    assert report["provider_request_attempts"] == 0
    assert report["export_files"] == 12 and report["calculation_iterations"] == 10_000
    assert report["import_cpu_seconds"] >= 0 and report["export_cpu_seconds"] >= 0
    assert report["process_cpu_seconds"] >= report["import_cpu_seconds"]
    assert not (output / "production-must-not-exist.db").exists()
