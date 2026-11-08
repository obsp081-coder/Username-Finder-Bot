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
