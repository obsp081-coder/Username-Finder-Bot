"""Account evidence protocol reused from the reviewed bulk-v2 experiment.

Account identity is separate from registration validation. No availability claims.
"""

import base64
import hashlib
import http.client
import json
import math
import re
import ssl
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from . import roblox_protocol as protocol

BULK_URL = "https://users.roblox.com/v1/usernames/users"
POLICY = "bulk-account-evidence-v1"
TIMEOUT_SECONDS = 10.0
MAX_FILE = 4 * 1024 * 1024
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


def safe_account_name(value):
    # A bounded account representation, not a claim about registration syntax.
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,128}", value) is not None


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
    """Account evidence only; never a registration classification."""
    keys = [key(n) for n in names]
    unresolved = {
        k: {"state": "NoMatchingBulkRecord", "reason": "missing_bulk_record", "api_code": None} for k in keys
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
    records, ids, current_ids = {}, {}, {}
    for row in body["data"]:
        if (
            not isinstance(row, dict)
            or not {"requestedUsername", "name", "id"} <= row.keys()
            or row.keys() - {"requestedUsername", "name", "id", "displayName", "hasVerifiedBadge"}
            or not valid_name(row.get("requestedUsername"))
            or not safe_account_name(row.get("name"))
            or type(row.get("id")) is not int
            or row["id"] <= 0
            or ("displayName" in row and not isinstance(row["displayName"], str))
            or ("hasVerifiedBadge" in row and type(row["hasVerifiedBadge"]) is not bool)
        ):
            return unresolved, "unsafe_bulk_record"
        requested, current, uid = key(row["requestedUsername"]), row["name"].lower(), row["id"]
        if requested not in unresolved:
            return unresolved, "unrequested_bulk_record"
        if requested in records:
            return unresolved, "duplicate_or_conflicting_bulk_record"
        if uid in ids and ids[uid] != current:
            return unresolved, "conflicting_account_names"
        if current in current_ids and current_ids[current] != uid:
            return unresolved, "conflicting_account_ids"
        current_ids[current] = uid
        ids[uid], records[requested] = current, row
    # No positive result survives an invalid later record in the same response.
    for requested, row in records.items():
        matched = row["name"].lower() == requested
        unresolved[requested] = {
            "state": "CurrentAccountMatchByAPI" if matched else "HistoricalOrDifferentCurrentName",
            "reason": "current_name_and_id_api_only" if matched else "historical_or_different_current_name",
            "api_code": None,
            "record": row,
        }
    return unresolved, ""


def cross_batch_conflict(previous, mapping):
    by_id, by_current = {}, {}
    for name, value in previous.items():
        if row := value.get("record"):
            by_id[row["id"]] = (row["name"].lower(), name)
            by_current[row["name"].lower()] = (row["id"], name)
    for name, value in mapping.items():
        if name in previous:
            return "cross_batch_duplicate_mapping", [name]
        if not (row := value.get("record")):
            continue
        current, uid = row["name"].lower(), row["id"]
        if uid in by_id and by_id[uid][0] != current:
            return "cross_batch_conflicting_account_names", [by_id[uid][1], name]
        if current in by_current and by_current[current][0] != uid:
            return "cross_batch_conflicting_account_ids", [by_current[current][1], name]
    return "", []


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
