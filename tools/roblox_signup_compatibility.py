"""Isolated signup-contract experiment: no CLI, runner, storage or default connection.

The user reported the browser contract on 2026-11-09. This module does not
establish GET/POST equivalence or permission for live checks. Supply a mocked
or verified loopback TLS connection factory for offline tests only.
"""

import base64
import hashlib
import http.client
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from username_finder import roblox_protocol as protocol
from username_finder.roblox import result_for

SIGNUP_URL = protocol.ENDPOINT + "?urlLocale=ar_001"
SIGNUP_BIRTHDAY = "2000-01-01T00:00:00.000Z"


@dataclass(frozen=True)
class SignupRequest:
    method: str
    url: str
    body: bytes
    headers: tuple[tuple[str, str], ...]


def build_request(username):
    if not isinstance(username, str):
        raise TypeError("Username must be a string")
    body = json.dumps(
        {"username": username, "context": "Signup", "birthday": SIGNUP_BIRTHDAY},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return SignupRequest("POST", SIGNUP_URL, body, (("Content-Type", "application/json"),))


class _SignupConnection:
    """Adapt only the wire request; preserve the production transport byte-for-byte."""

    def __init__(self, connection, request):
        self.connection, self.current_request = connection, request

    @property
    def sock(self):
        return self.connection.sock

    def connect(self):
        self.connection.connect()

    def close(self):
        self.connection.close()

    def getresponse(self):
        return self.connection.getresponse()

    def request(self, method, target, *, headers):
        request = self.current_request()
        destination = urlsplit(request.url)
        if method != "GET" or target != destination.path + "?" + destination.query:
            raise ValueError("Unexpected experimental request adapter contract")
        self.connection.request(
            request.method, target, body=request.body, headers={**headers, **dict(request.headers)}
        )


class ExperimentalSignupTransport(protocol.PersistentTransport):
    """Reuse verified TLS, framing, size bounds and no-replay transport semantics.

    An explicit offline connection factory is mandatory. No cookies, auth,
    CSRF-token negotiation, automatic retries or production fallback exist.
    """

    def __init__(self, *, connection_factory, context=None):
        if not callable(connection_factory):
            raise TypeError("An explicit offline connection factory is required")

        def offline_connection():
            connection = connection_factory()
            if isinstance(connection, http.client.HTTPConnection) and (
                not isinstance(connection, http.client.HTTPSConnection)
                or connection.host not in ("localhost", "127.0.0.1", "::1")
            ):
                connection.close()
                raise ValueError("Experimental real connections must use loopback HTTPS")
            return _SignupConnection(connection, lambda: self._signup_request)

        super().__init__(context=context, connection_factory=offline_connection)

    def fetch(self, username):
        self._signup_request = build_request(username)
        response = super().fetch(self._signup_request.url)
        # Even pre-dispatch failure is not offered to production retry machinery.
        response.retryable = False
        return response


def result_for_signup(username, response, *, checked_at=None):
    """Keep raw response evidence; replace GET metadata with the actual POST contract."""
    request = build_request(username)
    result = result_for(username, response, checked_at=checked_at)
    if "csrf" in result["message"].casefold() or "token validation failed" in result["message"].casefold():
        result["classification"], result["reason"] = "Unknown", "csrf_failure"
    result.update(
        experimental=True,
        request_method=request.method,
        request_url=request.url,
        parameters=json.loads(request.body),
        query_parameters={"urlLocale": "ar_001"},
        request_headers=dict(request.headers),
        request_body_base64=base64.b64encode(request.body).decode("ascii"),
        request_body_sha256=hashlib.sha256(request.body).hexdigest(),
    )
    return result
