"""X parser/direct transport reference from experiments/x-username-poc/x_poc.py.

Kept independent of research files. Negative/error parsing remains pinned to the preserved PoC.
Explicit fixed proxy transport is delegated to x_connection without changing classification.
Positive API indications cannot establish claimability and are conservatively Unknown.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

ENDPOINT = "https://api.x.com/i/users/username_available.json"


CLASSES = ("Available", "Taken", "Reserved", "Invalid", "Unknown")


MAX_BODY = 1024 * 1024


HEADERS = {
    "content-type",
    "date",
    "retry-after",
    "x-rate-limit-limit",
    "x-rate-limit-remaining",
    "x-rate-limit-reset",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}


SECRET = re.compile(r"password|token|cookie|authorization|session|secret|csrf|email", re.I)


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SECRET.search(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"(?is)<script\b[^>]*>.*?(?:</script>|$)", "<script>[REDACTED]</script>", value)
    value = re.sub(r"""(?i)\bvalue\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", 'value="[REDACTED]"', value)
    value = re.sub(
        r"""(?i)((?:password|token|cookie|authorization|session|secret|csrf|email)[\w-]*["']?\s*[:=]\s*)["']?[^\s,;<>"']+""",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [REDACTED]", value)
    return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", value)


@dataclass
class Response:
    status: int | None = None
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    error: str = ""
    complete: bool = True


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def fetch(username, timeout, *, connection=None):
    if connection is not None:
        from .x_connection import request as routed_request
        from .x_connection import validate

        route = validate(connection)
        if route["mode"] == "proxy":
            return routed_request(
                ENDPOINT + "?" + urllib.parse.urlencode({"username": username}), timeout, route
            )
    request = urllib.request.Request(
        ENDPOINT + "?" + urllib.parse.urlencode({"username": username}),
        headers={"Accept": "application/json", "User-Agent": "XUsernameResearch/0.1"},
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    received_response = False
    try:
        try:
            stream = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            stream = error
        with stream:
            received_response = True
            # Only selected diagnostic headers, never cookies or authentication secrets.
            headers = {key.lower(): value for key, value in stream.headers.items() if key.lower() in HEADERS}
            for key in stream.headers:
                if re.search(r"challenge|captcha|cf-mitigated", key, re.I):
                    headers["challenge-indicator"] = "present"
            body = stream.read(MAX_BODY + 1)
            return Response(stream.status, body[:MAX_BODY], headers, complete=len(body) <= MAX_BODY)
    except (TimeoutError, urllib.error.URLError, OSError) as error:
        from .overnight import transport_failure

        response = Response(error=type(error).__name__, complete=False)
        return response if received_response else transport_failure(response, error)


def validate_syntax(username):
    """Change-handle syntax only; no availability or keyword-reservation inference."""
    issues = []
    if len(username) < 5:
        issues.append("too_short")
    if len(username) > 15:
        issues.append("too_long")
    if re.search(r"[^A-Za-z0-9_]", username):
        issues.append("unsupported_characters")
    return {"valid": not issues, "issues": issues, "boundary_needs_verification": len(username) == 15}


def length_rejection(parsed, username):
    """Require coherent, bounded length wording and an independent local violation."""
    msg = " ".join(parsed.get("msg", "").split())
    desc = " ".join(parsed.get("desc", "").split())
    patterns = (
        (
            "short",
            "too_short",
            r"(?:Your )?username (?:is )?too short[.!]?",
            r"(?:Your )?username must be (?:longer than 4 characters|at least 5 characters(?: long)?)[.!]?",
        ),
        (
            "long",
            "too_long",
            r"(?:Your )?username (?:is )?too long[.!]?",
            r"(?:Your )?username (?:must be (?:shorter than 15 characters|at most 15 characters(?: long)?)"
            r"|cannot be longer than 15 characters)[.!]?",
        ),
    )
    issues = validate_syntax(username)["issues"]
    for kind, issue, msg_pattern, desc_pattern in patterns:
        if (
            issue in issues
            and re.fullmatch(msg_pattern, msg, re.I)
            and re.fullmatch(desc_pattern, desc, re.I)
        ):
            return kind
    return None


def positive_availability(message):
    without_negatives = re.sub(r"\bnot\s+available\b|\bunavailable\b", "", message, flags=re.I)
    return bool(re.search(r"\bavailable\b", without_negatives, re.I))


def parse(response, username):
    parsed = None
    try:

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate_json_key")
                result[key] = value
            return result

        parsed = json.loads(
            response.body.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")),
        )
    except (ValueError, UnicodeError, RecursionError):
        pass
    reason = ""
    remaining_values = [
        response.headers[key].strip()
        for key in ("x-rate-limit-remaining", "x-ratelimit-remaining")
        if key in response.headers
    ]
    if response.error:
        reason = "network_or_execution_error:" + response.error
    elif not response.complete:
        reason = "incomplete_or_oversized_body"
    elif response.status != 200:
        reason = f"unexpected_http_status:{response.status}"
    elif "retry-after" in response.headers:
        reason = "retry_after_present"
    elif any(re.fullmatch(r"-?\d+", value) and int(value) <= 0 for value in remaining_values):
        reason = "exhausted_rate_limit"
    elif any(not value.isdecimal() for value in remaining_values):
        reason = "unexpected_rate_limit_header"
    elif response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        reason = "unexpected_or_missing_content_type"
    elif "challenge-indicator" in response.headers or re.search(
        r"captcha|challenge|access denied|authenticate|authentication|log[ -]?in required|rate limit",
        response.body.decode("utf-8", errors="replace"),
        re.I,
    ):
        reason = "challenge_or_access_restriction"
    elif not isinstance(parsed, dict) or set(parsed) - {"valid", "reason", "msg", "desc"}:
        reason = "invalid_json_or_unexpected_schema"
    elif (
        type(parsed.get("valid")) is not bool
        or not isinstance(parsed.get("reason"), str)
        or any(not isinstance(parsed[key], str) for key in ("msg", "desc") if key in parsed)
    ):
        reason = "unexpected_field_types"
    else:
        pair = parsed["valid"], parsed["reason"]
        message = " ".join(parsed.get(key, "") for key in ("msg", "desc"))
        # Historically documented pairs only. These remain preliminary API indications.
        if pair == (True, "available"):
            if re.search(r"taken|not available|unavailable|invalid|reserved", message, re.I):
                return "Unknown", "contradictory_message", parsed
            if validate_syntax(username)["valid"]:
                return "Unknown", "api_available_does_not_establish_claimability", parsed
            reason = "available_conflicts_with_documented_local_syntax"
        elif pair == (False, "taken"):
            if positive_availability(message) or re.search(r"\breserved\b|\binvalid\b", message, re.I):
                return "Unknown", "contradictory_message", parsed
            return "Taken", "historical_pair_taken_unverified_current_semantics", parsed
        elif pair in ((False, "is_banned_word"), (False, "contains_banned_word")):
            if positive_availability(message) or re.search(r"\btaken\b|\binvalid\b", message, re.I):
                return "Unknown", "contradictory_message", parsed
            return "Reserved", f"preliminary_api_{pair[1]}_rejection_not_account_ownership", parsed
        elif pair == (False, "improper_format"):
            if re.search(r"taken|reserved", message, re.I) or positive_availability(message):
                return "Unknown", "contradictory_message", parsed
            if validate_syntax(username)["valid"]:
                return "Unknown", "format_rejection_conflicts_with_documented_local_syntax", parsed
            return (
                "Invalid",
                "preliminary_api_improper_format_syntax_rejection_not_ownership_or_availability",
                parsed,
            )
        elif pair == (False, "invalid_username"):
            kind = length_rejection(parsed, username)
            if kind:
                return "Invalid", f"preliminary_api_invalid_username_explicit_{kind}_length_rejection", parsed
            reason = "unsupported_or_contradictory_invalid_username_evidence"
        else:
            # Other rejection reasons have no established mapping.
            reason = "unrecognized_or_contradictory_pair"
    return "Unknown", reason, parsed
