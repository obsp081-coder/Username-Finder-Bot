"""Offline by default; a bounded, explicitly reviewed Roblox semantic experiment.

No application stores, jobs, GUI, credentials, retries, or concurrency are used.
All private inputs and evidence live under this experiment's ignored output/.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import http.client
import json
import math
import re
import secrets
import ssl
import statistics
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from username_finder import roblox_protocol as protocol  # noqa: E402

BULK_URL = "https://users.roblox.com/v1/usernames/users"
DATA = Path(__file__).resolve().parent / "output"
MAX_REQUESTS = 42
BATCH_SIZE = 20
PACE_SECONDS = 10.0
TIMEOUT_SECONDS = 10.0
MAX_FILE = 4 * 1024 * 1024
CLASSES = ("Available", "Taken", "Reserved", "Invalid", "Unknown")
CATEGORIES = ("current", "former", "restricted", "random")
SAFE_HEADERS = {
    "content-type",
    "content-encoding",
    "content-length",
    "date",
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}


def utcnow():
    return datetime.now(UTC).isoformat()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def strict_json(raw):
    if len(raw) > MAX_FILE:
        raise ValueError("JSON file exceeds the experiment limit")
    return json.loads(
        raw.decode("utf-8"), object_pairs_hook=protocol.unique_object, parse_constant=protocol.reject_constant
    )


def read_json(path):
    with path.open("rb") as stream:
        return strict_json(stream.read(MAX_FILE + 1))


def valid_name(value):
    return (
        isinstance(value, str)
        and 3 <= len(value) <= 20
        and re.fullmatch(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)?", value) is not None
    )


def key(value):
    if not valid_name(value):
        raise ValueError("Invalid ASCII Roblox username")
    return value.lower()


def fingerprint(manifest):
    return digest(encoded(manifest))


def protocol_fingerprint():
    return digest(Path(protocol.__file__).read_bytes())


def write_new(path, value):
    """Exclusive creation plus fsync: never replace a manifest or observation."""
    with path.open("xb") as stream:
        stream.write(encoded(value) + b"\n")
        stream.flush()
        import os

        os.fsync(stream.fileno())


def data_directory(path=DATA):
    path = Path(path)
    # The CLI has no arbitrary file/directory option. Reject redirected output/.
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError("Redirected experiment data directory is not allowed")
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare(path=DATA):
    path = data_directory(path)
    names = set()
    while len(names) < 20:
        names.add("".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4)))
    rows = []
    for category, count in (("current", 10), ("former", 5)):
        for _ in range(count):
            rows.append(
                {
                    "requested_username": "",
                    "category": category,
                    "evidence_reference": "",
                    "expected_current_username": "",
                    "expected_user_id": None,
                    "independently_established": False,
                    "hypothesis": "",
                    "limitations": "Public evidence not supplied",
                }
            )
    for name in sorted(names):
        rows.append(
            {
                "requested_username": name,
                "category": "random",
                "evidence_reference": "",
                "expected_current_username": "",
                "expected_user_id": None,
                "independently_established": False,
                "hypothesis": "Locally generated without known availability selection",
                "limitations": "No current ownership or claimability evidence",
            }
        )
    manifest = {
        "version": 1,
        "candidates": rows,
        "restricted_case_gap": "No documented account-state cases supplied; semantics unverified",
    }
    write_new(path / "manifest.json", manifest)
    return manifest


def validate_manifest(manifest):
    """No public evidence is fabricated or verified through a network request."""
    errors = []
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
        return ["Unsupported manifest version"]
    rows = manifest.get("candidates")
    if not isinstance(rows, list) or not 35 <= len(rows) <= 40:
        return ["Manifest needs 10 current, 5 former, 20 random, and 0-5 restricted records"]
    counts, seen, variants = Counter(), set(), 0
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            errors.append(f"Row {i}: expected an object")
            continue
        category, name = row.get("category"), row.get("requested_username")
        if category not in CATEGORIES:
            errors.append(f"Row {i}: unsupported category")
            continue
        counts[category] += 1
        if not valid_name(name):
            errors.append(f"Row {i}: missing or invalid username")
            continue
        if key(name) in seen:
            errors.append(f"Row {i}: case-insensitive duplicate")
        seen.add(key(name))
        if not isinstance(row.get("hypothesis"), str) or not row["hypothesis"].strip():
            errors.append(f"Row {i}: relationship/hypothesis missing")
        if not isinstance(row.get("limitations"), str):
            errors.append(f"Row {i}: limitations must be stated")
        if type(row.get("independently_established")) is not bool:
            errors.append(f"Row {i}: evidence status must be a boolean")
        account_id = row.get("expected_user_id")
        if account_id is not None and (type(account_id) is not int or account_id <= 0):
            errors.append(f"Row {i}: malformed expected account id")
        if category == "random":
            if len(name) != 4:
                errors.append(f"Row {i}: generated candidate must have four characters")
            continue
        ref = row.get("evidence_reference")
        ref_url = urlsplit(ref) if isinstance(ref, str) else None
        if (
            not ref_url
            or ref_url.scheme != "https"
            or not ref_url.hostname
            or ref_url.username
            or ref_url.password
            or ref_url.query
            or ref_url.fragment
        ):
            errors.append(f"Row {i}: public HTTPS evidence URL required, without secrets/query/fragment")
        if row.get("independently_established") is not True:
            errors.append(f"Row {i}: documented relationship not independently established by reviewer")
        current = row.get("expected_current_username")
        if category in ("current", "former"):
            if not valid_name(current):
                errors.append(f"Row {i}: documented current username missing")
            elif category == "current":
                if key(name) != key(current):
                    errors.append(f"Row {i}: current username relationship conflicts")
                elif name != current:
                    variants += 1
            elif key(name) == key(current):
                errors.append(f"Row {i}: former name must differ from documented current name")
        elif row.get("account_state") not in ("banned", "disabled", "retired", "restricted"):
            errors.append(f"Row {i}: documented account state missing")
    if (counts["current"], counts["former"], counts["random"]) != (10, 5, 20) or counts["restricted"] > 5:
        errors.append("Incorrect category counts")
    if variants != 4:
        errors.append("Exactly four current-name case-variation records are required")
    if counts["restricted"] < 5 and not str(manifest.get("restricted_case_gap", "")).strip():
        errors.append("Missing restricted-case coverage must be documented explicitly")
    return errors


def manifest_status(manifest):
    rows = manifest.get("candidates", []) if isinstance(manifest, dict) else []
    rows = rows if isinstance(rows, list) else []
    return {
        "fingerprint": fingerprint(manifest),
        "errors": validate_manifest(manifest),
        "categories": dict(Counter(str(r.get("category")) for r in rows if isinstance(r, dict))),
        "restricted_case_gap": manifest.get("restricted_case_gap", "") if isinstance(manifest, dict) else "",
        "independent_evidence_note": "Reviewer declarations; no automatic source verification",
    }


def response_stop(response):
    if len(response.body) > protocol.MAX_BODY:
        return "response_too_large"
    if response.error or not response.complete:
        return response.error or "incomplete_body"
    if response.challenge:
        return "challenge"
    if response.status != 200:
        return f"http_{response.status}"
    if "retry-after" in response.headers:
        return "retry_after_review_required"
    remaining = response.headers.get("x-ratelimit-remaining")
    if remaining is not None:
        try:
            if not math.isfinite(float(remaining)):
                return "unexpected_rate_limit_header"
            if float(remaining) <= 0:
                return "quota_exhausted_review_required"
        except ValueError:
            return "unexpected_rate_limit_header"
    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return "unexpected_content_type"
    if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
        return "unexpected_content_encoding"
    return ""


def interpret_bulk(names, response):
    """An API-only candidate Taken result; never Available or independently verified."""
    keys = [key(n) for n in names]
    unresolved = {
        k: {"preliminary_classification": "Unresolved", "reason": "missing_bulk_record", "api_code": None}
        for k in keys
    }
    if len(keys) != len(set(keys)):
        return unresolved, "duplicate_request_names"
    stop = response_stop(response)
    if stop:
        return unresolved, stop
    try:
        body = strict_json(response.body)
    except (ValueError, UnicodeError, RecursionError):
        return unresolved, "malformed_bulk_json"
    if not isinstance(body, dict) or set(body) != {"data"} or not isinstance(body["data"], list):
        return unresolved, "unexpected_bulk_schema"
    records, ids = {}, {}
    for row in body["data"]:
        if (
            not isinstance(row, dict)
            or not {"requestedUsername", "name", "id"} <= row.keys()
            or row.keys() - {"requestedUsername", "name", "id", "displayName", "hasVerifiedBadge"}
            or not valid_name(row.get("requestedUsername"))
            or not valid_name(row.get("name"))
            or type(row.get("id")) is not int
            or row["id"] <= 0
            or ("displayName" in row and not isinstance(row["displayName"], str))
            or ("hasVerifiedBadge" in row and type(row["hasVerifiedBadge"]) is not bool)
        ):
            return unresolved, "unsafe_bulk_record"
        requested, current, uid = key(row["requestedUsername"]), key(row["name"]), row["id"]
        if requested not in unresolved:
            return unresolved, "unrequested_bulk_record"
        if requested in records:
            return unresolved, "duplicate_or_conflicting_bulk_record"
        if uid in ids and ids[uid] != current:
            return unresolved, "conflicting_account_names"
        ids[uid], records[requested] = current, row
    # No positive result survives an invalid later record in the same response.
    for requested, row in records.items():
        matched = key(row["name"]) == requested
        unresolved[requested] = {
            "preliminary_classification": "Taken" if matched else "Unresolved",
            "reason": "current_name_and_id_api_only" if matched else "historical_or_different_current_name",
            "api_code": None,
            "record": row,
        }
    return unresolved, ""


def compare_evidence(candidate, bulk, individual=None):
    row = bulk.get("record")
    if row:
        expected = candidate.get("expected_current_username")
        uid = candidate.get("expected_user_id")
        if valid_name(expected) and key(expected) != key(row["name"]):
            return "documented_current_name_conflict"
        if uid is not None and uid != row["id"]:
            return "documented_account_id_conflict"
    if (
        individual
        and (bulk["preliminary_classification"] == "Taken" or candidate["category"] == "current")
        and individual["classification"] != "Taken"
    ):
        return "bulk_or_documented_current_vs_individual_conflict"
    return ""


def request_plan(manifest):
    rows = manifest["candidates"]
    bulk = [
        {
            "kind": "bulk",
            "method": "POST",
            "url": BULK_URL,
            "usernames": [r["requested_username"] for r in rows[i : i + BATCH_SIZE]],
        }
        for i in range(0, len(rows), BATCH_SIZE)
    ]
    individual = [
        {
            "kind": "individual",
            "method": "GET",
            "url": protocol.request_url(r["requested_username"]),
            "usernames": [r["requested_username"]],
        }
        for r in rows
    ]
    return bulk + individual


class Transport:
    """One direct HTTPS request, no redirects/proxies/cookies/auth/replay."""

    def __call__(self, request):
        target = urlsplit(request["url"])
        expected = (
            ("users.roblox.com", "/v1/usernames/users", "POST")
            if request["kind"] == "bulk"
            else ("auth.roblox.com", "/v1/usernames/validate", "GET")
        )
        if (
            target.scheme != "https"
            or (target.netloc, target.path, request["method"]) != expected
            or target.fragment
        ):
            raise ValueError("Destination not allowed")
        headers = {
            "User-Agent": "RobloxBulkSemanticExperiment/1.0",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        }
        body = None
        if request["kind"] == "bulk":
            headers["Content-Type"] = "application/json"
            body = encoded({"usernames": request["usernames"], "excludeBannedUsers": False})
        response = protocol.Response()
        started = time.monotonic()
        connection = http.client.HTTPSConnection(
            target.hostname, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context()
        )

        def remaining_timeout():
            remaining = TIMEOUT_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            return remaining

        try:
            connection.connect()
            connection.sock.settimeout(remaining_timeout())
            connection.request(
                request["method"],
                target.path + ("?" + target.query if target.query else ""),
                body=body,
                headers=headers,
            )
            connection.sock.settimeout(remaining_timeout())
            with connection.getresponse() as stream:
                response.status = stream.status
                response.headers = {
                    k.lower(): v for k, v in stream.headers.items() if k.lower() in SAFE_HEADERS
                }
                response.challenge = (
                    any(k.lower().startswith(("rblx-challenge-", "cf-chl-")) for k in stream.headers)
                    or stream.headers.get("cf-mitigated", "").lower() == "challenge"
                )
                if any(len(stream.headers.get_all(header, [])) > 1 for header in SAFE_HEADERS):
                    response.error = "ambiguous_response_headers"
                lengths = stream.headers.get_all("Content-Length", [])
                if len(lengths) > 1 or (lengths and stream.headers.get("Transfer-Encoding")):
                    response.error = "ambiguous_response_framing"
                while not response.error and len(response.body) <= protocol.MAX_BODY:
                    remaining = remaining_timeout()
                    if connection.sock is not None:
                        connection.sock.settimeout(remaining)
                    chunk = stream.read1(min(65536, protocol.MAX_BODY + 1 - len(response.body)))
                    if not chunk:
                        break
                    response.body += chunk
                    remaining_timeout()
                if len(response.body) > protocol.MAX_BODY:
                    response.body = response.body[: protocol.MAX_BODY]
                    response.error = "response_too_large"
                if lengths and (not lengths[0].isdigit() or int(lengths[0]) != len(response.body)):
                    response.error = response.error or "incomplete_body"
        except KeyboardInterrupt:
            response.error = "cancelled_during_request_delivery_uncertain"
        except http.client.IncompleteRead as exc:
            response.body = (response.body + exc.partial)[: protocol.MAX_BODY]
            response.error = "incomplete_body"
        except (OSError, http.client.HTTPException) as exc:
            response.error = protocol.error_kind(exc)
        finally:
            try:
                connection.close()
            except OSError:
                response.error = response.error or "cleanup_error"
            response.complete = not bool(response.error)
            response.elapsed = time.monotonic() - started
        return response


def public_response(response):
    raw = response.body[: protocol.MAX_BODY]
    # Do not persist opaque session/challenge tokens if unexpectedly returned in JSON.
    sensitive = re.search(rb'"(?:access_token|token|session|cookie|authorization)"\s*:', raw, re.I)
    return {
        "status": response.status,
        "headers": {k: v for k, v in response.headers.items() if k in SAFE_HEADERS},
        "latency_seconds": response.elapsed,
        "error": response.error,
        "complete": response.complete,
        "challenge": response.challenge,
        "body_sha256": digest(response.body),
        "body_base64": "" if sensitive else base64.b64encode(raw).decode(),
        "body_text": "" if sensitive else raw[:65536].decode("utf-8", errors="replace"),
        "body_text_is_preview": len(raw) > 65536,
        "body_omitted_for_sensitive_fields": bool(sensitive),
    }


class Journal:
    def __init__(self, path):
        self.path = path

    def append(self, event):
        import os

        with self.path.open("ab") as stream:
            stream.write(encoded(dict(event, recorded_at_utc=utcnow())) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())


def read_events(path):
    events, warnings = [], []
    with path.open("rb") as stream:
        number = 0
        while line := stream.readline(MAX_FILE + 1):
            number += 1
            try:
                event = strict_json(line)
                if not isinstance(event, dict):
                    raise ValueError("Non-object event")
                events.append(event)
            except (ValueError, UnicodeError, RecursionError):
                warnings.append(f"Unparseable journal record {number}; original bytes preserved")
                break
    return events, warnings


def review_path(path, manifest):
    return path / "reviews" / (fingerprint(manifest) + "-" + protocol_fingerprint()[:16] + ".json")


def reviewed_manifest(path=DATA):
    manifest = read_json(path / "manifest.json")
    errors = validate_manifest(manifest)
    if errors:
        raise ValueError("Manifest incomplete: " + "; ".join(errors))
    review = read_json(review_path(path, manifest))
    if (
        not isinstance(review, dict)
        or review.get("manifest_sha256") != fingerprint(manifest)
        or review.get("protocol_sha256") != protocol_fingerprint()
        or review.get("acknowledgment") != "REVIEW " + fingerprint(manifest)
    ):
        raise ValueError("Manifest or protocol changed since explicit review")
    return manifest, review


def describe(manifest):
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps(manifest_status(manifest), ensure_ascii=False, indent=2))
    print(f"Endpoints: POST {BULK_URL}; GET {protocol.ENDPOINT}")
    print("Budget: at most 42 attempts; sequential; timeout 10s; post-response pacing >=10s.")
    print("Stop on any failure/restriction/ambiguity/conflict; no retries or automatic resume.")
    print("API results are preliminary; no registration, credentials, or production database access.")
    print("Run only with other Roblox checkers stopped and no unresolved provider restriction.")


def review_manifest(path=DATA, *, acknowledge=input):
    path = data_directory(path)
    manifest = read_json(path / "manifest.json")
    describe(manifest)
    errors = validate_manifest(manifest)
    if errors:
        raise ValueError("Review blocked: " + "; ".join(errors))
    answer = acknowledge(
        "Confirm public evidence and missing-category limitations. Type REVIEW <fingerprint>: "
    )
    if answer != "REVIEW " + fingerprint(manifest):
        raise ValueError("Review not acknowledged")
    (path / "reviews").mkdir(exist_ok=True)
    write_new(
        review_path(path, manifest),
        {
            "manifest_sha256": fingerprint(manifest),
            "protocol_sha256": protocol_fingerprint(),
            "reviewed_at_utc": utcnow(),
            "acknowledgment": answer,
            "evidence_status": "Explicit reviewer declaration; not automatically verified",
        },
    )


def run_experiment(
    path=DATA, *, transport=None, acknowledge=input, cancel=None, wait=None, monotonic=time.monotonic
):
    path = data_directory(path)
    manifest, review = reviewed_manifest(path)
    describe(manifest)
    answer = acknowledge("Manual live execution only. Type RUN <fingerprint>: ")
    if answer != "RUN " + fingerprint(manifest):
        raise ValueError("Live execution not acknowledged")
    plan = request_plan(manifest)
    if len(plan) > MAX_REQUESTS:
        raise ValueError("Request budget exceeded before dispatch")
    runs = path / "runs"
    runs.mkdir(exist_ok=True)
    run = runs / fingerprint(manifest)
    run.mkdir()  # Existing evidence, even a crashed intent, permanently blocks implicit replay.
    write_new(run / "manifest.json", manifest)
    write_new(run / "review.json", review)
    journal = Journal(run / "events.jsonl")
    journal.append(
        {
            "event": "begin",
            "experiment_id": str(uuid.uuid4()),
            "manifest_sha256": fingerprint(manifest),
            "maximum_attempts": MAX_REQUESTS,
            "planned_attempts": len(plan),
            "pacing_seconds": PACE_SECONDS,
            "timeout_seconds": TIMEOUT_SECONDS,
            "experiment_source_sha256": digest(Path(__file__).read_bytes()),
            "individual_protocol_sha256": protocol_fingerprint(),
            "interpretation_policy": "bulk-current-name-v1",
        }
    )
    cancel = cancel or threading.Event()
    wait = wait or cancel.wait
    transport = transport or Transport()
    bulk_results, attempts, stop, previous_finished = {}, 0, "completed", None
    candidates = {key(r["requested_username"]): r for r in manifest["candidates"]}
    try:
        for request in plan:
            if cancel.is_set():
                stop = "cancelled_before_request"
                break
            if attempts >= MAX_REQUESTS:
                stop = "request_budget_exhausted"
                break
            while previous_finished is not None:
                remaining = PACE_SECONDS - (monotonic() - previous_finished)
                if remaining <= 0:
                    break
                if wait(remaining) or cancel.is_set():
                    stop = "cancelled_during_pacing"
                    break
            if stop != "completed":
                break
            if cancel.is_set():
                stop = "cancelled_before_request"
                break
            attempts += 1
            started = monotonic()
            journal.append(
                {
                    "event": "request_intent",
                    "attempt": attempts,
                    "request": request,
                    "request_at_utc": utcnow(),
                    "gap_since_previous_response_seconds": (
                        None if previous_finished is None else started - previous_finished
                    ),
                    "bulk_parameters": (
                        {"usernames": request["usernames"], "excludeBannedUsers": False}
                        if request["kind"] == "bulk"
                        else None
                    ),
                    "individual_parameters": (
                        None
                        if request["kind"] == "bulk"
                        else {
                            "Username": request["usernames"][0],
                            "Birthday": protocol.BIRTHDAY,
                            "Context": protocol.CONTEXT,
                        }
                    ),
                }
            )
            try:
                response = transport(request)
            except KeyboardInterrupt:
                stop = "cancelled_delivery_uncertain"
                break
            except Exception as exc:
                response = protocol.Response(
                    error="transport_exception_" + type(exc).__name__, complete=False
                )
            previous_finished = monotonic()
            evidence = public_response(response)
            # Raw evidence is durable before interpretation, even if the parser crashes.
            journal.append(
                {"event": "response", "attempt": attempts, "response": evidence, "response_at_utc": utcnow()}
            )
            stop = response_stop(response)
            if evidence["body_omitted_for_sensitive_fields"]:
                stop = "unexpected_sensitive_response_fields"
            if stop:
                break
            if request["kind"] == "bulk":
                mapping, stop = interpret_bulk(request["usernames"], response)
                if not stop:
                    for name, bulk in mapping.items():
                        stop = compare_evidence(candidates[name], bulk)
                        if stop:
                            break
                journal.append(
                    {
                        "event": "bulk_interpretation",
                        "attempt": attempts,
                        "mapping": mapping,
                        "stop_reason": stop,
                        "source": "bulk_lookup",
                        "independently_verified": False,
                    }
                )
                bulk_results.update(mapping)
            else:
                name = key(request["usernames"][0])
                try:
                    value = strict_json(response.body)
                except (ValueError, UnicodeError, RecursionError):
                    stop = "malformed_individual_json"
                    break
                if not isinstance(value, dict) or set(value) != {"code", "message"}:
                    stop = "unexpected_individual_schema"
                    break
                classification, reason, code, message = protocol.classify(response)
                result = {
                    "classification": classification,
                    "reason": reason,
                    "api_code": code,
                    "message": message,
                    "preliminary": True,
                    "independently_verified": False,
                }
                stop = (
                    "unknown_individual_" + reason
                    if classification == "Unknown"
                    else compare_evidence(candidates[name], bulk_results[name], result)
                )
                journal.append(
                    {
                        "event": "individual_interpretation",
                        "attempt": attempts,
                        "username_key": name,
                        "result": result,
                        "source": "individual_validator",
                        "stop_reason": stop,
                    }
                )
            if stop:
                break
            stop = "completed"
    except KeyboardInterrupt:
        stop = "cancelled"
    except Exception as exc:
        stop = "unexpected_execution_error_" + type(exc).__name__
    finally:
        journal.append({"event": "stop", "reason": stop, "dispatch_intents": attempts})
    return report_run(run)


def report_run(run):
    manifest = read_json(run / "manifest.json")
    events, warnings = read_events(run / "events.jsonl")
    intents = {e["attempt"]: e for e in events if e.get("event") == "request_intent"}
    responses = {e["attempt"]: e for e in events if e.get("event") == "response"}
    mappings, individuals, contradictions = {}, {}, []
    conflicted_names = set()
    for e in events:
        if e.get("event") == "bulk_interpretation":
            mappings.update(e["mapping"])
        if e.get("event") == "individual_interpretation":
            individuals[e["username_key"]] = e["result"]
        if "conflict" in e.get("stop_reason", ""):
            contradictions.append({"attempt": e["attempt"], "reason": e["stop_reason"]})
            conflicted_names.update(key(n) for n in intents[e["attempt"]]["request"]["usernames"])
    rows = []
    for candidate in manifest["candidates"]:
        name = key(candidate["requested_username"])
        bulk, individual = mappings.get(name, {}), individuals.get(name, {})
        linked = {
            kind: next(
                (
                    a
                    for a, e in intents.items()
                    if e["request"]["kind"] == kind and name in [key(n) for n in e["request"]["usernames"]]
                ),
                None,
            )
            for kind in ("bulk", "individual")
        }
        observed = {kind: responses.get(a, {}).get("response", {}) for kind, a in linked.items()}
        rows.append(
            {
                "username": candidate["requested_username"],
                "category": candidate["category"],
                "documented_evidence_reference": candidate["evidence_reference"],
                "documented_current_username": candidate.get("expected_current_username", ""),
                "documented_independently_established": candidate["independently_established"],
                "bulk_lookup_attempt": linked["bulk"],
                "bulk_http_status": observed["bulk"].get("status"),
                "bulk_latency_seconds": observed["bulk"].get("latency_seconds"),
                "bulk_preliminary_classification": bulk.get("preliminary_classification", "NotObserved"),
                "bulk_reason": bulk.get("reason", ""),
                "bulk_api_code": "",
                "individual_classification": individual.get("classification", "NotObserved"),
                "individual_validator_attempt": linked["individual"],
                "individual_http_status": observed["individual"].get("status"),
                "individual_latency_seconds": observed["individual"].get("latency_seconds"),
                "individual_api_code": individual.get("api_code"),
                "individual_reason": individual.get("reason", ""),
                "comparison_status": (
                    "Conflict"
                    if name in conflicted_names
                    else "ComparedNoConflict"
                    if individual
                    else "NotCompared"
                ),
                "comparison_conclusion": (
                    "Unknown" if name in conflicted_names else individual.get("classification", "Unknown")
                ),
                "independent_manual_classification": "Unknown",
            }
        )
    stops = [e for e in events if e.get("event") == "stop"]
    latencies = {
        kind: [
            r["response"]["latency_seconds"]
            for attempt, r in responses.items()
            if intents[attempt]["request"]["kind"] == kind
        ]
        for kind in ("bulk", "individual")
    }
    summary = {
        "run_directory": str(run.resolve()),
        "manifest_sha256": fingerprint(manifest),
        "candidates": len(rows),
        "dispatch_intents_including_failures": len(intents),
        "responses_recorded": len(responses),
        "http_status_observed": sum(r["response"]["status"] is not None for r in responses.values()),
        "complete_http_responses": sum(
            r["response"]["status"] is not None and r["response"]["complete"] for r in responses.values()
        ),
        "unanswered_delivery_uncertain_attempts": sorted(set(intents) - responses.keys()),
        "uncertain_transport_outcomes": [
            a for a, r in responses.items() if r["response"]["error"] or not r["response"]["complete"]
        ],
        "stop_reason": (
            "journal_integrity_review_required"
            if warnings
            else stops[-1]["reason"]
            if stops
            else "interrupted_no_durable_stop"
        ),
        "journal_warnings": warnings,
        "contradictions": contradictions,
        "bulk_preliminary_counts": dict(Counter(r["bulk_preliminary_classification"] for r in rows)),
        "bulk_interpreted_candidates": len(mappings),
        "strict_current_match_percent_of_interpreted": (
            100 * sum(r["preliminary_classification"] == "Taken" for r in mappings.values()) / len(mappings)
            if mappings
            else None
        ),
        "unresolved_percent_of_interpreted": (
            100
            * sum(r["preliminary_classification"] == "Unresolved" for r in mappings.values())
            / len(mappings)
            if mappings
            else None
        ),
        "individual_preliminary_counts": dict(Counter(r["individual_classification"] for r in rows)),
        "comparison_conclusion_counts": dict(Counter(r["comparison_conclusion"] for r in rows)),
        "request_count_note": "Intent count bounds attempts; uncertain intents are not confirmed deliveries",
        "independently_verified_accuracy": False,
        "remaining_semantics_unverified": True,
        "restricted_case_gap": manifest.get("restricted_case_gap", ""),
        "latency_seconds": {
            k: {
                "count": len(v),
                "mean": statistics.mean(v) if v else None,
                "p95": sorted(v)[math.ceil(0.95 * len(v)) - 1] if v else None,
            }
            for k, v in latencies.items()
        },
        "measured_post_response_gaps_seconds": [
            e["gap_since_previous_response_seconds"]
            for e in intents.values()
            if e["gap_since_previous_response_seconds"] is not None
        ],
        "restriction_observations": [
            {
                "attempt": a,
                "status": r["response"]["status"],
                "headers": r["response"]["headers"],
                "error": r["response"]["error"],
            }
            for a, r in responses.items()
            if r["response"]["status"] != 200
            or "retry-after" in r["response"]["headers"]
            or r["response"]["challenge"]
            or r["response"]["headers"].get("x-ratelimit-remaining") in ("0", "0.0")
        ],
    }
    # Reports are versioned, never overwrite evidence or a user's manual worksheet.
    identity = str(uuid.uuid4())
    write_new(run / f"report-{identity}.json", summary)
    csv_path = run / f"comparison-{identity}.csv"
    with csv_path.open("x", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    manual = run / "manual_comparison.csv"
    if not manual.exists():
        with manual.open("x", newline="", encoding="utf-8-sig") as stream:
            fields = [
                "username",
                "priority",
                "observed_at_utc",
                "official_interface_context",
                "exact_message",
                "allows_proceeding",
                "manual_classification",
                "limitations",
            ]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            taken = 0
            for row in rows:
                priority = []
                if contradictions:
                    priority.append("Review run contradictions")
                if row["individual_classification"] == "Available":
                    priority.append("API Available")
                if row["bulk_preliminary_classification"] == "Taken" and taken < 10:
                    priority.append("Bulk Taken sample")
                    taken += 1
                if row["category"] == "former":
                    priority.append("Documented former username")
                writer.writerow(
                    {
                        "username": row["username"],
                        "priority": "; ".join(priority),
                        "manual_classification": "Unknown",
                    }
                )
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", nargs="?", choices=("status", "prepare", "review", "live", "report"), default="status"
    )
    parser.add_argument("--run-id", help="Report an archived run by its full manifest SHA256")
    args = parser.parse_args(argv)
    try:
        if args.run_id and (args.action != "report" or not re.fullmatch(r"[0-9a-f]{64}", args.run_id)):
            raise ValueError("--run-id requires report and a full lowercase SHA256")
        if args.action == "prepare":
            value = manifest_status(prepare(DATA))
        elif args.action == "review":
            review_manifest(DATA)
            value = {"review_saved": True, "live_requests": 0}
        elif args.action == "live":
            if not sys.stdin.isatty():
                raise ValueError("Live mode requires a user-operated interactive terminal")
            value = run_experiment(DATA)
        elif args.action == "report":
            identity = args.run_id or fingerprint(read_json(DATA / "manifest.json"))
            value = report_run(DATA / "runs" / identity)
        elif (DATA / "manifest.json").exists():
            value = manifest_status(read_json(DATA / "manifest.json"))
            value["live_requests"] = 0
        else:
            value = {"mode": "offline", "live_requests": 0, "next": "prepare"}
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RecursionError) as exc:
        print(f"Blocked: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Cancelled; no automatic retry or resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
