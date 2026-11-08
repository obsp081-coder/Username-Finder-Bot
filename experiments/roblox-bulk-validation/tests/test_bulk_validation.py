"""Hypothetical fixtures only: no account histories or live responses are asserted."""

import copy
import csv
import http.client
import importlib.util
import json
import socket
import sqlite3
import subprocess
import sys
import threading
from email.message import Message
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bulk_validation.py"
spec = importlib.util.spec_from_file_location("roblox_bulk_validation_experiment", SCRIPT)
poc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poc)


@pytest.fixture(autouse=True)
def no_network_or_database(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network and database access forbidden in offline tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)


@pytest.fixture
def manifest():
    records = []
    for category, count in (("current", 10), ("former", 5), ("restricted", 5), ("random", 20)):
        for i in range(count):
            name = {
                "current": f"current{i:02}",
                "former": f"former{i:02}",
                "restricted": f"blocked{i:02}",
                "random": f"a{i:03}",
            }[category]
            expected = name if category == "current" else f"newname{i:02}" if category == "former" else ""
            records.append(
                {
                    "category": category,
                    "requested_username": name.upper() if category == "current" and i < 4 else name,
                    "expected_current_username": expected,
                    "expected_user_id": None,
                    "evidence_reference": "https://example.invalid/synthetic-fixture",
                    "independently_established": category != "random",
                    "account_state": "restricted",
                    "hypothesis": "Hypothetical test relationship only",
                    "limitations": "Synthetic",
                }
            )
    return {"version": 1, "candidates": records, "restricted_case_gap": ""}


def response(data=None, **kw):
    body = kw.pop("body", json.dumps(data if data is not None else {"data": []}).encode())
    headers = kw.pop("headers", {"content-type": "application/json"})
    return poc.protocol.Response(
        status=kw.pop("status", 200), body=body, headers=headers, elapsed=0.125, **kw
    )


def record(requested="Example", **kw):
    return dict(
        {"requestedUsername": requested, "name": requested, "id": 12, "displayName": "Unrelated"}, **kw
    )


class Clock:
    def __init__(self):
        self.at = 0
        self.waits = []

    def now(self):
        return self.at

    def wait(self, delay):
        self.waits.append(delay)
        self.at += delay
        return False


def setup(path, manifest):
    path.mkdir(exist_ok=True)
    poc.write_new(path / "manifest.json", manifest)
    (path / "reviews").mkdir(exist_ok=True)
    poc.write_new(
        poc.review_path(path, manifest),
        {
            "manifest_sha256": poc.fingerprint(manifest),
            "protocol_sha256": poc.protocol_fingerprint(),
            "acknowledgment": "REVIEW " + poc.fingerprint(manifest),
        },
    )
    return path


def simulate(tmp_path, manifest, *, fault=None, cancel=None, clock=None, pilot=False):
    setup(tmp_path, manifest)
    clock = clock or Clock()
    calls = []
    candidates = {poc.key(r["requested_username"]): r for r in manifest["candidates"]}

    def transport(request):
        calls.append(copy.deepcopy(request))
        clock.at += 0.125
        if fault:
            value = fault(request, len(calls))
            if value is not None:
                return value
        if request["kind"] == "bulk":
            return response(
                {
                    "data": [
                        record(
                            n,
                            name=candidates[poc.key(n)]["expected_current_username"] or n,
                            id=1000 + list(candidates).index(poc.key(n)),
                        )
                        for n in request["usernames"]
                    ]
                }
            )
        return response({"code": 1, "message": "Username is already in use"})

    summary = poc.run_experiment(
        tmp_path,
        transport=transport,
        acknowledge=lambda _: "RUN " + poc.fingerprint(manifest),
        cancel=cancel,
        wait=clock.wait,
        monotonic=clock.now,
        pilot=pilot,
    )
    return summary, calls, clock, tmp_path / "runs" / poc.fingerprint(manifest)


def pilot_from_full(path, manifest):
    path.mkdir(exist_ok=True)
    poc.write_new(path / "manifest.json", manifest)
    return poc.prepare_pilot(path)


@pytest.mark.parametrize("requested,current", [("Example", "Example"), ("EXAMPLE", "example")])
def test_current_and_case_matches(requested, current):
    mapping, stop = poc.interpret_bulk([requested], response({"data": [record(requested, name=current)]}))
    assert not stop
    assert mapping["example"]["preliminary_classification"] == "Taken"
    assert mapping["example"]["api_code"] is None
    assert "api_only" in mapping["example"]["reason"]


def test_alias_display_missing_and_partial():
    mapping, stop = poc.interpret_bulk(
        ["OldName", "Absent", "Current"],
        response(
            {"data": [record("OldName", name="NewName", displayName="OldName"), record("Current", id=13)]}
        ),
    )
    assert not stop
    assert mapping["oldname"]["reason"] == "historical_or_different_current_name"
    assert mapping["absent"]["preliminary_classification"] == "Unresolved"
    assert mapping["current"]["preliminary_classification"] == "Taken"
    empty, stop = poc.interpret_bulk(["Absent"], response())
    assert not stop and empty["absent"]["preliminary_classification"] == "Unresolved"


@pytest.mark.parametrize("bad_id", [None, True, False, 0, -1, "12", 1.5, [], {}])
def test_unsafe_ids(bad_id):
    mapping, stop = poc.interpret_bulk(["Example"], response({"data": [record(id=bad_id)]}))
    assert stop and mapping["example"]["preliminary_classification"] == "Unresolved"


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "text",
        True,
        1,
        {"data": None},
        {"data": {}},
        {"data": [], "errors": []},
        {"data": [None]},
        {"data": [dict(record(), extra=True)]},
        {"data": [{"requestedUsername": "Example", "name": "Example"}]},
        {"data": [record("Other")]},
        {"data": [record(), record()]},
        {"data": [record(), record(id=99)]},
    ],
)
def test_unexpected_bulk_shapes_and_duplicates(data):
    mapping, stop = poc.interpret_bulk(["Example"], response(body=json.dumps(data).encode()))
    assert stop
    assert all(r["preliminary_classification"] != "Taken" for r in mapping.values())


@pytest.mark.parametrize(
    "body", [b'{"data":[],"data":[]}', b'{"data":[{"id":1,"id":2}]}', b'{"data":NaN}', b"{", b"\xff"]
)
def test_malformed_and_duplicate_json_keys(body):
    _, stop = poc.interpret_bulk(["Example"], response(body=body))
    assert stop == "malformed_bulk_json"


def test_conflicting_accounts_and_duplicate_inputs():
    _, stop = poc.interpret_bulk(["Example", "Second"], response({"data": [record(), record("Second")]}))
    assert stop == "conflicting_account_names"
    _, stop = poc.interpret_bulk(["Example", "EXAMPLE"], response())
    assert stop == "duplicate_request_names"


def test_manifest_complete_counts_and_bounded_plan(manifest):
    assert poc.validate_manifest(manifest) == []
    plan = poc.request_plan(manifest)
    assert len(plan) == 42
    assert [len(r["usernames"]) for r in plan[:2]] == [20, 20]
    names = [n.lower() for r in plan[:2] for n in r["usernames"]]
    assert len(set(names)) == 40
    assert len({r["usernames"][0].lower() for r in plan[2:]}) == 40
    assert poc.protocol.BIRTHDAY == "2000-01-01T00:00:00Z" and poc.protocol.CONTEXT == 0


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "nonascii",
        "bad_evidence",
        "unreviewed",
        "bad_id",
        "case_variations",
        "wrong_category",
        "missing_former",
        "missing_relation",
    ],
)
def test_manifest_rejects_invalid_cases(manifest, change):
    row = manifest["candidates"][0]
    if change == "duplicate":
        row["requested_username"] = manifest["candidates"][1]["requested_username"].lower()
    elif change == "nonascii":
        row["requested_username"] = "n\u0430me"
    elif change == "bad_evidence":
        row["evidence_reference"] = "https://user:secret@example.invalid/"
    elif change == "unreviewed":
        row["independently_established"] = False
    elif change == "bad_id":
        row["expected_user_id"] = True
    elif change == "case_variations":
        row["requested_username"] = row["expected_current_username"]
    elif change == "wrong_category":
        row["category"] = "other"
    elif change == "missing_former":
        manifest["candidates"].pop(10)
    else:
        row["hypothesis"] = ""
    assert poc.validate_manifest(manifest)


def test_optional_restricted_gap_is_explicit(manifest):
    manifest["candidates"] = [r for r in manifest["candidates"] if r["category"] != "restricted"]
    assert poc.validate_manifest(manifest)
    manifest["restricted_case_gap"] = "No publicly documented cases supplied; state semantics unverified"
    assert poc.validate_manifest(manifest) == []
    assert len(poc.request_plan(manifest)) == 37


def test_prepare_does_not_invent_documented_accounts_and_never_overwrites(tmp_path):
    m = poc.prepare(tmp_path)
    generated = [r["requested_username"] for r in m["candidates"] if r["category"] == "random"]
    assert len(set(generated)) == 20 and all(len(n) == 4 and poc.valid_name(n) for n in generated)
    assert all(not r["requested_username"] for r in m["candidates"] if r["category"] != "random")
    assert poc.validate_manifest(m)
    before = (tmp_path / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        poc.prepare(tmp_path)
    assert (tmp_path / "manifest.json").read_bytes() == before


def test_defaults_and_live_requires_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(poc, "DATA", tmp_path / "unused")
    assert poc.main([]) == 0 and "offline" in capsys.readouterr().out
    assert not (tmp_path / "unused").exists()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert poc.main(["live"]) == 2


def test_review_fingerprint_and_acknowledgments(tmp_path, manifest):
    poc.write_new(tmp_path / "manifest.json", manifest)
    with pytest.raises(ValueError, match="not acknowledged"):
        poc.review_manifest(tmp_path, acknowledge=lambda _: "yes")
    poc.review_manifest(tmp_path, acknowledge=lambda _: "REVIEW " + poc.fingerprint(manifest))
    with pytest.raises(ValueError, match="not acknowledged"):
        poc.run_experiment(tmp_path, acknowledge=lambda _: "yes")
    assert not (tmp_path / "runs").exists()
    original = (tmp_path / "manifest.json").read_bytes()
    changed = copy.deepcopy(manifest)
    changed["candidates"][0]["limitations"] += " changed"
    (tmp_path / "manifest.json").write_bytes(poc.encoded(changed))
    with pytest.raises(FileNotFoundError):
        poc.reviewed_manifest(tmp_path)
    (tmp_path / "manifest.json").write_bytes(original)
    assert poc.reviewed_manifest(tmp_path)[0] == manifest


def test_full_run_sequential_paced_budget_and_evidence(tmp_path, manifest):
    summary, calls, clock, run = simulate(tmp_path, manifest)
    assert summary["stop_reason"] == "completed"
    assert (
        len(calls)
        == summary["complete_http_responses"]
        == summary["dispatch_intents_including_failures"]
        == 42
    )
    assert [r["kind"] for r in calls] == ["bulk"] * 2 + ["individual"] * 40
    assert len(clock.waits) == 41 and all(w == 10 for w in clock.waits)
    assert all(g >= 10 for g in summary["measured_post_response_gaps_seconds"])
    assert not summary["independently_verified_accuracy"]
    events, warnings = poc.read_events(run / "events.jsonl")
    assert not warnings
    assert len([e for e in events if e["event"] == "individual_interpretation"]) == 40
    assert (run / "manifest.json").read_bytes() == (tmp_path / "manifest.json").read_bytes()
    assert all("body_base64" in e["response"] for e in events if e["event"] == "response")


@pytest.mark.parametrize(
    "fault",
    [response(status=s) for s in (400, 401, 403, 404, 429, 500, 529)]
    + [
        response(error="timeout", complete=False),
        response(error="network_error", complete=False),
        response(complete=False),
        response(challenge=True),
        response(body=b"{"),
        response(headers={"content-type": "application/json", "retry-after": "300"}),
        response(headers={"content-type": "application/json", "x-ratelimit-remaining": "0"}),
        response(headers={"content-type": "application/json", "x-ratelimit-remaining": "NaN"}),
        response(headers={"content-type": "text/html"}),
        response({"data": "unexpected"}),
        response({"token": "do-not-record-this-value"}),
    ],
)
@pytest.mark.parametrize("pilot", [False, True])
def test_first_failure_stops_no_retry_or_endpoint_fallback(tmp_path, manifest, fault, pilot):
    if pilot:
        manifest = pilot_from_full(tmp_path / "source", manifest)
    path = tmp_path / "execution"
    summary, calls, clock, run = simulate(path, manifest, fault=lambda *_: fault, pilot=pilot)
    assert len(calls) == 1 and not clock.waits and summary["stop_reason"] != "completed"
    before = (run / "events.jsonl").read_bytes()
    with pytest.raises(FileExistsError):
        poc.run_experiment(
            path,
            acknowledge=lambda _: "RUN " + poc.fingerprint(manifest),
            transport=lambda _: pytest.fail("No replay allowed"),
            pilot=pilot,
        )
    assert (run / "events.jsonl").read_bytes() == before
    if b'"token"' in fault.body:
        assert b"do-not-record-this-value" not in before


def test_contradiction_stops_after_first_individual_preserves_sources(tmp_path, manifest):
    summary, calls, _, run = simulate(
        tmp_path,
        manifest,
        fault=lambda req, _: (
            response({"code": 0, "message": "Username is valid"}) if req["kind"] == "individual" else None
        ),
    )
    assert len(calls) == 3 and summary["contradictions"]
    assert summary["individual_preliminary_counts"]["Available"] == 1
    assert summary["comparison_conclusion_counts"]["Unknown"] == 40
    assert summary["bulk_preliminary_counts"]["Taken"] > 0
    events, _ = poc.read_events(run / "events.jsonl")
    result = next(e for e in events if e["event"] == "individual_interpretation")
    assert result["result"]["classification"] == "Available"
    assert result["source"] == "individual_validator"


@pytest.mark.parametrize(
    "code,message",
    [(999, "Unknown code"), (0, "Username is not available"), (True, "Bad type"), (1, "Username is valid")],
)
def test_conservative_individual_regressions(tmp_path, manifest, code, message):
    summary, calls, _, _ = simulate(
        tmp_path,
        manifest,
        fault=lambda req, _: (
            response({"code": code, "message": message}) if req["kind"] == "individual" else None
        ),
    )
    assert len(calls) == 3 and summary["stop_reason"].startswith("unknown_individual_")


def test_budget_rejected_before_dispatch(tmp_path, manifest, monkeypatch):
    setup(tmp_path, manifest)
    monkeypatch.setattr(poc, "MAX_REQUESTS", 41)
    with pytest.raises(ValueError, match="budget"):
        poc.run_experiment(
            tmp_path,
            acknowledge=lambda _: "RUN " + poc.fingerprint(manifest),
            transport=lambda _: pytest.fail("Budget must be enforced first"),
        )


def test_cancellation_during_wait_and_before_first_request(tmp_path, manifest):
    class CancelClock(Clock):
        def wait(self, delay):
            return True

    summary, calls, _, _ = simulate(tmp_path / "waiting", manifest, clock=CancelClock())
    assert len(calls) == 1 and summary["stop_reason"] == "cancelled_during_pacing"
    cancel = threading.Event()
    cancel.set()
    summary, calls, _, _ = simulate(tmp_path / "before", manifest, cancel=cancel)
    assert not calls and summary["stop_reason"] == "cancelled_before_request"


def test_ctrl_c_during_dispatch_is_uncertain_not_retried(tmp_path, manifest):
    def interrupt(*args):
        raise KeyboardInterrupt

    summary, calls, _, _ = simulate(tmp_path, manifest, fault=interrupt)
    assert len(calls) == 1
    assert summary["unanswered_delivery_uncertain_attempts"] == [1]
    assert summary["stop_reason"] == "cancelled_delivery_uncertain"


def test_read_only_reporting_preserves_journal_and_manual_notes(tmp_path, manifest):
    _, _, _, run = simulate(tmp_path, manifest)
    journal = (run / "events.jsonl").read_bytes()
    worksheet = run / "manual_comparison.csv"
    worksheet.write_bytes(b"User observations remain private and unchanged\n")
    poc.report_run(run)
    assert worksheet.read_bytes() == b"User observations remain private and unchanged\n"
    assert (run / "events.jsonl").read_bytes() == journal
    with (run / "events.jsonl").open("ab") as stream:
        stream.write(b'{"event":')
    damaged = (run / "events.jsonl").read_bytes()
    summary = poc.report_run(run)
    assert summary["journal_warnings"]
    assert summary["stop_reason"] == "journal_integrity_review_required"
    assert (run / "events.jsonl").read_bytes() == damaged


@pytest.mark.parametrize("pilot", [False, True])
def test_real_subprocess_crash_preserves_intent_and_blocks_replay(tmp_path, manifest, pilot):
    if pilot:
        manifest = pilot_from_full(tmp_path / "source", manifest)
    path = tmp_path / "execution"
    setup(path, manifest)
    code = """
import importlib.util, os, pathlib, socket, sys
spec=importlib.util.spec_from_file_location('experiment',sys.argv[1])
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
def forbidden(*a, **k): raise AssertionError('No live network')
socket.socket.connect=forbidden
path=pathlib.Path(sys.argv[2]);m=p.strict_json((path/'manifest.json').read_bytes())
p.run_experiment(path,acknowledge=lambda _: 'RUN '+p.fingerprint(m),transport=lambda _: os._exit(73),
                 pilot=sys.argv[3]=='pilot')
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(SCRIPT), str(path), "pilot" if pilot else "full"],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    run = path / "runs" / poc.fingerprint(manifest)
    summary = poc.report_run(run)
    assert summary["unanswered_delivery_uncertain_attempts"] == [1]
    assert summary["stop_reason"] == "interrupted_no_durable_stop"
    with pytest.raises(FileExistsError):
        poc.run_experiment(
            path,
            acknowledge=lambda _: "RUN " + poc.fingerprint(manifest),
            transport=lambda _: pytest.fail("Never replay uncertain dispatch"),
            pilot=pilot,
        )


@pytest.mark.parametrize("kind", ["bulk", "individual"])
def test_real_transport_request_contract_and_secret_headers_removed(monkeypatch, kind):
    payload = b'{"data":[]}' if kind == "bulk" else b'{"code":1,"message":"Already in use"}'
    connection = fake_connection(
        monkeypatch,
        payload,
        extra_headers=[
            ("Set-Cookie", "private-cookie"),
            ("rblx-challenge-id", "private-challenge"),
            ("Retry-After", "300"),
            ("x-ratelimit-remaining", "0"),
        ],
    )
    request = {
        "kind": kind,
        "method": "POST" if kind == "bulk" else "GET",
        "url": poc.BULK_URL if kind == "bulk" else poc.protocol.request_url("Example"),
        "usernames": ["Example"],
    }
    observed = poc.Transport()(request)
    method, target, body, headers = connection.requests[0]
    assert method == request["method"]
    if kind == "bulk":
        assert json.loads(body) == {"usernames": ["Example"], "excludeBannedUsers": False}
    else:
        assert target == "/v1/usernames/validate?Username=Example&Birthday=2000-01-01T00%3A00%3A00Z&Context=0"
        assert body is None
    assert "Authorization" not in headers and "Cookie" not in headers
    assert all(0 < t <= 10 for t in connection.timeouts)
    assert observed.body == payload and observed.challenge and connection.closed
    assert "set-cookie" not in observed.headers and "rblx-challenge-id" not in observed.headers
    assert observed.headers["retry-after"] == "300"


def fake_connection(monkeypatch, payload, *, extra_headers=(), error=None):
    class Connection:
        def __init__(self):
            self.sock = self
            self.requests = []
            self.timeouts = []
            self.closed = False

        def connect(self):
            pass

        def settimeout(self, value):
            self.timeouts.append(value)

        def request(self, method, path, *, body, headers):
            self.requests.append((method, path, body, headers))

        def getresponse(self):
            return Stream()

        def close(self):
            self.closed = True

    class Stream:
        def __init__(self):
            self.status = 200
            self.headers = Message()
            self.headers.add_header("Content-Type", "application/json")
            self.headers.add_header("Content-Length", str(len(payload)))
            for k, v in extra_headers:
                self.headers.add_header(k, v)
            self.position = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read1(self, size):
            if error and self.position:
                raise error
            chunk = payload[self.position : self.position + min(size, 65536)]
            self.position += len(chunk)
            return chunk

    connection = Connection()

    def factory(host, *, timeout, context):
        assert host in ("users.roblox.com", "auth.roblox.com") and timeout == 10
        assert context.check_hostname and context.verify_mode == poc.ssl.CERT_REQUIRED
        return connection

    monkeypatch.setattr(http.client, "HTTPSConnection", factory)
    return connection


@pytest.mark.parametrize(
    "failure", ["timeout", "cancel", "oversize", "length", "duplicate_header", "framing"]
)
def test_transport_partial_failures_preserve_bounded_evidence_and_close(monkeypatch, failure):
    payload = b"x" * (poc.protocol.MAX_BODY + 1 if failure == "oversize" else 30)
    headers = {
        "length": [("Content-Length", "999")],
        "duplicate_header": [("Content-Type", "text/html")],
        "framing": [("Transfer-Encoding", "chunked")],
    }.get(failure, [])
    error = {"timeout": TimeoutError(), "cancel": KeyboardInterrupt()}.get(failure)
    connection = fake_connection(monkeypatch, payload, extra_headers=headers, error=error)
    r = poc.Transport()({"kind": "bulk", "method": "POST", "url": poc.BULK_URL, "usernames": ["Example"]})
    assert r.error and not r.complete and connection.closed
    assert len(r.body) <= poc.protocol.MAX_BODY and len(connection.requests) == 1
    if failure in ("timeout", "cancel"):
        assert r.body == payload
    assert poc.response_stop(r)


@pytest.mark.parametrize(
    "url",
    [
        "http://users.roblox.com/v1/usernames/users",
        "https://example.invalid/v1/usernames/users",
        "https://users.roblox.com/v1/usernames/users#fragment",
        "https://user:password@users.roblox.com/v1/usernames/users",
    ],
)
def test_transport_refuses_alternate_destinations_without_network(url):
    with pytest.raises(ValueError, match="Destination"):
        poc.Transport()({"kind": "bulk", "method": "POST", "url": url, "usernames": ["Example"]})


@pytest.mark.parametrize(
    "body",
    [b"{", b'{"code":1,"code":0,"message":"valid"}', b'{"code":1,"message":"Already in use","extra":true}'],
)
def test_malformed_individual_stops_with_original_response(tmp_path, manifest, body):
    summary, calls, _, run = simulate(
        tmp_path, manifest, fault=lambda req, _: response(body=body) if req["kind"] == "individual" else None
    )
    assert len(calls) == 3
    assert summary["stop_reason"] in ("malformed_individual_json", "unexpected_individual_schema")
    events, _ = poc.read_events(run / "events.jsonl")
    assert next(e for e in events if e.get("event") == "response" and e["attempt"] == 3)["response"][
        "body_sha256"
    ] == poc.digest(body)


def test_known_account_identity_conflict_stops_before_validator(tmp_path, manifest):
    manifest["candidates"][0]["expected_user_id"] = 99999
    summary, calls, _, _ = simulate(tmp_path, manifest)
    assert len(calls) == 1 and summary["stop_reason"] == "documented_account_id_conflict"


def test_early_wait_wakeups_cannot_shorten_pacing(tmp_path, manifest):
    class EarlyClock(Clock):
        def wait(self, delay):
            self.waits.append(delay)
            self.at += min(3, delay)
            return False

    summary, calls, clock, _ = simulate(tmp_path, manifest, clock=EarlyClock())
    assert len(calls) == 42 and len(clock.waits) == 164
    assert all(g >= 10 for g in summary["measured_post_response_gaps_seconds"])


def test_protocol_change_invalidates_review(tmp_path, manifest, monkeypatch):
    setup(tmp_path, manifest)
    monkeypatch.setattr(poc, "protocol_fingerprint", lambda: "0" * 64)
    with pytest.raises(FileNotFoundError):
        poc.reviewed_manifest(tmp_path)


def test_report_old_run_without_current_manifest_and_reject_path_injection(tmp_path, manifest, monkeypatch):
    _, _, _, run = simulate(tmp_path, manifest)
    monkeypatch.setattr(poc, "DATA", tmp_path)
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    assert poc.main(["report", "--run-id", run.name]) == 0
    assert poc.main(["report", "--run-id", "../runtime"]) == 2


def test_incomplete_manifest_never_reaches_acknowledgment_or_transport(tmp_path):
    poc.prepare(tmp_path)
    with pytest.raises(ValueError, match="incomplete"):
        poc.run_experiment(
            tmp_path,
            acknowledge=lambda _: pytest.fail("Incomplete manifest must block first"),
            transport=lambda _: pytest.fail("No network on incomplete manifest"),
        )
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("value", [None, [], {"version": 1, "candidates": 5}])
def test_invalid_manifest_status_is_offline_and_informative(value):
    assert poc.manifest_status(value)["errors"]


def test_incomplete_read_preserves_exception_partial_bytes(monkeypatch):
    connection = fake_connection(monkeypatch, b"prefix", error=http.client.IncompleteRead(b"suffix", 20))
    r = poc.Transport()({"kind": "bulk", "method": "POST", "url": poc.BULK_URL, "usernames": ["Example"]})
    assert r.body == b"prefixsuffix" and r.error == "incomplete_body" and connection.closed


def test_cleanup_failure_preserves_received_response(monkeypatch):
    connection = fake_connection(monkeypatch, b'{"data":[]}')

    def fail():
        raise OSError("Test cleanup error")

    connection.close = fail
    r = poc.Transport()({"kind": "bulk", "method": "POST", "url": poc.BULK_URL, "usernames": ["Example"]})
    assert r.body == b'{"data":[]}' and r.error == "cleanup_error" and not r.complete


def test_pilot_reuses_exact_source_order_and_preserves_full_manifest(tmp_path, manifest, monkeypatch):
    # Deliberately reverse the fixture rows: the pilot must not sort or cherry-pick.
    manifest["candidates"].reverse()
    pilot = pilot_from_full(tmp_path, manifest)
    source = (tmp_path / "manifest.json").read_bytes()
    expected = [r for r in manifest["candidates"] if r["category"] == "random"]
    assert pilot["candidates"] == expected and len(expected) == 20
    assert pilot["mode"] == "RANDOM_BULK_PILOT"
    assert pilot["sample_origin"]["source_manifest_sha256"] == poc.fingerprint(manifest)
    assert poc.validate_pilot_manifest(pilot) == []
    frozen = (tmp_path / "random-pilot/manifest.json").read_bytes()
    monkeypatch.setattr(poc.secrets, "choice", lambda _: pytest.fail("Never regenerate an existing sample"))
    assert poc.prepare_pilot(tmp_path) == pilot
    assert (tmp_path / "random-pilot/manifest.json").read_bytes() == frozen
    assert (tmp_path / "manifest.json").read_bytes() == source


def test_pilot_fresh_fallback_only_when_original_unavailable(tmp_path, monkeypatch):
    pilot = poc.prepare_pilot(tmp_path)
    names = [r["requested_username"] for r in pilot["candidates"]]
    assert len(set(names)) == 20 and all(len(n) == 4 and poc.valid_name(n) for n in names)
    assert pilot["sample_origin"]["kind"] == "fresh_unbiased_sample_original_unavailable"
    assert pilot["sample_origin"]["source_manifest_sha256"] is None
    assert not (tmp_path / "manifest.json").exists()
    monkeypatch.setattr(poc.secrets, "choice", lambda _: pytest.fail("Generate only once"))
    assert poc.prepare_pilot(tmp_path) == pilot


@pytest.mark.parametrize(
    "change", ["incomplete", "duplicate", "invalid", "wrong_mode", "nonrandom", "ownership", "reordered"]
)
def test_invalid_pilot_blocked_before_ack_or_dispatch(tmp_path, manifest, change):
    pilot = pilot_from_full(tmp_path / "source", manifest)
    if change == "incomplete":
        pilot["candidates"].pop()
    elif change == "duplicate":
        pilot["candidates"][0]["requested_username"] = pilot["candidates"][1]["requested_username"].upper()
    elif change == "invalid":
        pilot["candidates"][0]["requested_username"] = "bad!"
    elif change == "wrong_mode":
        pilot["mode"] = "FULL_SEMANTIC_VALIDATION"
    elif change == "nonrandom":
        pilot["candidates"][0]["category"] = "current"
    elif change == "ownership":
        pilot["candidates"][0]["independently_established"] = True
    else:
        pilot["candidates"].reverse()
    path = setup(tmp_path / "execution", pilot)
    assert poc.validate_pilot_manifest(pilot)
    with pytest.raises(ValueError, match="incomplete"):
        poc.run_experiment(
            path,
            pilot=True,
            acknowledge=lambda _: pytest.fail("Invalid pilot must block first"),
            transport=lambda _: pytest.fail("No dispatch for invalid pilot"),
        )
    assert not (path / "runs").exists()


def test_bad_existing_sample_never_silently_generates_replacement(tmp_path, manifest, monkeypatch):
    manifest["candidates"] = manifest["candidates"][:-1]
    poc.write_new(tmp_path / "manifest.json", manifest)
    source = (tmp_path / "manifest.json").read_bytes()
    monkeypatch.setattr(poc.secrets, "choice", lambda _: pytest.fail("Bad source is not missing source"))
    with pytest.raises(ValueError, match="exactly 20"):
        poc.prepare_pilot(tmp_path)
    assert source == (tmp_path / "manifest.json").read_bytes()
    assert not (tmp_path / "random-pilot").exists()


def test_full_and_pilot_review_gates_are_separate(tmp_path, manifest):
    pilot = pilot_from_full(tmp_path / "source", manifest)
    assert poc.validate_manifest(pilot)
    assert poc.validate_pilot_manifest(manifest)
    path = setup(tmp_path / "execution", pilot)
    with pytest.raises(ValueError, match="incomplete"):
        poc.reviewed_manifest(path)  # Full semantic mode cannot accept a pilot.
    assert poc.reviewed_manifest(path, pilot=True)[0] == pilot
    for row in pilot["candidates"]:
        row["limitations"] += " edited after review"
    (path / "manifest.json").write_bytes(poc.encoded(pilot))
    with pytest.raises(FileNotFoundError):
        poc.reviewed_manifest(path, pilot=True)
    assert not (path / "runs").exists()


def test_pilot_complete_21_attempts_sequential_paced_and_auditable(tmp_path, manifest):
    pilot = pilot_from_full(tmp_path / "source", manifest)
    summary, calls, clock, run = simulate(tmp_path / "execution", pilot, pilot=True)
    names = [r["requested_username"] for r in pilot["candidates"]]
    assert summary["mode"] == "RANDOM_BULK_PILOT" and summary["usernames"] == names
    assert summary["maximum_attempts"] == summary["planned_attempts"] == 21
    assert summary["bulk_requested_usernames"] == summary["individual_attempted_usernames"] == names
    assert len(calls) == summary["complete_http_responses"] == 21
    assert [r["kind"] for r in calls] == ["bulk"] + ["individual"] * 20
    assert calls[0]["usernames"] == names
    assert [r["usernames"][0] for r in calls[1:]] == names
    assert len(clock.waits) == 20 and clock.waits == [10] * 20
    assert summary["elapsed_seconds_including_pacing"] == pytest.approx(202.625)
    assert summary["latency_seconds"]["bulk"]["count"] == 1
    assert summary["latency_seconds"]["individual"]["count"] == 20
    assert summary["taken_api_agreements"] == 20
    assert summary["potential_shortcut_usernames_api_only"] == names
    assert not summary["independently_verified_accuracy"]
    events, _ = poc.read_events(run / "events.jsonl")
    assert events[0]["maximum_attempts"] == 21 and events[0]["planned_attempts"] == 21
    journal = (run / "events.jsonl").read_bytes()
    worksheet = (run / "manual_comparison.csv").read_bytes()
    assert poc.report_run(run)["usernames"] == names
    assert (run / "events.jsonl").read_bytes() == journal
    assert (run / "manual_comparison.csv").read_bytes() == worksheet


@pytest.mark.parametrize("bulk_case", ["missing", "alias"])
def test_pilot_unresolved_is_not_available_or_automatic_contradiction(tmp_path, manifest, bulk_case):
    pilot = pilot_from_full(tmp_path / "source", manifest)

    def fault(request, _):
        if request["kind"] == "bulk":
            data = (
                []
                if bulk_case == "missing"
                else [record(n, name=f"renamed{i}", id=1000 + i) for i, n in enumerate(request["usernames"])]
            )
            return response({"data": data})
        return response({"code": 0, "message": "Username is valid"})

    summary, calls, _, _ = simulate(tmp_path / "execution", pilot, pilot=True, fault=fault)
    assert len(calls) == 21 and summary["stop_reason"] == "completed"
    assert summary["bulk_preliminary_counts"] == {"Unresolved": 20}
    assert summary["individual_preliminary_counts"] == {"Available": 20}
    assert not summary["contradictions"] and not summary["potential_shortcut_usernames_api_only"]
    assert summary["compared_without_conflict"] == 20
    assert (
        len(
            summary["missing_bulk_records"]
            if bulk_case == "missing"
            else summary["historical_or_different_current_names"]
        )
        == 20
    )


def test_pilot_contradiction_preserves_sources_and_stops_after_two_attempts(tmp_path, manifest):
    pilot = pilot_from_full(tmp_path / "source", manifest)
    summary, calls, _, run = simulate(
        tmp_path / "execution",
        pilot,
        pilot=True,
        fault=lambda r, _: (
            response({"code": 0, "message": "Username is valid"}) if r["kind"] == "individual" else None
        ),
    )
    assert len(calls) == 2 and summary["contradictions"]
    assert len(summary["usernames"]) == len(summary["bulk_requested_usernames"]) == 20
    assert summary["individual_attempted_usernames"] == [pilot["candidates"][0]["requested_username"]]
    assert summary["bulk_preliminary_counts"] == {"Taken": 20}
    assert summary["individual_preliminary_counts"]["Available"] == 1
    assert summary["comparison_conclusion_counts"] == {"Unknown": 20}
    assert not summary["potential_shortcut_usernames_api_only"]
    events, _ = poc.read_events(run / "events.jsonl")
    assert len([e for e in events if e["event"] == "response"]) == 2


def test_pilot_invalid_bulk_is_not_reported_as_valid_missing_records(tmp_path, manifest):
    pilot = pilot_from_full(tmp_path / "source", manifest)
    summary, calls, _, run = simulate(
        tmp_path / "execution",
        pilot,
        pilot=True,
        fault=lambda r, _: response({"data": [record(r["usernames"][0])] * 2}),
    )
    assert len(calls) == 1 and summary["stop_reason"] != "completed"
    assert not summary["missing_bulk_records"]
    assert not summary["individual_attempted_usernames"]
    csv_path = next(run.glob("comparison-*.csv"))
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert all(r["bulk_interpretation_stop_reason"] for r in rows)


def test_pilot_budget_enforced_before_and_during_dispatch(tmp_path, manifest, monkeypatch):
    pilot = pilot_from_full(tmp_path / "source", manifest)
    plan = poc.request_plan(pilot)
    monkeypatch.setattr(poc, "request_plan", lambda _: plan)

    # Extend after preflight to exercise the in-loop bound as well.
    def append_extra(request, attempt):
        if attempt == 1:
            plan.append(dict(plan[-1]))

    summary, calls, clock, _ = simulate(tmp_path / "execution", pilot, pilot=True, fault=append_extra)
    assert len(calls) == 21 and len(clock.waits) == 20
    assert summary["stop_reason"] == "request_budget_exhausted"
    path = setup(tmp_path / "preflight", pilot)
    with pytest.raises(ValueError, match="budget"):
        poc.run_experiment(
            path,
            pilot=True,
            acknowledge=lambda _: "RUN " + poc.fingerprint(pilot),
            transport=lambda _: pytest.fail("22 planned attempts must fail before dispatch"),
        )
    assert not (path / "runs").exists()


def test_pilot_cancel_and_metadata_edits_do_not_reset_lifetime_budget(tmp_path, manifest):
    pilot = pilot_from_full(tmp_path / "source", manifest)

    class CancelClock(Clock):
        def wait(self, _):
            return True

    path = tmp_path / "execution"
    summary, calls, _, run = simulate(path, pilot, pilot=True, clock=CancelClock())
    assert len(calls) == 1 and summary["stop_reason"] == "cancelled_during_pacing"
    assert not summary["individual_attempted_usernames"]
    journal = (run / "events.jsonl").read_bytes()
    pilot["semantic_limitations"] += "; revised review notes"
    (path / "manifest.json").write_bytes(poc.encoded(pilot))
    poc.review_manifest(path, pilot=True, acknowledge=lambda _: "REVIEW " + poc.fingerprint(pilot))
    with pytest.raises(FileExistsError):
        poc.run_experiment(
            path,
            pilot=True,
            acknowledge=lambda _: "RUN " + poc.fingerprint(pilot),
            transport=lambda _: pytest.fail("New review cannot reset pilot budget"),
        )
    assert (run / "events.jsonl").read_bytes() == journal


def test_pilot_cli_preparation_status_and_default_remain_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(poc, "DATA", tmp_path)
    assert poc.main(["prepare", "--pilot"]) == 0
    assert "RANDOM_BULK_PILOT" in capsys.readouterr().out
    assert poc.main(["status", "--pilot"]) == 0
    assert '"maximum_attempts": 21' in capsys.readouterr().out
    assert poc.main([]) == 0 and '"mode": "offline"' in capsys.readouterr().out
    assert not (tmp_path / "random-pilot/reviews").exists()
    assert not (tmp_path / "random-pilot/runs").exists()


@pytest.mark.parametrize("current", ["roblox_user_12345678901", "__unusual__", "a" * 128])
def test_bounded_unusual_account_names_are_unresolved_not_fatal(current):
    source = record("Probe", name=current, id=12345678901, displayName="Probe")
    mapping, stop = poc.interpret_bulk(["Probe", "Normal"], response({"data": [source, record("Normal")]}))
    assert not stop and mapping["probe"]["preliminary_classification"] == "Unresolved"
    assert mapping["probe"]["record"] == source
    assert mapping["normal"]["preliminary_classification"] == "Taken"
    assert not poc.compare_evidence({"category": "random"}, mapping["probe"])
    assert poc.compare_evidence({"expected_current_username": "Probe"}, mapping["probe"])


@pytest.mark.parametrize(
    "current", [None, True, 12, [], {}, "", "a" * 129, "x\ny", "x\x00y", "na me", "n\u0430me"]
)
def test_unsafe_current_name_representations_remain_fatal(current):
    mapping, stop = poc.interpret_bulk(
        ["Probe", "Normal"], response({"data": [record("Normal"), record("Probe", name=current, id=13)]})
    )
    assert stop == "unsafe_bulk_record"
    assert all(r["preliminary_classification"] == "Unresolved" for r in mapping.values())


def test_conflicting_ids_for_same_current_name_are_fatal():
    mapping, stop = poc.interpret_bulk(
        ["Probe", "Normal"], response({"data": [record("Probe", name="Normal", id=13), record("Normal")]})
    )
    assert stop == "conflicting_account_ids"
    assert all(r["preliminary_classification"] == "Unresolved" for r in mapping.values())


def stopped_source(root, manifest):
    """Synthetic v1 journal, deliberately retaining its old all-Unresolved interpretation."""
    pilot = pilot_from_full(root / "input", manifest)
    run_id = poc.fingerprint(pilot)
    run = root / "random-pilot/runs" / run_id
    run.parent.mkdir(parents=True)
    setup(run, pilot)
    poc.write_new(run / "review.json", poc.read_json(poc.review_path(run, pilot)))
    names = [r["requested_username"] for r in pilot["candidates"]]
    rows = [record(n, id=1000 + i) for i, n in enumerate(names)]
    rows[12]["name"] = "roblox_user_12345678901"
    journal = poc.Journal(run / "events.jsonl")
    journal.append(
        {
            "event": "begin",
            "mode": poc.PILOT_MODE,
            "manifest_sha256": run_id,
            "maximum_attempts": 21,
            "planned_attempts": 21,
            "interpretation_policy": "bulk-current-name-v1",
            "individual_protocol_sha256": poc.protocol_fingerprint(),
        }
    )
    journal.append(
        {
            "event": "request_intent",
            "attempt": 1,
            "request": poc.request_plan(pilot)[0],
            "bulk_parameters": {"usernames": names, "excludeBannedUsers": False},
            "individual_parameters": None,
            "request_at_utc": poc.utcnow(),
        }
    )
    journal.append(
        {
            "event": "response",
            "attempt": 1,
            "response_at_utc": poc.utcnow(),
            "response": poc.public_response(response({"data": rows})),
        }
    )
    journal.append(
        {
            "event": "bulk_interpretation",
            "attempt": 1,
            "source": "bulk_lookup",
            "stop_reason": "unsafe_bulk_record",
            "independently_verified": False,
            "mapping": {
                poc.key(n): {
                    "preliminary_classification": "Unresolved",
                    "reason": "missing_bulk_record",
                    "api_code": None,
                }
                for n in names
            },
        }
    )
    journal.append({"event": "stop", "reason": "unsafe_bulk_record", "dispatch_intents": 1})
    return run_id, run, pilot


def original_bytes(run):
    return {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}


def edit_source_events(run, edit):
    events, warnings = poc.read_events(run / "events.jsonl")
    assert not warnings
    edit(events)
    (run / "events.jsonl").write_bytes(b"".join(poc.encoded(e) + b"\n" for e in events))


def comparison_setup(root, manifest):
    run_id, source, pilot = stopped_source(root, manifest)
    prepared = poc.prepare_comparison(root, run_id)
    path = root / "individual-comparison" / run_id
    poc.review_manifest(path, comparison=True, acknowledge=lambda _: "REVIEW " + poc.fingerprint(prepared))
    return path, prepared, source, pilot


def test_offline_reanalysis_is_versioned_bound_and_nondestructive(tmp_path, manifest):
    run_id, source, _ = stopped_source(tmp_path, manifest)
    before = original_bytes(source)
    first = poc.reanalyze_pilot(tmp_path, run_id)
    second = poc.reanalyze_pilot(tmp_path, run_id)
    assert first["analysis_file"] != second["analysis_file"]
    assert first["bulk_preliminary_counts"] == {"Taken": 19, "Unresolved": 1}
    assert first["original_stop_reason"] == "unsafe_bulk_record" and not first["corrected_stop_reason"]
    assert first["individual_attempts"] == first["independent_confirmations"] == first["live_requests"] == 0
    analysis = poc.read_json(Path(first["analysis_file"]))["analysis"]
    assert analysis["original_events"][3]["stop_reason"] == "unsafe_bulk_record"
    assert all(
        r["preliminary_classification"] == "Unresolved"
        for r in analysis["original_events"][3]["mapping"].values()
    )
    assert analysis["source_files_sha256"]["events.jsonl"] == poc.digest(before["events.jsonl"])
    assert original_bytes(source) == before


@pytest.mark.parametrize(
    "tamper",
    [
        "body_hash",
        "base64",
        "preview",
        "truncated",
        "incomplete",
        "http",
        "retry_after",
        "challenge",
        "quota",
        "review",
        "binding",
        "request",
        "order",
        "stop",
        "attempt",
        "extra_event",
        "json",
        "id",
        "missing_time",
        "boolean_latency",
        "unexpected_header",
    ],
)
def test_untrusted_saved_evidence_cannot_authorize_comparison(tmp_path, manifest, tamper):
    run_id, source, _ = stopped_source(tmp_path, manifest)

    def edit(events):
        evidence = events[2]["response"]
        if tamper == "body_hash":
            evidence["body_sha256"] = "0" * 64
        elif tamper == "base64":
            evidence["body_base64"] = "!"
        elif tamper == "preview":
            evidence["body_text"] = "altered"
        elif tamper == "incomplete":
            evidence["complete"] = False
        elif tamper == "http":
            evidence["status"] = 429
        elif tamper == "retry_after":
            evidence["headers"]["retry-after"] = "60"
        elif tamper == "quota":
            evidence["headers"]["x-ratelimit-remaining"] = "0"
        elif tamper == "challenge":
            evidence["challenge"] = True
        elif tamper == "binding":
            events[0]["manifest_sha256"] = "0" * 64
        elif tamper == "request":
            events[1]["bulk_parameters"]["excludeBannedUsers"] = True
        elif tamper == "order":
            events[1]["request"]["usernames"].reverse()
        elif tamper == "stop":
            events[4]["reason"] = "http_429"
        elif tamper == "attempt":
            events[2]["attempt"] = 2
        elif tamper == "extra_event":
            events.append({"event": "request_intent", "attempt": 2})
        elif tamper == "json":
            events[2]["response"] = poc.public_response(response(body=b'{"data":[],"data":[]}'))
        elif tamper == "id":
            events[2]["response"] = poc.public_response(response({"data": [record("a000", id=True)]}))
        elif tamper == "missing_time":
            del events[2]["response_at_utc"]
        elif tamper == "boolean_latency":
            evidence["latency_seconds"] = True
        elif tamper == "unexpected_header":
            evidence["headers"]["set-cookie"] = "SyntheticForbiddenHeader"

    edit_source_events(source, edit)
    if tamper == "truncated":
        p = source / "events.jsonl"
        p.write_bytes(p.read_bytes()[:-1])
    if tamper == "review":
        p = source / "review.json"
        value = poc.read_json(p)
        value["acknowledgment"] = "REVIEW wrong"
        p.write_bytes(poc.encoded(value))
    with pytest.raises(ValueError):
        poc.prepare_comparison(tmp_path, run_id)
    assert not (tmp_path / "individual-comparison").exists()


def test_followup_preparation_and_review_are_independent_and_offline(tmp_path, manifest, monkeypatch, capsys):
    run_id, source, pilot = stopped_source(tmp_path, manifest)
    before = original_bytes(source)
    monkeypatch.setattr(poc, "DATA", tmp_path)
    assert poc.main(["reanalyze", "--pilot", "--run-id", run_id]) == 0
    assert poc.main(["prepare", "--comparison-source", run_id]) == 0
    assert poc.main(["status", "--comparison-source", run_id]) == 0
    assert '"maximum_attempts": 20' in capsys.readouterr().out
    path = tmp_path / "individual-comparison" / run_id
    prepared = poc.read_json(path / "manifest.json")
    assert prepared["candidates"] == pilot["candidates"]
    frozen = original_bytes(path)
    assert poc.prepare_comparison(tmp_path, run_id) == prepared and original_bytes(path) == frozen
    with pytest.raises(FileNotFoundError):
        poc.run_experiment(path, comparison=True, transport=lambda _: pytest.fail("No review"))
    assert not (path / "reviews").exists() and not (path / "runs").exists()
    with pytest.raises(ValueError, match="acknowledged"):
        poc.review_manifest(path, comparison=True, acknowledge=lambda _: "REVIEW " + poc.fingerprint(pilot))
    assert original_bytes(source) == before


def test_followup_20_individuals_pacing_and_final_comparison(tmp_path, manifest):
    path, prepared, source, pilot = comparison_setup(tmp_path, manifest)
    before = original_bytes(source)
    clock, calls = Clock(), []
    active = False

    def transport(request):
        nonlocal active
        assert not active and request["kind"] == "individual"
        active = True
        calls.append(copy.deepcopy(request))
        clock.at += 0.125
        active = False
        return response({"code": 1, "message": "Username is already in use"})

    summary = poc.run_experiment(
        path,
        comparison=True,
        transport=transport,
        acknowledge=lambda _: "RUN " + poc.fingerprint(prepared),
        monotonic=clock.now,
        wait=clock.wait,
    )
    names = [r["requested_username"] for r in pilot["candidates"]]
    assert [r["usernames"][0] for r in calls] == names and len(calls) == 20
    assert clock.waits == [10] * 19
    assert summary["elapsed_seconds_including_pacing"] == pytest.approx(192.5)
    assert summary["maximum_attempts"] == summary["dispatch_intents_including_failures"] == 20
    assert summary["bulk_requested_usernames"] == [] and summary["individual_attempted_usernames"] == names
    assert summary["bulk_preliminary_counts"] == {"Taken": 19, "Unresolved": 1}
    assert summary["taken_api_agreements"] == 19 and summary["compared_without_conflict"] == 20
    assert summary["source_bulk_attempts_not_part_of_this_run"] == 1
    assert summary["saved_bulk_original_stop_reason"] == "unsafe_bulk_record"
    assert summary["latency_seconds"]["bulk"]["count"] == 0
    assert not summary["independently_verified_accuracy"]
    run = path / "runs" / poc.fingerprint(prepared)
    with next(run.glob("comparison-*.csv")).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 20 and all(r["individual_response_at_utc"] for r in rows)
    assert all(
        json.loads(r["original_bulk_interpretation"])["preliminary_classification"] == "Unresolved"
        for r in rows
    )
    assert rows[12]["bulk_preliminary_classification"] == "Unresolved"
    assert original_bytes(source) == before
    with pytest.raises(FileExistsError):
        poc.run_experiment(
            path,
            comparison=True,
            acknowledge=lambda _: "RUN " + poc.fingerprint(prepared),
            transport=transport,
        )
    assert len(calls) == 20


@pytest.mark.parametrize(
    "fault",
    [
        response(status=401),
        response(status=403),
        response(status=429),
        response(status=500),
        response(headers={"content-type": "application/json", "retry-after": "60"}),
        response(headers={"content-type": "application/json", "x-ratelimit-remaining": "0"}),
        response(challenge=True),
        response(complete=False),
        response(error="timeout"),
        response(body=b"{"),
        response({"code": 999, "message": "Unexpected"}),
        response({"code": True, "message": "Unexpected"}),
        response({"errors": []}),
        response({"code": 0, "message": "Username is valid"}),
    ],
)
def test_followup_failure_or_conflict_stops_without_retry(tmp_path, manifest, fault):
    path, prepared, source, _ = comparison_setup(tmp_path, manifest)
    before = original_bytes(source)
    calls, clock = [], Clock()

    def transport(request):
        assert request["kind"] == "individual"
        calls.append(request)
        return fault

    summary = poc.run_experiment(
        path,
        comparison=True,
        transport=transport,
        acknowledge=lambda _: "RUN " + poc.fingerprint(prepared),
        monotonic=clock.now,
        wait=clock.wait,
    )
    assert summary["dispatch_intents_including_failures"] == len(calls) == 1
    assert summary["stop_reason"] != "completed" and not clock.waits
    assert summary["comparison_conclusion_counts"] == {"Unknown": 20}
    if fault.body == response({"code": 0, "message": "Username is valid"}).body:
        assert summary["contradictions"] and summary["individual_preliminary_counts"]["Available"] == 1
    with pytest.raises(FileExistsError):
        poc.run_experiment(
            path,
            comparison=True,
            transport=transport,
            acknowledge=lambda _: "RUN " + poc.fingerprint(prepared),
        )
    assert len(calls) == 1 and original_bytes(source) == before


def test_followup_cancellation_and_edited_metadata_cannot_reset_budget(tmp_path, manifest):
    path, prepared, source, _ = comparison_setup(tmp_path, manifest)
    before = original_bytes(source)
    calls = []

    def transport(request):
        calls.append(request)
        return response({"code": 1, "message": "Username is already in use"})

    summary = poc.run_experiment(
        path,
        comparison=True,
        transport=transport,
        acknowledge=lambda _: "RUN " + poc.fingerprint(prepared),
        wait=lambda _: True,
    )
    assert summary["stop_reason"] == "cancelled_during_pacing" and len(calls) == 1
    prepared["semantic_limitations"] += "; revised notes"
    (path / "manifest.json").write_bytes(poc.encoded(prepared))
    poc.review_manifest(path, comparison=True, acknowledge=lambda _: "REVIEW " + poc.fingerprint(prepared))
    with pytest.raises(FileExistsError):
        poc.run_experiment(
            path,
            comparison=True,
            transport=transport,
            acknowledge=lambda _: "RUN " + poc.fingerprint(prepared),
        )
    assert len(calls) == 1 and original_bytes(source) == before


def test_followup_explicit_ack_and_preflight_budget(tmp_path, manifest, monkeypatch):
    path, prepared, _, pilot = comparison_setup(tmp_path, manifest)
    with pytest.raises(ValueError, match="acknowledged"):
        poc.run_experiment(
            path,
            comparison=True,
            acknowledge=lambda _: "RUN " + poc.fingerprint(pilot),
            transport=lambda _: pytest.fail("Original authorization cannot authorize a new run"),
        )
    assert not (path / "runs").exists()
    original_plan = poc.request_plan

    def excess_plan(value):
        plan = original_plan(value)
        if value["mode"] == poc.COMPARISON_MODE:
            plan.append(plan[-1])
        return plan

    monkeypatch.setattr(poc, "request_plan", excess_plan)
    with pytest.raises(ValueError, match="budget"):
        poc.run_experiment(
            path,
            comparison=True,
            acknowledge=lambda _: "RUN " + poc.fingerprint(prepared),
            transport=lambda _: pytest.fail("21 individual attempts are forbidden"),
        )
    assert not (path / "runs").exists()


@pytest.mark.parametrize("tamper", ["source", "analysis", "names", "policy"])
def test_followup_bindings_are_rechecked_before_execution(tmp_path, manifest, tamper):
    path, prepared, source, _ = comparison_setup(tmp_path, manifest)
    if tamper == "source":
        edit_source_events(source, lambda events: events[4].update(note="Source changed since review"))
    elif tamper == "analysis":
        p = path / "source_analysis.json"
        value = poc.read_json(p)
        value["corrected_mapping"]["a000"]["preliminary_classification"] = "Available"
        p.write_bytes(poc.encoded(value))
    else:
        if tamper == "names":
            prepared["candidates"].reverse()
        else:
            prepared["corrected_policy"] = "unknown-policy"
        (path / "manifest.json").write_bytes(poc.encoded(prepared))
    with pytest.raises(ValueError):
        poc.run_experiment(
            path,
            comparison=True,
            acknowledge=lambda _: pytest.fail("Changed evidence blocks first"),
            transport=lambda _: pytest.fail("Never send with changed evidence"),
        )
    assert not (path / "runs").exists()


def test_followup_real_process_crash_blocks_replay_and_preserves_original(tmp_path, manifest):
    path, prepared, source, _ = comparison_setup(tmp_path, manifest)
    before = original_bytes(source)
    code = """
import importlib.util, os, pathlib, socket, sys
spec=importlib.util.spec_from_file_location('experiment',sys.argv[1])
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
def forbidden(*a, **k): raise AssertionError('No live network')
socket.socket.connect=forbidden
path=pathlib.Path(sys.argv[2]);m=p.read_json(path/'manifest.json')
p.run_experiment(path,comparison=True,acknowledge=lambda _: 'RUN '+p.fingerprint(m),
                 transport=lambda _: os._exit(73))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(SCRIPT), str(path)], capture_output=True, timeout=15
    )
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    run = path / "runs" / poc.fingerprint(prepared)
    summary = poc.report_run(run)
    assert summary["unanswered_delivery_uncertain_attempts"] == [1]
    assert summary["individual_preliminary_counts"] == {"NotObserved": 20}
    assert summary["elapsed_seconds_including_pacing"] is None
    archived = original_bytes(run)
    with pytest.raises(FileExistsError):
        poc.run_experiment(
            path,
            comparison=True,
            acknowledge=lambda _: "RUN " + poc.fingerprint(prepared),
            transport=lambda _: pytest.fail("Never replay uncertain delivery"),
        )
    assert original_bytes(run) == archived and original_bytes(source) == before


def expanded_source(root, manifest):
    pilot = pilot_from_full(root, manifest)
    archive = root / "random-pilot/runs" / poc.fingerprint(pilot)
    archive.mkdir(parents=True)
    poc.write_new(archive / "manifest.json", pilot)
    return pilot


def expanded_ack(manifest):
    return lambda prompt: ("SAFE " if "Type SAFE" in prompt else "RUN ") + poc.fingerprint(manifest)


def expanded_setup(root, manifest):
    pilot = expanded_source(root, manifest)
    value = poc.prepare_expanded(root)
    path = root / "expanded-100"
    poc.review_manifest(path, expanded=True, acknowledge=lambda _: "REVIEW " + poc.fingerprint(value))
    return path, value, pilot


def simulate_expanded(path, value, *, fault=None, clock=None):
    clock, calls = clock or Clock(), []
    names = [r["requested_username"] for r in value["candidates"]]
    active = False

    def transport(request):
        nonlocal active
        assert not active
        active = True
        calls.append(copy.deepcopy(request))
        clock.at += 0.125
        if fault and (result := fault(request, len(calls), names)) is not None:
            active = False
            return result
        result = (
            response(
                {
                    "data": [
                        record(n, name=n.upper(), id=1000 + names.index(n))
                        for n in reversed(request["usernames"])
                    ]
                }
            )
            if request["kind"] == "bulk"
            else response({"code": 1, "message": "Username is already in use"})
        )
        active = False
        return result

    result = poc.run_experiment(
        path,
        expanded=True,
        acknowledge=expanded_ack(value),
        transport=transport,
        monotonic=clock.now,
        wait=clock.wait,
    )
    return result, calls, clock, path / "runs" / poc.fingerprint(value)


def test_expanded_uniform_draw_order_exclusions_duplicates_and_frozen_sample(tmp_path, manifest, monkeypatch):
    pilot = expanded_source(tmp_path, manifest)
    excluded = [r["requested_username"] for r in pilot["candidates"]]
    candidates = [f"b{i:03}" for i in range(100)]
    draws = iter("".join([excluded[0], candidates[0], candidates[0], *candidates[1:]]))
    calls = []

    def choice(alphabet):
        assert alphabet == "abcdefghijklmnopqrstuvwxyz0123456789"
        calls.append(alphabet)
        return next(draws)

    monkeypatch.setattr(poc.secrets, "choice", choice)
    value = poc.prepare_expanded(tmp_path)
    assert [r["requested_username"] for r in value["candidates"]] == candidates
    assert len(calls) == 408 and not poc.validate_expanded_manifest(value)
    assert not set(candidates).intersection(excluded)
    path = tmp_path / "expanded-100"
    frozen = original_bytes(path)
    monkeypatch.setattr(poc.secrets, "choice", lambda _: pytest.fail("Never regenerate"))
    assert poc.prepare_expanded(tmp_path) == value and original_bytes(path) == frozen
    plan = poc.request_plan(value)
    assert len(plan) == 105 and [r["kind"] for r in plan] == ["bulk"] * 5 + ["individual"] * 100
    assert [len(r["usernames"]) for r in plan[:5]] == [20] * 5
    assert [n for r in plan[:5] for n in r["usernames"]] == candidates
    assert [r["usernames"][0] for r in plan[5:]] == candidates


@pytest.mark.parametrize(
    "change", ["incomplete", "duplicate", "excluded", "format", "order", "metadata", "ownership"]
)
def test_expanded_invalid_or_altered_sample_is_preserved_and_blocks(tmp_path, manifest, change):
    path, value, pilot = expanded_setup(tmp_path, manifest)
    if change == "incomplete":
        value["candidates"].pop()
    elif change == "duplicate":
        value["candidates"][0]["requested_username"] = value["candidates"][1]["requested_username"].upper()
    elif change == "excluded":
        value["candidates"][0]["requested_username"] = pilot["candidates"][0]["requested_username"]
    elif change == "format":
        value["candidates"][0]["requested_username"] = "no!!"
    elif change == "order":
        value["candidates"].reverse()
    elif change == "metadata":
        value["sample_origin"]["method"] = "unknown"
    else:
        value["candidates"][0]["independently_established"] = True
    (path / "manifest.json").write_bytes(poc.encoded(value))
    frozen = original_bytes(path)
    assert poc.validate_expanded_manifest(value)
    with pytest.raises(ValueError):
        poc.prepare_expanded(tmp_path)
    with pytest.raises(ValueError):
        poc.run_experiment(path, expanded=True, acknowledge=lambda _: pytest.fail("Invalid sample"))
    assert original_bytes(path) == frozen and not (path / "runs").exists()


def test_expanded_success_all_105_sequential_requests_measured_and_reported(tmp_path, manifest):
    path, value, _ = expanded_setup(tmp_path, manifest)
    summary, calls, clock, run = simulate_expanded(path, value)
    names = [r["requested_username"] for r in value["candidates"]]
    assert len(calls) == summary["maximum_attempts"] == summary["dispatch_intents_including_failures"] == 105
    assert summary["bulk_requested_usernames"] == summary["individual_attempted_usernames"] == names
    assert summary["completed_bulk_batches"] == 5
    assert summary["bulk_preliminary_counts"] == {"Taken": 100}
    assert summary["individual_preliminary_counts"] == {"Taken": 100}
    assert summary["taken_api_agreements"] == 100
    assert clock.waits == [10] * 104
    assert summary["deliberate_pacing_seconds"] == 1040
    assert summary["transport_elapsed_seconds"] == pytest.approx(13.125)
    assert summary["elapsed_seconds_including_pacing"] == pytest.approx(1053.125)
    assert summary["unattributed_elapsed_seconds"] == 0
    for kind, count in (("bulk", 5), ("individual", 100)):
        assert summary["observed_phases"][kind] == {
            "dispatch_intents": count,
            "responses": count,
            "complete_http_responses": count,
            "http_status_counts": {"200": count},
        }
        assert summary["latency_seconds"][kind] == {
            "count": count,
            "mean": 0.125,
            "median": 0.125,
            "p95": 0.125,
        }
    savings = summary["hypothetical_bulk_first"]
    assert savings["available"] and savings["potential_individual_requests_avoided"] == 100
    assert (
        savings["hypothetical_total_requests"] == 5
        and savings["estimated_request_count_reduction_percent"] == 95
    )
    assert savings["sustained_throughput_estimate"] is None and not savings["independent_accuracy_verified"]
    with next(run.glob("comparison-*.csv")).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [r["username"] for r in rows] == names
    assert [int(r["bulk_lookup_attempt"]) for r in rows] == [i // 20 + 1 for i in range(100)]
    events, warnings = poc.read_events(run / "events.jsonl")
    assert not warnings and events[0]["safety_acknowledgment"] == "SAFE " + poc.fingerprint(value)
    assert all(
        e["bulk_parameters"]["excludeBannedUsers"] is False
        for e in events
        if e["event"] == "request_intent" and e["request"]["kind"] == "bulk"
    )


def test_expanded_unresolved_cases_and_all_individual_classes_are_distinguished(tmp_path, manifest):
    path, value, _ = expanded_setup(tmp_path, manifest)

    def fault(request, attempt, names):
        if attempt == 1:
            rows = [record(n, id=1000 + names.index(n)) for n in request["usernames"] if n != names[2]]
            rows[0]["name"] = "renamed_account"
            rows[1]["name"] = "roblox_user_12345678901"
            rows[2]["name"] = "another_name"
            return response({"data": rows})
        if request["kind"] == "individual" and request["usernames"][0] in names[:4]:
            return response(
                {"code": [1, 12, 2, 0][names.index(request["usernames"][0])], "message": "Synthetic response"}
            )

    summary, calls, _, _ = simulate_expanded(path, value, fault=fault)
    assert len(calls) == 105 and summary["stop_reason"] == "completed"
    assert summary["bulk_preliminary_counts"] == {"Unresolved": 4, "Taken": 96}
    assert summary["classification_comparisons"] == {
        "TakenApiAgreement": 96,
        "UnresolvedBulk_IndividualTaken": 1,
        "UnresolvedBulk_IndividualReserved": 1,
        "UnresolvedBulk_IndividualInvalid": 1,
        "UnresolvedBulk_IndividualAvailable": 1,
    }
    assert (
        len(summary["missing_bulk_records"]) == 1
        and len(summary["historical_or_different_current_names"]) == 3
    )
    assert not summary["contradictions"]
    assert summary["hypothetical_bulk_first"]["hypothetical_total_requests"] == 9


@pytest.mark.parametrize(
    "kind", ["same_id", "same_current", "duplicate", "unrequested", "bad_type", "malformed"]
)
def test_expanded_conflicts_within_and_across_batches_stop_before_individuals(tmp_path, manifest, kind):
    path, value, _ = expanded_setup(tmp_path, manifest)

    def fault(request, attempt, names):
        if attempt != 2:
            return None
        rows = [record(n, id=1000 + names.index(n)) for n in request["usernames"]]
        if kind == "same_id":
            rows[0]["id"] = 1000
        elif kind == "same_current":
            rows[0]["name"] = names[0]
        elif kind == "duplicate":
            rows.append(rows[0])
        elif kind == "unrequested":
            rows.append(record(names[0], id=1000))
        elif kind == "bad_type":
            rows[0]["id"] = True
        else:
            return response(body=b'{"data":[],"data":[]}')
        return response({"data": rows})

    summary, calls, _, _ = simulate_expanded(path, value, fault=fault)
    assert len(calls) == 2 and all(r["kind"] == "bulk" for r in calls)
    assert summary["stop_reason"] != "completed" and summary["individual_preliminary_counts"] == {
        "NotObserved": 100
    }
    assert not summary["hypothetical_bulk_first"]["available"]
    assert summary["hypothetical_bulk_first"]["estimated_request_count_reduction_percent"] is None
    if kind in ("same_id", "same_current"):
        assert summary["stop_reason"].startswith("cross_batch_conflicting_")
        assert value["candidates"][0]["requested_username"] in summary["bulk_evidence_conflicted_usernames"]
        assert summary["comparison_conclusion_counts"] == {"Unknown": 100}


@pytest.mark.parametrize("attempt", [1, 6])
@pytest.mark.parametrize(
    "fault",
    [
        response(status=401),
        response(status=403),
        response(status=429),
        response(status=500),
        response(headers={"content-type": "application/json", "retry-after": "90"}),
        response(headers={"content-type": "application/json", "x-ratelimit-remaining": "0"}),
        response(challenge=True),
        response(complete=False),
        response(error="timeout"),
        response(body=b"{"),
        response({"code": 999, "message": "Unknown"}),
    ],
)
def test_expanded_restrictions_and_unsafe_responses_stop_at_each_phase(tmp_path, manifest, attempt, fault):
    path, value, _ = expanded_setup(tmp_path, manifest)
    summary, calls, _, run = simulate_expanded(
        path, value, fault=lambda r, a, n: fault if a == attempt else None
    )
    assert len(calls) == summary["dispatch_intents_including_failures"] == attempt
    assert summary["stop_reason"] != "completed"
    assert not summary["hypothetical_bulk_first"]["available"]
    frozen = original_bytes(run)
    with pytest.raises(FileExistsError):
        poc.run_experiment(
            path,
            expanded=True,
            acknowledge=expanded_ack(value),
            transport=lambda _: pytest.fail("Never retry a stopped experiment"),
        )
    assert original_bytes(run) == frozen


def test_expanded_individual_contradiction_preserves_sources_and_stops(tmp_path, manifest):
    path, value, _ = expanded_setup(tmp_path, manifest)
    summary, calls, _, run = simulate_expanded(
        path,
        value,
        fault=lambda r, a, n: response({"code": 0, "message": "Username is valid"}) if a == 6 else None,
    )
    assert len(calls) == 6 and summary["contradictions"]
    assert summary["individual_preliminary_counts"] == {"Available": 1, "NotObserved": 99}
    assert summary["classification_comparisons"] == {"Conflict": 1, "NotCompared": 99}
    assert summary["comparison_conclusion_counts"] == {"Unknown": 100}
    events, _ = poc.read_events(run / "events.jsonl")
    assert len([e for e in events if e["event"] == "response"]) == 6
    assert summary["hypothetical_bulk_first"]["potential_individual_requests_avoided"] is None


def test_expanded_raised_transport_error_records_actual_elapsed_not_default_zero(tmp_path, manifest):
    path, value, _ = expanded_setup(tmp_path, manifest)

    def fault(request, attempt, names):
        raise TimeoutError("Synthetic timeout")

    summary, calls, _, _ = simulate_expanded(path, value, fault=fault)
    assert len(calls) == 1 and summary["stop_reason"] == "transport_exception_TimeoutError"
    assert summary["latency_seconds"]["bulk"]["mean"] == 0.125
    assert summary["transport_elapsed_seconds"] == 0.125
    assert summary["observed_phases"]["bulk"]["complete_http_responses"] == 0


def test_expanded_budget_and_request_order_guard(tmp_path, manifest, monkeypatch):
    path, value, _ = expanded_setup(tmp_path, manifest)
    original_plan = poc.request_plan
    plan = poc.request_plan(value)
    monkeypatch.setattr(poc, "request_plan", lambda _: plan)

    def append_after_preflight(request, attempt, names):
        if attempt == 105:
            plan.append(copy.deepcopy(plan[-1]))

    summary, calls, _, _ = simulate_expanded(path, value, fault=append_after_preflight)
    assert len(calls) == 105 and summary["stop_reason"] == "request_budget_exhausted"
    second = tmp_path / "second"
    second.mkdir()
    second_path, second_value, _ = expanded_setup(second, manifest)
    with pytest.raises(ValueError, match="budget"):
        poc.run_experiment(
            second_path,
            expanded=True,
            acknowledge=expanded_ack(second_value),
            transport=lambda _: pytest.fail("106 requests must fail before dispatch"),
        )
    assert not (second_path / "runs").exists()
    # Same plan length and kinds, but a duplicated individual name must also stop.
    second_plan = original_plan(second_value)
    second_plan[6] = copy.deepcopy(second_plan[5])
    monkeypatch.setattr(poc, "request_plan", lambda _: second_plan)
    summary, calls, _, _ = simulate_expanded(second_path, second_value)
    assert len(calls) == 6 and summary["stop_reason"] == "unexpected_expanded_request_plan"


def test_expanded_source_and_postreview_manifest_changes_block_before_dispatch(tmp_path, manifest):
    with pytest.raises(FileNotFoundError):
        poc.prepare_expanded(tmp_path)
    assert not (tmp_path / "expanded-100").exists()
    path, value, _ = expanded_setup(tmp_path, manifest)
    value["note"] = "A valid but unreviewed manifest change"
    (path / "manifest.json").write_bytes(poc.encoded(value))
    with pytest.raises(FileNotFoundError):
        poc.run_experiment(path, expanded=True, acknowledge=lambda _: pytest.fail("Review must bind changes"))
    poc.review_manifest(path, expanded=True, acknowledge=lambda _: "REVIEW " + poc.fingerprint(value))
    source = tmp_path / "random-pilot/manifest.json"
    pilot = poc.read_json(source)
    pilot["note"] = "Source no longer matches the archived pilot"
    source.write_bytes(poc.encoded(pilot))
    with pytest.raises((ValueError, FileNotFoundError)):
        poc.run_experiment(path, expanded=True, acknowledge=lambda _: pytest.fail("Changed exclusion source"))
    assert not (path / "runs").exists()


def test_expanded_timing_separates_journal_overhead_from_transport_and_waits(tmp_path, manifest, monkeypatch):
    path, value, _ = expanded_setup(tmp_path, manifest)
    clock = Clock()
    original_append = poc.Journal.append

    def disk_cost(journal, event):
        clock.at += 0.01
        original_append(journal, event)

    monkeypatch.setattr(poc.Journal, "append", disk_cost)
    summary, _, _, _ = simulate_expanded(path, value, clock=clock)
    assert summary["transport_elapsed_seconds"] == pytest.approx(13.125)
    assert summary["deliberate_pacing_seconds"] < 1040
    assert summary["unattributed_elapsed_seconds"] > 0
    assert min(summary["measured_post_response_gaps_seconds"]) >= 10 - 1e-8
    assert summary["elapsed_seconds_including_pacing"] == pytest.approx(
        summary["transport_elapsed_seconds"]
        + summary["deliberate_pacing_seconds"]
        + summary["unattributed_elapsed_seconds"]
    )


def test_expanded_cancellation_during_pacing_is_durable_and_does_not_reset(tmp_path, manifest):
    path, value, _ = expanded_setup(tmp_path, manifest)

    class CancelClock(Clock):
        def wait(self, seconds):
            self.at += 0.75
            return True

    summary, calls, _, run = simulate_expanded(path, value, clock=CancelClock())
    assert len(calls) == 1 and summary["stop_reason"] == "cancelled_during_pacing"
    assert summary["deliberate_pacing_seconds"] == 0.75
    assert summary["transport_elapsed_seconds"] == 0.125
    assert summary["elapsed_seconds_including_pacing"] == 0.875
    frozen = original_bytes(run)
    value["note"] = "New review notes cannot reset the lifetime experiment limit"
    (path / "manifest.json").write_bytes(poc.encoded(value))
    poc.review_manifest(path, expanded=True, acknowledge=lambda _: "REVIEW " + poc.fingerprint(value))
    with pytest.raises(FileExistsError):
        poc.run_experiment(path, expanded=True, acknowledge=expanded_ack(value))
    assert original_bytes(run) == frozen


def test_expanded_offline_cli_and_distinct_review_safety_and_run_confirmations(
    tmp_path, manifest, monkeypatch, capsys
):
    pilot = expanded_source(tmp_path, manifest)
    original = {str(p): p.read_bytes() for p in (tmp_path / "random-pilot").rglob("*") if p.is_file()}
    monkeypatch.setattr(poc, "DATA", tmp_path)
    assert poc.main(["--expanded-100"]) == 0
    assert poc.main(["prepare", "--expanded-100"]) == 0
    assert poc.main(["status", "--expanded-100"]) == 0
    assert '"maximum_attempts": 105' in capsys.readouterr().out
    path = tmp_path / "expanded-100"
    value = poc.read_json(path / "manifest.json")
    with pytest.raises(FileNotFoundError):
        poc.run_experiment(path, expanded=True, acknowledge=lambda _: pytest.fail("No new review yet"))
    with pytest.raises(ValueError, match="acknowledged"):
        poc.review_manifest(path, expanded=True, acknowledge=lambda _: "REVIEW " + poc.fingerprint(pilot))
    poc.review_manifest(path, expanded=True, acknowledge=lambda _: "REVIEW " + poc.fingerprint(value))
    with pytest.raises(ValueError, match="safety"):
        poc.run_experiment(path, expanded=True, acknowledge=lambda _: "RUN " + poc.fingerprint(value))
    with pytest.raises(ValueError, match="Live execution not acknowledged"):
        poc.run_experiment(path, expanded=True, acknowledge=lambda _: "SAFE " + poc.fingerprint(value))
    assert not (path / "runs").exists()
    assert {str(p): p.read_bytes() for p in (tmp_path / "random-pilot").rglob("*") if p.is_file()} == original
    # Report stays offline and preserves the existing journal and user's worksheet.
    summary, _, _, run = simulate_expanded(path, value)
    journal = (run / "events.jsonl").read_bytes()
    worksheet = (run / "manual_comparison.csv").read_bytes()
    assert poc.main(["report", "--expanded-100"]) == 0
    assert summary["mode"] == poc.EXPANDED_MODE
    assert (run / "events.jsonl").read_bytes() == journal
    assert (run / "manual_comparison.csv").read_bytes() == worksheet


@pytest.mark.parametrize("crash_at", ["dispatch", "wait"])
def test_expanded_process_crash_has_honest_partial_timings_and_no_replay(tmp_path, manifest, crash_at):
    path, value, _ = expanded_setup(tmp_path, manifest)
    code = """
import importlib.util, os, pathlib, socket, sqlite3, sys
spec=importlib.util.spec_from_file_location('experiment',sys.argv[1])
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
def forbidden(*a, **k): raise AssertionError('No live network or database')
socket.socket.connect=forbidden;sqlite3.connect=forbidden
path=pathlib.Path(sys.argv[2]);m=p.read_json(path/'manifest.json')
def ack(prompt): return ('SAFE ' if 'Type SAFE' in prompt else 'RUN ')+p.fingerprint(m)
def transport(request):
    if sys.argv[3]=='dispatch': os._exit(73)
    return p.protocol.Response(status=200,body=b'{"data":[]}',headers={'content-type':'application/json'})
p.run_experiment(path,expanded=True,acknowledge=ack,transport=transport,wait=lambda _: os._exit(73))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(SCRIPT), str(path), crash_at], capture_output=True, timeout=15
    )
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    run = path / "runs" / poc.fingerprint(value)
    summary = poc.report_run(run)
    assert summary["elapsed_seconds_including_pacing"] is None
    assert summary["dispatch_intents_including_failures"] == 1
    assert summary["stop_reason"] == "interrupted_no_durable_stop"
    assert summary["hypothetical_bulk_first"]["estimated_request_count_reduction_percent"] is None
    if crash_at == "dispatch":
        assert summary["transport_elapsed_seconds"] is None
        assert summary["dispatches_without_timing"] == [1]
        assert summary["unanswered_delivery_uncertain_attempts"] == [1]
    else:
        assert summary["deliberate_pacing_seconds"] is None and summary["unfinished_pacing_ids"]
    with pytest.raises(FileExistsError):
        poc.run_experiment(
            path, expanded=True, acknowledge=expanded_ack(value), transport=lambda _: pytest.fail("No replay")
        )
