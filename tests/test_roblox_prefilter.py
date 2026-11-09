import hashlib
import json
import socket
import sqlite3
import subprocess
import sys

import pytest
from test_roblox_bulk import Clock, record, response

from username_finder import roblox_bulk_protocol as bulk
from username_finder.roblox_activity import RobloxActivity
from username_finder.roblox_prefilter import MATCH, Prefilter, settings


@pytest.fixture(autouse=True)
def no_live(monkeypatch):
    def blocked(*a, **kw):
        raise AssertionError("No live requests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def job(tmp_path):
    path = tmp_path / "names.txt"
    path.write_text("\n".join(f"User{i:05}" for i in range(45)), encoding="utf-8")
    clock, calls = Clock(), []

    def fetch(request):
        calls.append((clock.now(), request))
        clock.at += 0.25
        return response([record(n, uid=int(n[4:]) + 1) for n in request["usernames"]])

    tool = Prefilter(
        tmp_path / "run",
        tmp_path / "production.db",
        transport=fetch,
        clock=clock.now,
        wall=clock.wall,
        wait=clock.wait,
    )
    tool.test_calls, tool.test_clock, tool.test_source = calls, clock, path
    return tool


def execute(tool, phase="pilot"):
    return tool.run(phase, tool.review(phase))


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "10", None])
def test_bad_delays(value):
    with pytest.raises(ValueError):
        settings(delay=value)


@pytest.mark.parametrize("size", [0, 1001, -1, True, 1.5, "20"])
def test_bad_sizes(size):
    with pytest.raises(ValueError):
        settings(batch_size=size)


def test_default_settings_and_no_requests_offline(job):
    assert settings() == {"delay": 10.0, "batch_size": 20, "pilot_names": 100}
    summary = job.prepare(job.test_source)
    assert summary["counts"]["unique"] == summary["counts"]["valid"] == 45
    assert summary["planned_requests"] == 3
    assert len(summary["samples"]) == 30
    assert not job.test_calls and not job.database.exists()
    original = job.manifest_path.read_bytes()
    job.prepare(job.test_source, delay=1, batch_size=1)
    assert job.manifest_path.read_bytes() == original
    assert job.summary()["manifest"]["settings"]["delay"] == 10


@pytest.mark.parametrize("size", [1, 2, 7, 20])
def test_batches_pacing_pilot_boundary_and_explicit_full(job, size):
    job.prepare(job.test_source, batch_size=size, delay=1.5, pilot_names=21)
    with pytest.raises(ValueError):
        job.review("full")
    with pytest.raises(ValueError):
        job.run("pilot", "")
    result = execute(job)
    assert result["phases"]["pilot"]["state"] == "Completed"
    assert "full" not in result["phases"]
    assert result["counts"]["processed"] == result["counts"]["excluded"] == 21
    pilot_requests = (21 + size - 1) // size
    assert len(job.test_calls) == pilot_requests
    assert len(job.test_clock.waits) == pilot_requests - 1
    assert all(d == 1.5 for d in job.test_clock.waits)
    result = execute(job, "full")
    assert result["counts"]["processed"] == result["counts"]["excluded"] == 45
    names = [n for _, r in job.test_calls for n in r["usernames"]]
    assert len(names) == len(set(names)) == 45
    for (start, req), (next_start, _) in zip(job.test_calls, job.test_calls[1:], strict=False):
        assert len(req["usernames"]) <= size
        assert next_start - start == 1.75
    assert result["quality"]["pilot"]["pilot_exclusion_fraction"] == 1
    assert result["quality"]["pilot"]["safe_responses"] == pilot_requests
    with pytest.raises(ValueError):
        job.review("pilot")


@pytest.mark.parametrize(
    "extension,text",
    [
        ("txt", " NameOne \nnameone\ninvalid-name\n\n@formula\nNameTwo\n"),
        (
            "csv",
            "username,extra\n NameOne ,first\nnameone,duplicate\ninvalid-name,bad\n"
            "@formula,bad\nNameTwo,last\n",
        ),
    ],
)
def test_stream_parsing_dedup_invalid_and_source(job, extension, text):
    source = job.test_source.with_suffix("." + extension)
    source.write_text(text, encoding="utf-8-sig")
    before = source.read_bytes()
    summary = job.prepare(source)
    c = summary["counts"]
    assert c["unique"] == 4 and c["duplicates"] == 1 and c["invalid"] == 2 and c["valid"] == 2
    assert summary["samples"][0]["username"] == "NameOne"
    assert summary["samples"][0]["source_record"] == 1
    assert summary["manifest"]["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert source.read_bytes() == before and not job.test_calls


def test_invalid_unicode_casefold_cannot_hide_a_valid_ascii_candidate(job):
    job.test_source.write_text("\u212aelvin\nkelvin\nKELVIN\n", encoding="utf-8")
    result = job.prepare(job.test_source)
    assert result["counts"]["invalid"] == 1 and result["counts"]["valid"] == 1
    assert result["counts"]["duplicates"] == 1 and result["counts"]["unique"] == 2


@pytest.mark.parametrize("text", ["", "\n \n", "a\n-\n", "username,username\naaa,bbb\n"])
def test_empty_invalid_and_malformed_inputs_never_dispatch(job, text):
    file = job.test_source.with_suffix(".csv" if "," in text else ".txt")
    file.write_text(text, encoding="utf-8")
    job.prepare(file)
    with pytest.raises(ValueError):
        job.review("pilot")
    assert not job.test_calls


def test_cancel_import_resume_and_preserve_manifest(job):
    with job.test_source.open("w", encoding="utf-8") as f:
        for i in range(2501):
            f.write(f"User{i:05}\n")
    job.updated = job.cancel.set
    paused = job.prepare(job.test_source)
    assert paused["import_state"] == "ImportPaused" and paused["counts"]["unique"] == 1000
    frozen = job.manifest_path.read_bytes()
    job.cancel.clear()
    job.updated = lambda: None
    done = job.prepare()
    assert done["counts"]["unique"] == 2501 and done["counts"]["duplicates"] == 0
    assert frozen == job.manifest_path.read_bytes() and not job.test_calls


def test_cancellation_during_pacing_restart_preserves_settings(job):
    job.prepare(job.test_source, batch_size=2, pilot_names=5, delay=0.5)
    job.wait = lambda _: True
    paused = execute(job)
    assert paused["phases"]["pilot"]["state"] == "Paused" and len(job.test_calls) == 1
    restored = Prefilter(
        job.directory,
        job.database,
        transport=job.transport,
        clock=job.clock,
        wall=job.wall,
        wait=job.test_clock.wait,
    )
    restored.recover()
    execute(restored)
    assert len(job.test_calls) == 3
    assert len(set(n for _, r in job.test_calls for n in r["usernames"])) == 5
    assert restored.manifest()["settings"]["delay"] == 0.5


@pytest.mark.parametrize(
    "changes",
    [
        {"status": 403},
        {"status": 429},
        {"status": 401},
        {"status": 500},
        {"challenge": True},
        {"error": "timeout", "complete": False},
        {"headers": {"content-type": "application/json", "retry-after": "100"}},
        {"body": b"bad"},
        {"body": b'{"data":[],"data":[]}'},
    ],
)
def test_failure_restrictions_stop_and_do_not_replay(job, changes):
    job.prepare(job.test_source, pilot_names=10, batch_size=2)
    calls = []
    job.transport = lambda r: calls.append(r) or response(**changes)
    result = execute(job)
    assert len(calls) == 1 and result["counts"]["failed_requests"] == 1
    assert result["phases"]["pilot"]["state"] == "Restricted"
    for phase in ("pilot", "full"):
        with pytest.raises(ValueError):
            job.review(phase)
    with pytest.raises(ValueError):
        with RobloxActivity(job.database):
            pass
    with pytest.raises(ValueError):
        job.review_uncertain(job.manifest()["fingerprint"])


def test_uncertain_delivery_requires_review_and_is_never_replayed(job):
    job.prepare(job.test_source, batch_size=2, pilot_names=5)
    actual = job.transport
    job.transport = lambda r: (_ for _ in ()).throw(OSError("uncertain"))
    execute(job)
    report = job.recover()
    assert report["counts"]["unknown"] == 2
    assert report["phases"]["pilot"]["state"] == "ReviewRequired"
    with pytest.raises(ValueError):
        job.review("pilot")
    with pytest.raises(ValueError):
        with RobloxActivity(job.database):
            pass
    job.review_uncertain(job.manifest()["fingerprint"])
    job.transport = actual
    result = execute(job)
    assert result["phases"]["pilot"]["state"] == "Completed"
    assert len(job.test_calls) == 2
    assert not {"User00000", "User00001"} & {n for _, r in job.test_calls for n in r["usernames"]}
    assert result["counts"]["unknown"] == 2


def test_durable_intent_raw_before_classification_and_response_recovery(job, monkeypatch):
    job.prepare(job.test_source, batch_size=2, pilot_names=5)
    fetch = job.transport

    def network(request):
        with job.connect() as db:
            row = db.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()
            assert row["state"] == "Intent" and row["evidence"] is None
        return fetch(request)

    job.transport = network
    actual = job._finish

    def interrupted(db, request, response, meta):
        with job.connect() as other:
            assert other.execute("SELECT evidence FROM requests").fetchone()[0]
        raise RuntimeError("crash after durable raw")

    monkeypatch.setattr(job, "_finish", interrupted)
    execute(job)
    assert len(job.test_calls) == 1
    monkeypatch.setattr(job, "_finish", actual)
    recovered = job.recover()
    assert recovered["counts"]["excluded"] == 2
    assert recovered["phases"]["pilot"]["state"] == "Paused"
    execute(job)
    assert len(job.test_calls) == 3


def test_historical_missing_and_current_evidence_export_separately(job, tmp_path):
    job.prepare(job.test_source, pilot_names=3, batch_size=3)
    job.transport = lambda r: response(
        [record(r["usernames"][0], uid=10), record(r["usernames"][1], "different_current", uid=11)]
    )
    execute(job)
    report = job.summary()
    assert report["counts"]["excluded"] == 1 and report["remaining_candidates"] == 44
    out = job.export(tmp_path / "exports")
    current = json.loads((out / "current.json").read_text(encoding="utf-8"))
    candidates = json.loads((out / "candidates.json").read_text(encoding="utf-8"))
    assert len(current) == 1 and len(candidates) == 44
    assert current[0]["state"] == MATCH
    assert candidates[0]["state"] == "HistoricalOrDifferentCurrentName"
    assert candidates[1]["state"] == "NoMatchingBulkRecord"
    assert all(row["state"] not in ("Taken", "Available") for row in current + candidates)
    assert len((out / "individual-candidates.txt").read_text().splitlines()) == 44
    requests = json.loads((out / "requests.json").read_text())
    assert json.loads(requests[0]["evidence"])["body_base64"]
    assert json.loads((out / "export-state.json").read_text())["complete"]


def test_cross_batch_conflict_audits_original_and_revokes_exclusion(job):
    job.prepare(job.test_source, batch_size=1, pilot_names=3)
    job.transport = lambda r: response([record(r["usernames"][0], uid=1)])
    report = execute(job)
    assert report["counts"]["excluded"] == 0 and report["counts"]["unsafe"] == 2
    assert report["phases"]["pilot"]["state"] == "Restricted"
    with job.connect() as db:
        decisions = list(db.execute("SELECT state FROM decisions ORDER BY id"))
        assert decisions[0][0] == MATCH and decisions[1][0] == "UnsafeOrContradictoryResponse"


@pytest.mark.parametrize(
    "control",
    [
        {"blocked": True},
        {"active_run": "other"},
        {"restriction_streak": 1},
        {"not_before": "2099-01-01T00:00:00+00:00"},
    ],
)
def test_activity_control_preserved(job, control):
    job.prepare(job.test_source)
    job.control = lambda: control
    with pytest.raises(ValueError):
        execute(job)
    assert not job.test_calls


def test_production_database_untouched_and_other_workers_locked(job):
    db = sqlite3.connect(job.database)
    db.execute("CREATE TABLE private(value TEXT)")
    db.execute("INSERT INTO private VALUES('unchanged')")
    db.commit()
    db.close()
    before = job.database.read_bytes()
    job.prepare(job.test_source)
    with RobloxActivity(job.database):
        with pytest.raises(ValueError):
            execute(job)
    execute(job)
    assert job.database.read_bytes() == before


def test_no_network_when_policy_or_source_changes(job):
    job.prepare(job.test_source)
    value = json.loads(job.manifest_path.read_bytes())
    value["policy"] = "other"
    del value["fingerprint"]
    value["fingerprint"] = bulk.digest(bulk.encoded(value))
    job.manifest_path.write_bytes(bulk.encoded(value))
    with pytest.raises(ValueError):
        execute(job)
    assert not job.test_calls


def test_export_cancellation_resume_and_tamper_detection(job, tmp_path):
    job.prepare(job.test_source, pilot_names=3)
    execute(job)
    root = tmp_path / "export"
    job.updated = job.cancel.set
    job.export(root)
    partial = json.loads((root / "export-state.json").read_bytes())
    assert not partial["complete"] and len(partial["files"]) == 1
    original = (root / "current.csv").read_bytes()
    job.cancel.clear()
    job.updated = lambda: None
    job.export(root)
    assert json.loads((root / "export-state.json").read_bytes())["complete"]
    assert (root / "current.csv").read_bytes() == original
    (root / "current.csv").write_text("tampered")
    with pytest.raises(ValueError):
        job.export(root)
    assert not json.loads((root / "export-state.json").read_bytes())["complete"]


def test_abrupt_process_crash_keeps_intent_and_unknowns_without_replay(job):
    job.prepare(job.test_source, pilot_names=5, batch_size=2)
    script = """
import os,sys,socket
from username_finder.roblox_prefilter import Prefilter
socket.socket.connect=lambda *a,**kw: (_ for _ in ()).throw(AssertionError('No live'))
p=Prefilter(sys.argv[1],sys.argv[2],transport=lambda _:os._exit(7))
p.run('pilot',p.review('pilot'))
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(job.directory), str(job.database)], capture_output=True, timeout=10
    )
    assert child.returncode == 7
    recovered = job.recover()
    assert recovered["counts"]["unknown"] == 2 and recovered["counts"]["attempts"] == 1
    assert recovered["phases"]["pilot"]["state"] == "ReviewRequired"
    assert not job.test_calls


def test_shared_guard_cannot_clear_other_experiment_or_provider_blocks(job):
    job.prepare(job.test_source)
    with RobloxActivity(job.database) as guard:
        guard.mark_pending()
    with pytest.raises(ValueError):
        execute(job)
    assert not job.test_calls


def test_uncertain_review_never_overrides_a_provider_block(job):
    job.prepare(job.test_source, pilot_names=5, batch_size=2)
    job.transport = lambda _: (_ for _ in ()).throw(OSError("uncertain"))
    execute(job)
    job.recover()
    activity = RobloxActivity(job.database)
    activity.block.write_text("http_403")
    job.review_uncertain(job.manifest()["fingerprint"])
    with pytest.raises(ValueError):
        execute(job)
    assert activity.block.read_text() == "http_403" and not job.test_calls


def test_resume_keeps_completed_outputs_revision_and_pilot_rate_denominator(job):
    job.prepare(job.test_source, pilot_names=10, batch_size=2)
    job.wait = lambda _: True
    result = execute(job)
    assert result["quality"]["pilot"]["pilot_exclusion_fraction"] == 1
    assert result["quality"]["pilot"]["pilot_coverage"] == 0.2
    before = result["revision"]
    assert job.recover()["revision"] == before


def test_local_invalid_export_formula_safety(job, tmp_path):
    job.test_source.write_text("=formula\n@bad\n+value\n-valid\nGoodName")
    job.prepare(job.test_source)
    root = job.export(tmp_path / "out")
    raw = (root / "invalid.csv").read_text(encoding="utf-8-sig")
    assert "'=formula" in raw and "'@bad" in raw
    assert "=formula" in (root / "invalid.json").read_text()


def test_bounded_memory_across_local_input_sizes(tmp_path):
    script = """
import json,sys,time,socket,tracemalloc
from pathlib import Path
from username_finder.roblox_prefilter import Prefilter,memory_peak
socket.socket.connect=lambda *a,**kw: (_ for _ in ()).throw(AssertionError('No live'))
root=Path(sys.argv[1]);count=int(sys.argv[2]);source=root/'synthetic.txt'
with source.open('w') as stream:
    for i in range(count):stream.write(f'User{i:08}\\n')
tracemalloc.start();started=time.monotonic();baseline=memory_peak()
p=Prefilter(root/'run',root/'production.db');result=p.prepare(source)
current,peak=tracemalloc.get_traced_memory()
assert not p.database.exists()
print(json.dumps({'rows':count,'unique':result['counts']['unique'],'seconds':time.monotonic()-started,
 'python_peak_bytes':peak,'process_peak_bytes':memory_peak(),'baseline_process_peak_bytes':baseline}))
"""
    results = []
    for count in (10000, 100000):
        root = tmp_path / str(count)
        root.mkdir()
        child = subprocess.run(
            [sys.executable, "-c", script, str(root), str(count)],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        results.append(json.loads(child.stdout))
    assert [r["unique"] for r in results] == [10000, 100000]
    assert results[1]["python_peak_bytes"] < results[0]["python_peak_bytes"] + 2 * 1024 * 1024
    assert results[1]["process_peak_bytes"] < results[0]["process_peak_bytes"] + 20 * 1024 * 1024
    print("Bounded import measurements: " + json.dumps(results))
