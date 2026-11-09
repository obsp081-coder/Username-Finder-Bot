"""Offline 200-name invariants and small local-cost measurements; no production guard changes."""

import base64
import hashlib
import json
import sqlite3
import time
from collections import Counter
from email.message import Message

import pytest
from test_roblox_bulk import record, response
from test_roblox_bulk_batch_research import synthetic_limit as synthetic_limit
from test_roblox_prefilter import execute
from test_roblox_prefilter import job as job
from test_roblox_prefilter import no_live as no_live
from test_roblox_prefilter_review import mark_old_policy

from tools.roblox_prefilter_offline_benchmark import disk_bytes
from username_finder import roblox_bulk_protocol as bulk
from username_finder import roblox_prefilter as prefilter
from username_finder import roblox_prefilter_transport as transport
from username_finder.roblox_activity import RobloxActivity
from username_finder.roblox_prefilter_estimates import capacity_plan


def names(count=200):
    return [f"User{i:05}" for i in range(count)]


def test_200_casefold_exact_historical_and_legitimate_missing_records():
    requested = names()
    rows = [record(n.swapcase(), current=n.upper(), uid=i + 1) for i, n in enumerate(requested[:160])]
    rows.extend(record(n, current=f"Changed{i:05}", uid=i + 1) for i, n in enumerate(requested[160:180], 160))
    mapping, stop = bulk.interpret_bulk(requested, response(rows))
    assert not stop and len(mapping) == 200
    assert Counter(v["state"] for v in mapping.values()) == {
        "CurrentAccountMatchByAPI": 160,
        "HistoricalOrDifferentCurrentName": 20,
        "NoMatchingBulkRecord": 20,
    }
    assert not {v["state"] for v in mapping.values()} & {"Available", "Taken", "Reserved"}
    # A valid empty account lookup is not a registration-availability result or proof of semantic coverage.
    empty, stop = bulk.interpret_bulk(requested, response())
    assert not stop and all(v["state"] == "NoMatchingBulkRecord" for v in empty.values())


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "id_conflict",
        "name_conflict",
        "unrequested",
        "bool_id",
        "zero_id",
        "float_id",
        "missing_id",
        "extra_field",
        "malformed",
        "duplicate_json_key",
        "nan",
        "invalid_utf8",
        "schema",
    ],
)
def test_200_late_invalid_record_revokes_entire_positive_response(fault):
    requested = names()
    rows = [record(n, uid=i + 1) for i, n in enumerate(requested)]
    result = response(rows)
    if fault == "duplicate":
        rows.append(rows[0])
    elif fault == "id_conflict":
        rows[-1]["id"] = rows[0]["id"]
    elif fault == "name_conflict":
        rows[-1]["name"] = rows[0]["name"]
    elif fault == "unrequested":
        rows[-1]["requestedUsername"] = "NotRequested"
    elif fault == "bool_id":
        rows[-1]["id"] = True
    elif fault == "zero_id":
        rows[-1]["id"] = 0
    elif fault == "float_id":
        rows[-1]["id"] = 1.0
    elif fault == "missing_id":
        rows[-1].pop("id")
    elif fault == "extra_field":
        rows[-1]["unexpected"] = 1
    else:
        result.body = {
            "malformed": b'{"data":[',
            "duplicate_json_key": b'{"data":[],"data":[]}',
            "nan": b'{"data":[NaN]}',
            "invalid_utf8": b'{"data":["\xff"]}',
            "schema": b'{"data":{}}',
        }[fault]
    if fault in {
        "duplicate",
        "id_conflict",
        "name_conflict",
        "unrequested",
        "bool_id",
        "zero_id",
        "float_id",
        "missing_id",
        "extra_field",
    }:
        result = response(rows)
    mapping, stop = bulk.interpret_bulk(requested, result)
    assert stop and all(v["state"] == "NoMatchingBulkRecord" for v in mapping.values())


@pytest.mark.parametrize("failure", ["truncated", "oversized", "timeout", "connection", "cancel"])
def test_owned_transport_200_bounds_and_uncertain_failures_never_retry(monkeypatch, failure):
    requested = names()
    payload = bulk.encoded({"data": [record(n, uid=i + 1) for i, n in enumerate(requested)]})
    if failure == "oversized":
        payload = b" " * (bulk.protocol.MAX_BODY + 1)
    calls, connections = [], []

    class Stream:
        status = 200

        def __init__(self):
            self.raw = payload
            self.headers = Message()
            self.headers["Content-Type"] = "application/json"
            self.headers["Content-Length"] = str(len(payload) + (failure == "truncated"))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read1(self, length):
            chunk, self.raw = self.raw[:length], self.raw[length:]
            return chunk

    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock = self
            self.closed = False
            connections.append(self)

        def settimeout(self, value):
            assert 0 < value <= 10

        def request(self, method, path, *, body, headers):
            calls.append(json.loads(body))

        def getresponse(self):
            if failure == "timeout":
                raise TimeoutError
            if failure == "connection":
                raise ConnectionResetError
            if failure == "cancel":
                raise KeyboardInterrupt
            return Stream()

        def close(self):
            self.closed = True
            self.sock = None

    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    session = transport.PrefilterTransport()
    try:
        observed = session({"kind": "bulk", "method": "POST", "url": bulk.BULK_URL, "usernames": requested})
    finally:
        session.close()
    assert len(calls) == len(connections) == 1 and connections[0].closed
    assert calls[0] == {"usernames": requested, "excludeBannedUsers": False}
    assert observed.error and not observed.complete and len(observed.body) <= bulk.protocol.MAX_BODY
    mapping, stop = bulk.interpret_bulk(requested, observed)
    assert stop and all(v["state"] == "NoMatchingBulkRecord" for v in mapping.values())


@pytest.mark.parametrize(
    "fault",
    [400, 401, 403, 429, "timeout", "connection", "truncated", "quota", "challenge", "retry_after", "bad_id"],
)
def test_200_failure_after_pilot_preserves_evidence_and_blocks_resume(job, synthetic_limit, fault):
    job.test_source.write_text("\n".join(names(313)), encoding="utf-8")
    job.prepare(job.test_source, batch_size=200, pilot_names=100)
    execute(job)
    with job.connect(read_only=True) as db:
        pilot = tuple(db.execute("SELECT * FROM requests").fetchone())
    calls = []

    def fetch(request):
        calls.append(request)
        if isinstance(fault, int):
            return response(status=fault)
        if fault in ("timeout", "connection"):
            return response(error=fault, complete=False, status=None)
        if fault == "truncated":
            return response(body=b'{"data":[', complete=False, error="incomplete_body")
        if fault == "quota":
            return response(headers={"content-type": "application/json", "x-ratelimit-remaining": "0"})
        if fault == "challenge":
            return response(challenge=True)
        if fault == "retry_after":
            return response(headers={"content-type": "application/json", "retry-after": "3600"})
        return response([record(n, uid=2**63 + i) for i, n in enumerate(request["usernames"])])

    job.transport = fetch
    result = execute(job, "full")
    assert len(calls) == 1 and len(calls[0]["usernames"]) == 200
    assert result["counts"]["excluded"] == 100
    assert result["counts"]["unknown"] + result["counts"]["unsafe"] == 200
    assert result["phases"]["full"]["state"] == "Restricted" and result["unprocessed"] == 13
    marker = RobloxActivity(job.database).block
    before = marker.read_bytes()
    job.recover()
    with pytest.raises(ValueError):
        execute(job, "full")
    with pytest.raises(ValueError):
        job.review_uncertain(job.manifest()["fingerprint"])
    assert marker.read_bytes() == before and len(calls) == 1 and not job.database.exists()
    with job.connect(read_only=True) as db:
        assert tuple(db.execute("SELECT * FROM requests WHERE id=1").fetchone()) == pilot


@pytest.mark.parametrize("point", ["intent", "raw"])
def test_200_interruption_recovery_never_resends_prior_batch(job, synthetic_limit, monkeypatch, point):
    job.test_source.write_text("\n".join(names(313)), encoding="utf-8")
    job.prepare(job.test_source, batch_size=200, pilot_names=100)
    execute(job)
    finish, fetch = job._finish, job.transport
    calls = []

    def interrupted(request):
        calls.append(request)
        if point == "intent":
            raise ConnectionError("Synthetic uncertain delivery")
        return fetch(request)

    def after_raw(*args):
        raise RuntimeError("Synthetic interruption after raw commit")

    job.transport = interrupted
    if point == "raw":
        monkeypatch.setattr(job, "_finish", after_raw)
    execute(job, "full")
    with job.connect(read_only=True) as db:
        before = db.execute("SELECT evidence FROM requests WHERE id=2").fetchone()[0]
    monkeypatch.setattr(job, "_finish", finish)
    started = time.perf_counter()
    result = job.recover()
    duration = time.perf_counter() - started
    assert len(calls) == 1 and result["counts"]["processed"] == 300 and result["unprocessed"] == 13
    assert result["phases"]["full"]["state"] == ("ReviewRequired" if point == "intent" else "Paused")
    assert result["counts"]["unknown"] == (200 if point == "intent" else 0)
    with job.connect(read_only=True) as db:
        assert db.execute("SELECT evidence FROM requests WHERE id=2").fetchone()[0] == before
    print("RECOVERY_MEASUREMENT=" + json.dumps({"point": point, "seconds": duration, "replays": 0}))


def test_200_preparation_and_old_policy_recovery_cannot_authorize_dispatch(job):
    job.test_source.write_text("\n".join(names(313)), encoding="utf-8")
    job.prepare(job.test_source, batch_size=200, pilot_names=100)
    assert prefilter.VERIFIED_OPERATIONAL_BATCH == 20 and prefilter.settings()["batch_size"] == 20
    with pytest.raises(ValueError):
        execute(job)
    with pytest.raises(ValueError):
        job.run("pilot", "forged-token")
    mark_old_policy(job)
    before = (job.path.read_bytes(), job.manifest_path.read_bytes())
    restored = job.recover()
    assert not restored["timing_policy_current"] and not job.test_calls
    assert before == (job.path.read_bytes(), job.manifest_path.read_bytes())
    with pytest.raises(ValueError):
        execute(job)


@pytest.mark.parametrize("batch", [20, 100, 200])
def test_local_cost_measurement(job, synthetic_limit, monkeypatch, tmp_path, batch):
    """Run individually in fresh, network-blocked pytest children for comparable OS RAM peaks.

    Reuses the existing TEST-ONLY synthetic_limit fixture. No shipped CLI, environment
    switch, policy change or application authorization is introduced for batches >20.
    """
    total = 4103
    job.test_source.write_text("\n".join(names(total)), encoding="utf-8")
    started, cpu = time.perf_counter(), time.process_time()
    job.prepare(job.test_source, batch_size=batch, pilot_names=100)
    metrics = {
        "batch": batch,
        "rows": total,
        "import_seconds": time.perf_counter() - started,
        "import_cpu_seconds": time.process_time() - cpu,
    }
    timing = Counter()
    attempts = []
    connect, parse, public, finish = sqlite3.connect, bulk.interpret_bulk, bulk.public_response, job._finish

    class MeasuredConnection(sqlite3.Connection):
        def commit(self):
            before = time.perf_counter()
            try:
                return super().commit()
            finally:
                timing["explicit_commit_seconds"] += time.perf_counter() - before
                timing["explicit_commit_calls"] += 1

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, factory=MeasuredConnection, **kw))

    def parsed(*args):
        before = time.perf_counter()
        try:
            return parse(*args)
        finally:
            timing["parse_seconds"] += time.perf_counter() - before

    def evidenced(*args):
        before = time.perf_counter()
        try:
            return public(*args)
        finally:
            timing["evidence_encoding_seconds"] += time.perf_counter() - before

    def checkpoint(*args):
        before, parser_before = time.perf_counter(), timing["parse_seconds"]
        try:
            return finish(*args)
        finally:
            timing["decision_sql_seconds"] += (
                time.perf_counter() - before - (timing["parse_seconds"] - parser_before)
            )

    def fetch(request):
        before = time.perf_counter()
        body = bulk.encoded({"usernames": request["usernames"], "excludeBannedUsers": False})
        timing["request_encoding_seconds"] += time.perf_counter() - before
        timing["request_json_bytes"] += len(body)
        attempts.append(tuple(request["usernames"]))
        before = time.perf_counter()
        rows = []
        for name in request["usernames"]:
            index = int(name[4:])
            if index % 10 == 9:
                continue
            rows.append(
                record(
                    name.swapcase(), current=name if index % 10 < 7 else f"Changed{index:05}", uid=index + 1
                )
            )
        observed = response(rows, elapsed=0.0)
        timing["synthetic_server_seconds"] += time.perf_counter() - before
        timing["response_body_bytes"] += len(observed.body)
        return observed

    monkeypatch.setattr(bulk, "interpret_bulk", parsed)
    monkeypatch.setattr(bulk, "public_response", evidenced)
    monkeypatch.setattr(job, "_finish", checkpoint)
    job.transport = fetch
    started, cpu = time.perf_counter(), time.process_time()
    pilot = execute(job)
    pilot_wall, pilot_cpu = time.perf_counter() - started, time.process_time() - cpu
    assert pilot["phases"]["pilot"]["state"] == "Completed" and "full" not in pilot["phases"]
    completed_before = len(attempts)
    steady_timing = timing.copy()
    wait = job.wait
    job.wait = lambda _: job.cancel.set() or True
    started = time.perf_counter()
    paused = execute(job, "full")
    metrics["cancel_during_pacing_seconds"] = time.perf_counter() - started
    assert paused["phases"]["full"]["state"] == "Paused" and len(attempts) == completed_before
    job.cancel.clear()
    job.wait = wait
    started = time.perf_counter()
    job.recover()
    metrics["paused_recovery_seconds"] = time.perf_counter() - started
    assert len(attempts) == completed_before
    timing.clear()
    timing.update(steady_timing)
    started, cpu = time.perf_counter(), time.process_time()
    result = execute(job, "full")
    wall = pilot_wall + time.perf_counter() - started
    cpu = pilot_cpu + time.process_time() - cpu
    expected_requests = (100 + batch - 1) // batch + (total - 100 + batch - 1) // batch
    assert len(attempts) == expected_requests and len(attempts[-1]) == 3
    sent = [name for request in attempts for name in request]
    assert len(sent) == len(set(sent)) == total and not job.database.exists()
    with job.connect(read_only=True) as db:
        counts = dict(db.execute("SELECT state,count(*) FROM names GROUP BY state"))
        stored = list(db.execute("SELECT evidence FROM requests ORDER BY id"))
    for row in stored:
        evidence = json.loads(row[0])
        assert (
            hashlib.sha256(base64.b64decode(evidence["body_base64"])).hexdigest() == evidence["body_sha256"]
        )
    assert result["counts"]["unknown"] == result["counts"]["unsafe"] == 0
    measured_timing = dict(timing)
    scratch = disk_bytes(job.directory)
    started, export_cpu = time.perf_counter(), time.process_time()
    exported = job.export(tmp_path / "exports")
    metrics.update(
        export_seconds=time.perf_counter() - started, export_cpu_seconds=time.process_time() - export_cpu
    )
    assert json.loads((exported / "export-state.json").read_bytes())["complete"]
    local = wall - timing["synthetic_server_seconds"]
    metrics.update(
        measured_timing,
        requests=len(attempts),
        local_run_seconds=local,
        run_cpu_seconds=cpu,
        run_seconds_including_synthetic_server=wall,
        counts=counts,
        local_attempted_names_per_second=total / local,
        local_protocol_accounted_names_per_second=total / local,
        local_current_match_names_per_second=result["counts"]["excluded"] / local,
        scratch_bytes=scratch,
        export_bytes=disk_bytes(exported),
        final_directory_bytes=disk_bytes(tmp_path),
        peak_process_bytes=prefilter.memory_peak(),
        provider_requests=0,
        provider_latency_measured=False,
        note="Local mock cost only; fake-clock pacing does not sleep. Missing matches are not availability. "
        "Explicit commit timing is included in total, not all context-manager commits are isolated.",
    )
    local_mean = local / len(attempts)
    metrics["models"] = [
        capacity_plan(
            2_000_000,
            batch,
            delay,
            latency=latency,
            local_seconds_per_request=local_mean,
            safe_outcome_fraction=fraction,
        )
        for delay, latency, fraction in [(10, 0.5, 1), (1, 0.5, 1), (1, 0.5, 0.99)]
    ]
    report = tmp_path / f"local-cost-{batch}.json"
    report.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print("LOCAL_COST_MEASUREMENT=" + json.dumps({"report": str(report), **metrics}))
