"""Verbatim transport and parser from the isolated 2026-11-07 Roblox PoC.

Contract: https://create.roblox.com/docs/cloud/reference/domains/auth.md
The API indication is preliminary; signup-context equivalence is not proven.
No account credentials, proxies, redirects, retries, or concurrent requests.
"""

import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

ENDPOINT = "https://auth.roblox.com/v1/usernames/validate"


BIRTHDAY = "2000-01-01T00:00:00Z"


CONTEXT = 0


TIMEOUT_SECONDS = 10.0


MAX_BODY = 1024 * 1024


CLASSES = ("Available", "Taken", "Reserved", "Invalid", "Unknown")


CODES = {
    0: ("Available", "ValidUsername"),
    1: ("Taken", "AlreadyInUseError"),
    2: ("Invalid", "ModerationError"),
    3: ("Invalid", "InvalidLengthError"),
    4: ("Invalid", "StartsOrEndsWithUnderscoreError"),
    5: ("Invalid", "TooManyUnderscoresError"),
    6: ("Invalid", "ContainsSpacesError"),
    7: ("Invalid", "InvalidCharactersError"),
    10: ("Invalid", "ContainsPiiError"),
    12: ("Reserved", "ContainsReservedUsernameError"),
}


def request_url(username):
    return (
        ENDPOINT
        + "?"
        + urllib.parse.urlencode({"Username": username, "Birthday": BIRTHDAY, "Context": CONTEXT})
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Response:
    status: int | None = None
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    elapsed: float = 0.0
    error: str = ""
    complete: bool = True
    challenge: bool = False


def error_kind(exc):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, ssl.SSLError):
        return "tls_error"
    return "network_error"


def fetch(url):
    """One GET, verified TLS, no proxy/cookie/authentication/redirect/retry handling."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": "RobloxUsernameValidationResearch/0.1 (20-name read-only study)",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        },
    )
    result = Response()
    started = time.perf_counter()
    try:
        try:
            stream = opener.open(request, timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            stream = exc  # Preserve non-2xx bodies too, without interpreting HTTP error codes as enum codes.
        with stream:
            result.status = stream.code
            result.headers = {
                key.lower(): value
                for key, value in stream.headers.items()
                if key.lower()
                in (
                    "content-type",
                    "content-encoding",
                    "date",
                    "retry-after",
                    "x-ratelimit-limit",
                    "x-ratelimit-remaining",
                    "x-ratelimit-reset",
                )
            }
            # Record presence, not opaque challenge/session IDs. Never follow or solve challenges.
            result.challenge = any(key.lower().startswith("rblx-challenge-") for key in stream.headers)
            parts, length = [], 0
            while length <= MAX_BODY:
                chunk = stream.read1(min(65536, MAX_BODY + 1 - length))
                if not chunk:
                    break
                parts.append(chunk)
                length += len(chunk)
                result.body = b"".join(parts)
            if length > MAX_BODY:
                result.complete = False
                result.error = "response_too_large"
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        from .overnight import transient_exception

        result.error = error_kind(exc)
        result.retryable = transient_exception(exc)
        result.complete = False
    finally:
        result.elapsed = time.perf_counter() - started
    return result


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def reject_constant(_):
    raise ValueError("nonstandard JSON number")


def classify(response):
    """Return a documented API indication only; never an independently verified result."""
    if response.challenge:
        return "Unknown", "challenge_header", None, ""
    if response.error or not response.complete:
        return "Unknown", response.error or "incomplete_body", None, ""
    if response.status != 200:
        reason = {
            429: "rate_limited",
            401: "access_restricted",
            403: "access_restricted",
            408: "timeout",
        }.get(response.status, "http_error")
        if response.status is not None and 300 <= response.status < 400:
            reason = "redirect_not_followed"
        return "Unknown", reason, None, ""
    if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
        return "Unknown", "unexpected_content_encoding", None, ""
    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return "Unknown", "unexpected_content_type", None, ""
    try:
        value = json.loads(
            response.body.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant
        )
    except (UnicodeError, ValueError, RecursionError):
        return "Unknown", "malformed_json", None, ""
    if not isinstance(value, dict) or "errors" in value:
        return "Unknown", "unexpected_response_schema", None, ""
    code, message = value.get("code"), value.get("message")
    if type(code) is not int or not isinstance(message, str) or not message.strip():
        return "Unknown", "unexpected_response_schema", None, ""
    text = message.casefold()
    if any(
        word in text
        for word in (
            "captcha",
            "challenge",
            "rate limit",
            "too many requests",
            "access denied",
            "verify you are human",
        )
    ):
        return "Unknown", "restriction_message", code, message
    if code not in CODES:
        return "Unknown", "undocumented_code", code, message
    if text.strip() in ("string", "error"):
        return "Unknown", "ambiguous_message", code, message
    if (
        code == 0
        and any(
            word in text
            for word in ("already", "not valid", "invalid", "reserved", "not available", "not allowed")
        )
    ) or (code != 0 and text.strip().rstrip(".") == "username is valid"):
        return "Unknown", "conflicting_code_and_message", code, message
    classification, symbol = CODES[code]
    return classification, "documented_" + symbol + "; api_indication_only", code, message
