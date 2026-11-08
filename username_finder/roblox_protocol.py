"""Preserved parser/one-shot transport, plus an owned sequential HTTPS session.

Contract: https://create.roblox.com/docs/cloud/reference/domains/auth.md
The API indication is preliminary; signup-context equivalence is not proven.
No account credentials, proxies, redirects, retries, or concurrent requests.
"""

import http.client
import json
import select
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


class PersistentTransport:
    """One verified connection, no redirects or replay after possible dispatch.

    Readable idle sockets are retired before dispatch. A closure racing with
    dispatch fails conservatively; this class never replays a request.
    """

    def __init__(self, *, context=None, connection_factory=None):
        self.context = context or ssl.create_default_context()
        if not self.context.check_hostname or self.context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("Verified TLS and hostname checking are required")
        self.context.set_alpn_protocols(["http/1.1"])
        self.connection_factory = connection_factory or (
            lambda: http.client.HTTPSConnection(
                "auth.roblox.com", timeout=TIMEOUT_SECONDS, context=self.context
            )
        )
        self.connection = None

    def close(self):
        connection, self.connection = self.connection, None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                # Cleanup failure must not erase a durable response or replay it.
                pass

    def fetch(self, url):
        target = urllib.parse.urlsplit(url)
        if (
            target.scheme != "https"
            or target.netloc != "auth.roblox.com"
            or target.path != "/v1/usernames/validate"
            or target.fragment
        ):
            raise ValueError("Unexpected Roblox validation destination")
        result, dispatched = Response(), False
        started = time.perf_counter()
        try:
            if self.connection is not None and self.connection.sock is not None:
                sock = self.connection.sock
                if sock.pending() or select.select([sock], [], [], 0)[0]:
                    # No response is pending between sequential requests. Readability
                    # may be EOF, TLS housekeeping or unsolicited bytes; reconnect
                    # before dispatch rather than probing by sending a username.
                    self.close()
            if self.connection is None:
                self.connection = self.connection_factory()
            connection = self.connection
            # Connect separately: only a failure before dispatch can qualify for
            # the existing bounded overnight transport recovery.
            if connection.sock is None:
                connection.connect()
            dispatched = True
            connection.request(
                "GET",
                target.path + ("?" + target.query if target.query else ""),
                headers={
                    "User-Agent": "RobloxUsernameValidationResearch/0.1 (20-name read-only study)",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Cache-Control": "no-cache",
                },
            )
            with connection.getresponse() as stream:
                result.status = stream.status
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
                result.challenge = any(key.lower().startswith("rblx-challenge-") for key in stream.headers)
                lengths = stream.headers.get_all("Content-Length", [])
                if len(lengths) > 1 or (lengths and stream.headers.get("Transfer-Encoding")):
                    result.error, result.complete = "ambiguous_response_framing", False
                parts, length = [], 0
                while not result.error and length <= MAX_BODY:
                    chunk = stream.read1(min(65536, MAX_BODY + 1 - length))
                    if not chunk:
                        break
                    parts.append(chunk)
                    length += len(chunk)
                    result.body = b"".join(parts)
                if length > MAX_BODY:
                    result.complete, result.error = False, "response_too_large"
                elif not result.error and stream.length not in (None, 0):
                    result.complete, result.error = False, "incomplete_body"
                if (
                    stream.will_close
                    or result.error
                    or result.status != 200
                    or result.challenge
                    or "retry-after" in result.headers
                    or result.headers.get("x-ratelimit-remaining") == "0"
                ):
                    self.close()
        except (OSError, http.client.HTTPException) as exc:
            from .overnight import transient_exception

            if isinstance(exc, http.client.IncompleteRead) and exc.partial:
                result.body = (result.body + exc.partial)[: MAX_BODY + 1]
            result.error, result.complete = error_kind(exc), False
            result.retryable = not dispatched and transient_exception(exc)
            self.close()
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
