"""Synthetic 20/21/50/100-name compatibility only; never increases the real limit."""

import json
from email.message import Message

import pytest
from test_roblox_bulk import record, response
from test_roblox_prefilter import execute
from test_roblox_prefilter import job as job
from test_roblox_prefilter import no_live as no_live

from username_finder import roblox_bulk_protocol as bulk
from username_finder import roblox_prefilter as prefilter
from username_finder import roblox_prefilter_transport as transport

SIZES = (20, 21, 50, 100)


@pytest.fixture
def synthetic_limit(monkeypatch):
    """Permit larger manifests only inside these offline tests, with mock transports."""
    original = prefilter.settings

    def research_settings(delay=10, batch_size=20, pilot_names=100):
        assert batch_size in SIZES
        config = original(delay=delay, batch_size=20, pilot_names=pilot_names)
        return dict(config, batch_size=batch_size)

    monkeypatch.setattr(prefilter, "settings", research_settings)


@pytest.mark.parametrize("size", SIZES)
def test_bulk_encoding_on_owned_mock_connection(monkeypatch, size):
    names = [f"Research{i:05}" for i in range(size)]
    body = bulk.encoded({"data": [record(n, uid=i + 1) for i, n in enumerate(names)]})
    calls = []

    class Stream:
        status = 200

        def __init__(self):
            self.raw = body
            self.headers = Message()
            self.headers["Content-Type"] = "application/json"
            self.headers["Content-Length"] = str(len(body))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read1(self, length):
            result, self.raw = self.raw[:length], self.raw[length:]
            return result

    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock = self

        def settimeout(self, value):
            assert 0 < value <= 10

        def request(self, method, path, *, body, headers):
            calls.append(json.loads(body))
            assert (method, path) == ("POST", "/v1/usernames/users")
            assert calls[-1] == {"usernames": names, "excludeBannedUsers": False}
            assert not {"Authorization", "Cookie"} & headers.keys()

        def getresponse(self):
            return Stream()

        def close(self):
            self.sock = None

    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    session = transport.PrefilterTransport()
    try:
        result = session({"kind": "bulk", "method": "POST", "url": bulk.BULK_URL, "usernames": names})
    finally:
        session.close()
    mapping, stop = bulk.interpret_bulk(names, result)
    assert len(calls) == 1 and len(mapping) == size and not stop
    assert all(v["state"] == "CurrentAccountMatchByAPI" for v in mapping.values())


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(
    "kind",
    ["missing", "duplicate", "conflicting_ids", "conflicting_names", "oversized", "unrequested", "malformed"],
)
def test_conservative_protocol_for_every_research_size(size, kind):
    names = [f"Research{i:05}" for i in range(size)]
    rows = [record(n, uid=i + 1) for i, n in enumerate(names)]
    if kind == "missing":
        rows.pop()
    elif kind == "duplicate":
        rows.append(rows[0])
    elif kind == "conflicting_ids":
        rows[1]["name"] = rows[0]["name"]
    elif kind == "conflicting_names":
        rows[1]["id"] = rows[0]["id"]
    elif kind == "unrequested":
        rows[-1]["requestedUsername"] = "NotRequested"
    result = response(rows)
    if kind == "oversized":
        result.body = b" " * (bulk.protocol.MAX_BODY + 1)
    elif kind == "malformed":
        result.body = b'{"data":{}}'
    mapping, stop = bulk.interpret_bulk(names, result)
    if kind == "missing":
        assert not stop and mapping[names[-1].lower()]["state"] == "NoMatchingBulkRecord"
    else:
        assert stop
        assert all(v["state"] == "NoMatchingBulkRecord" for v in mapping.values())
    assert all(v["state"] not in ("Available", "Taken", "Reserved") for v in mapping.values())


@pytest.mark.parametrize("size", SIZES)
def test_synthetic_large_batch_durability_export_recovery_without_replay(
    job, synthetic_limit, monkeypatch, size, tmp_path
):
    total = size * 2 + 3
    job.test_source.write_text("\n".join(f"User{i:05}" for i in range(total)), encoding="utf8")
    job.prepare(job.test_source, batch_size=size, pilot_names=size + 1)
    finish = job._finish

    def interrupt(db, request, response, meta):
        if request["id"] == 2:
            raise RuntimeError("Synthetic crash after durable raw")
        return finish(db, request, response, meta)

    monkeypatch.setattr(job, "_finish", interrupt)
    execute(job)
    assert len(job.test_calls) == 2
    with job.connect() as db:
        raw = [r[0] for r in db.execute("SELECT evidence FROM requests ORDER BY id")]
    monkeypatch.setattr(job, "_finish", finish)
    restored = job.recover()
    assert restored["phases"]["pilot"]["state"] == "Completed"
    assert "full" not in restored["phases"]
    assert len(job.test_calls) == 2
    result = execute(job, "full")
    assert result["counts"]["excluded"] == total
    sent = [n for _, request in job.test_calls for n in request["usernames"]]
    assert len(sent) == len(set(sent)) == total
    assert [len(r["usernames"]) for _, r in job.test_calls] == [size, 1, size, 2]
    with job.connect() as db:
        assert [r[0] for r in db.execute("SELECT evidence FROM requests WHERE id<=2 ORDER BY id")] == raw
    out = job.export(tmp_path / "export")
    assert len(json.loads((out / "current.json").read_bytes())) == total
    assert not (out / "individual-candidates.txt").read_text().strip()
    assert json.loads((out / "export-state.json").read_bytes())["complete"]


@pytest.mark.parametrize("size", SIZES)
def test_cross_batch_identity_conflict_revokes_exclusions(job, synthetic_limit, size, tmp_path):
    job.test_source.write_text("\n".join(f"User{i:05}" for i in range(2 * size + 3)), encoding="utf8")
    job.prepare(job.test_source, batch_size=size, pilot_names=2 * size)
    calls = []

    def fetch(request):
        calls.append(request)
        return response([record(n, uid=i + 1) for i, n in enumerate(request["usernames"])])

    job.transport = fetch
    result = execute(job)
    assert len(calls) == 2 and result["phases"]["pilot"]["state"] == "Restricted"
    assert result["counts"]["excluded"] == 0 and result["counts"]["unsafe"] == 2 * size
    out = job.export(tmp_path / "conflict-export")
    assert len(json.loads((out / "unresolved.json").read_bytes())) == 2 * size


def test_operational_limit_remains_twenty():
    assert prefilter.settings(batch_size=20)["batch_size"] == 20
    for value in (21, 50, 100):
        with pytest.raises(ValueError):
            prefilter.settings(batch_size=value)
