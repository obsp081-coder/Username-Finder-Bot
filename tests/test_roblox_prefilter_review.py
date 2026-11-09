"""Offline review, immutable compatibility, unsupported-batch guards and audit tests."""

import hashlib
import json
import subprocess
import sys

import pytest
from test_roblox_bulk import response
from test_roblox_prefilter import execute
from test_roblox_prefilter import job as job
from test_roblox_prefilter import no_live as no_live

from username_finder import roblox_bulk_protocol as bulk
from username_finder.roblox_activity import RobloxActivity
from username_finder.roblox_prefilter import Prefilter, settings
from username_finder.roblox_prefilter_estimates import calculate
from username_finder.roblox_prefilter_review import inspect_operation, record_review
from username_finder.roblox_prefilter_transport import PrefilterTransport


def table_rows(job):
    with job.connect(read_only=True) as db:
        return {
            table: [tuple(r) for r in db.execute("SELECT * FROM " + table)]
            for table in ("names", "requests", "decisions")
        }


def mark_old_policy(job):
    manifest = job.manifest()
    manifest.pop("fingerprint")
    manifest["policy"] = "historical-policy-fixture"
    manifest["fingerprint"] = bulk.digest(bulk.encoded(manifest))
    job.manifest_path.write_bytes(bulk.encoded(manifest))
    with job.connect() as db:
        meta = job.meta(db)
        meta["fingerprint"] = manifest["fingerprint"]
        job.save(db, meta)


@pytest.mark.parametrize("size", [1, 20, 50, 100, 200, 500, 1000])
def test_configured_sizes_are_exact_and_calculator_is_hypothetical(size):
    assert settings(batch_size=size)["batch_size"] == size
    model = calculate(0.001, size, 100)
    assert model["names_per_second"] == pytest.approx(size / 0.501)
    assert model["batch_supported"] == (size <= 20)


@pytest.mark.parametrize("size", [21, 50, 100, 200, 500, 1000])
def test_unsupported_sizes_persist_but_never_dispatch_or_make_intent(job, size):
    job.prepare(job.test_source, batch_size=size)
    before = job.manifest_path.read_bytes()
    with pytest.raises(ValueError, match="20"):
        execute(job)
    restarted = Prefilter(job.directory, job.database, transport=lambda _: pytest.fail("No traffic"))
    assert inspect_operation(restarted)["manifest"]["settings"]["batch_size"] == size
    assert (
        "unverified_batch_size_dispatch_disabled"
        in inspect_operation(restarted)["restriction_review"]["resume_blockers"]
    )
    assert restarted.recover()["counts"]["attempts"] == 0
    assert job.manifest_path.read_bytes() == before and not job.test_calls
    with job.connect() as db:
        assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0


@pytest.mark.parametrize("names", [[], ["name"] * 1001, ["a" * 21], [None], ("name",)])
def test_transport_rejects_unbounded_or_invalid_payload_before_network(names):
    session = PrefilterTransport()
    with pytest.raises(ValueError, match="bounded"):
        session({"kind": "bulk", "url": bulk.BULK_URL, "method": "POST", "usernames": names})
    assert session.connection is None


@pytest.mark.parametrize("status", [401, 403, 429])
def test_read_only_restriction_review_keeps_evidence_and_never_unlocks(job, status, tmp_path):
    job.prepare(job.test_source)
    job.transport = lambda _: response(status=status, headers={"x-ratelimit-reset": "9", "retry-after": "1"})
    execute(job)
    job.test_clock.at += 100000
    before = table_rows(job)
    manifest = job.manifest_path.read_bytes()
    marker = RobloxActivity(job.database).block
    blocked = marker.read_bytes()
    db_before = hashlib.sha256(job.path.read_bytes()).hexdigest()
    result = inspect_operation(job)
    report = result["restriction_review"]
    assert report["recovery_decision"] == "Blocked" and not report["dispatch_authorized"]
    assert report["last_response_evidence"]["status"] == status
    assert report["last_response_evidence"]["body_integrity_verified"]
    assert report["last_response_evidence"]["headers"]["x-ratelimit-reset"] == "9"
    assert report["last_response_evidence"]["response_at"]
    assert hashlib.sha256(job.path.read_bytes()).hexdigest() == db_before
    assert marker.read_bytes() == blocked and job.manifest_path.read_bytes() == manifest
    with pytest.raises(ValueError):
        record_review(job, "wrong")
    result = record_review(job, result["manifest"]["fingerprint"])
    assert len(result["restriction_review"]["recent_reviews"]) == 1
    assert "no unlock" in result["restriction_review"]["recent_reviews"][0]["data"]
    assert result["counts"] == inspect_operation(job)["counts"]
    assert table_rows(job) == before and marker.read_bytes() == blocked
    with pytest.raises(ValueError):
        execute(job)
    job.export(tmp_path / "export")
    assert table_rows(job) == before


def test_old_policy_does_not_reinterpret_pending_evidence_or_replay(job, monkeypatch, tmp_path):
    job.prepare(job.test_source)

    def crash(*args):
        raise RuntimeError("After durable response")

    monkeypatch.setattr(job, "_finish", crash)
    execute(job)
    mark_old_policy(job)
    before = table_rows(job)
    manifest = job.manifest_path.read_bytes()
    db_hash = hashlib.sha256(job.path.read_bytes()).hexdigest()
    report = inspect_operation(job)["restriction_review"]
    assert not report["policy_matches"] and report["pending_requests"]
    assert "source_policy_mismatch_requires_technical_compatibility_review" in report["resume_blockers"]
    assert job.recover()["counts"]["processed"] == 0
    assert hashlib.sha256(job.path.read_bytes()).hexdigest() == db_hash
    assert table_rows(job) == before and job.manifest_path.read_bytes() == manifest
    with pytest.raises(ValueError, match="السياسة"):
        execute(job)
    job.export(tmp_path / "old-export")
    assert len(job.test_calls) == 1 and table_rows(job) == before


def test_cli_report_is_read_only_and_does_not_open_production_database(job, tmp_path):
    job.prepare(job.test_source)
    target = tmp_path / "report.json"
    before = job.manifest_path.read_bytes(), job.path.read_bytes()
    args = [
        sys.executable,
        "tools/roblox_prefilter_review.py",
        "--run",
        str(job.directory),
        "--database",
        str(job.database),
        "--output",
        str(target),
    ]
    result = subprocess.run(args, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not json.loads(target.read_bytes())["restriction_review"]["dispatch_authorized"]
    assert before == (job.manifest_path.read_bytes(), job.path.read_bytes())
    assert not job.database.exists()
    original = target.read_bytes()
    assert subprocess.run(args, capture_output=True, timeout=15).returncode != 0
    assert target.read_bytes() == original and not job.test_calls


@pytest.mark.parametrize("size", [20, 50, 100, 200, 500, 1000])
@pytest.mark.parametrize("error", [TimeoutError, ConnectionResetError])
def test_large_mock_transport_failure_is_not_retried(size, error):
    calls = []

    class Connection:
        def __init__(self):
            self.sock = self

        def settimeout(self, seconds):
            assert 0 < seconds <= 10

        def request(self, method, path, *, body, headers):
            calls.append(json.loads(body))

        def getresponse(self):
            raise error("injected")

        def close(self):
            self.sock = None

    session = PrefilterTransport()
    session.connection = Connection()
    names = [f"Research{i:05}" for i in range(size)]
    result = session({"kind": "bulk", "url": bulk.BULK_URL, "method": "POST", "usernames": names})
    assert len(calls) == 1 and calls[0]["usernames"] == names
    expected = "timeout" if error is TimeoutError else "network_error"
    assert result.error == expected and not result.complete and session.connection is None
    mapping, stop = bulk.interpret_bulk(names, result)
    assert stop and len(mapping) == size
    assert all(r["state"] not in ("Available", "Taken", "Reserved") for r in mapping.values())
