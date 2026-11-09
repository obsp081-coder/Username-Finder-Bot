"""Offline signup-contract compatibility; no provider traffic or persistent jobs."""

import base64
import hashlib
import http.client
import json
import socket
import ssl
from urllib.parse import parse_qs, urlsplit

import pytest

from tools.roblox_pipeline_benchmark import Handler, local_server
from tools.roblox_signup_compatibility import (
    SIGNUP_URL,
    ExperimentalSignupTransport,
    build_request,
    result_for_signup,
)
from username_finder import roblox_protocol as protocol
from username_finder.overnight import retryable_response
from username_finder.roblox import result_for


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    original = socket.create_connection

    def connect(address, *args, **kwargs):
        assert address[0] in ("localhost", "127.0.0.1", "::1"), "Provider traffic is forbidden"
        return original(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", connect)


class SignupHandler(Handler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.posts.append((self.command, self.path, dict(self.headers), body))
        self.path = protocol.request_url("Bench00000000")
        self.do_GET()  # Reuse the existing bounded/chunked/failure test server.


def session(server, context, *, timeout=1):
    server.RequestHandlerClass = SignupHandler
    server.posts = []
    return ExperimentalSignupTransport(
        context=context,
        connection_factory=lambda: http.client.HTTPSConnection(
            "localhost", server.server_port, context=context, timeout=timeout
        ),
    )


def test_builder_exact_observed_contract_and_unchanged_get():
    request = build_request("qv7m2xp4t")
    assert request.method == "POST" and request.url == SIGNUP_URL
    assert request.body == (
        b'{"username":"qv7m2xp4t","context":"Signup","birthday":"2000-01-01T00:00:00.000Z"}'
    )
    assert dict(request.headers) == {"Content-Type": "application/json"}
    assert parse_qs(urlsplit(protocol.request_url("fert")).query) == {
        "Username": ["fert"],
        "Birthday": ["2000-01-01T00:00:00Z"],
        "Context": ["0"],
    }
    assert protocol.BIRTHDAY == "2000-01-01T00:00:00Z" and protocol.CONTEXT == 0


@pytest.mark.parametrize(
    "username,body,expected",
    [
        ("qv7m2xp4t", b'{"code":0,"message":"Username is valid"}', "Available"),
        ("fert", b'{"code":1,"message":"Username is already in use"}', "Taken"),
        ("fixture", b'{"code":12,"message":"Reserved username"}', "Reserved"),
        ("fixture", b'{"code":2,"message":"Moderation rejection"}', "Invalid"),
        ("fixture", b'{"code":3,"message":"Invalid length"}', "Invalid"),
        ("fixture", b'{"code":99,"message":"Unsupported"}', "Unknown"),
        ("fixture", b'{"code":0,"message":"Username is already in use"}', "Unknown"),
        ("fixture", b'{"code":1,"message":"Username is valid"}', "Unknown"),
        ("fixture", b'{"code":0,"message":"string"}', "Unknown"),
        ("fixture", b'{"code":0,"code":1,"message":"Rejected"}', "Unknown"),
        ("fixture", b'{"code":0,"message":"Token Validation Failed"}', "Unknown"),
        ("fixture", b'{"code":0,"message":"CSRF validation failed"}', "Unknown"),
        ("fixture", b'{"errors":[{"code":0,"message":"Token Validation Failed"}]}', "Unknown"),
        ("fixture", b'{"code":true,"message":"Username is valid"}', "Unknown"),
        ("fixture", b"{", "Unknown"),
        ("fixture", b"\xff", "Unknown"),
    ],
)
def test_preliminary_evidence_uses_post_metadata_and_preserves_raw_body(username, body, expected):
    response = protocol.Response(status=200, body=body, headers={"content-type": "application/json"})
    result = result_for_signup(username, response)
    assert result["classification"] == expected
    assert result["preliminary"] and result["independently_verified_classification"] == "Unknown"
    assert result["experimental"] and result["request_method"] == "POST"
    assert result["request_url"] == SIGNUP_URL and result["parameters"]["context"] == "Signup"
    assert "Context" not in result["parameters"]
    assert base64.b64decode(result["raw_base64"]) == body
    assert result["raw_sha256"] == hashlib.sha256(body).hexdigest()
    request_body = base64.b64decode(result["request_body_base64"])
    assert result["request_body_sha256"] == hashlib.sha256(request_body).hexdigest()
    assert json.loads(request_body) == result["parameters"]
    if "CSRF" in body.decode(errors="replace") or b'"message":"Token Validation Failed"' in body:
        assert result["classification"] == "Unknown"


def test_post_over_verified_local_tls_has_exact_headers_and_no_credentials():
    with local_server() as (server, context):
        transport = session(server, context)
        try:
            result = result_for_signup("qv7m2xp4t", transport.fetch("qv7m2xp4t"))
            assert result["classification"] == "Available"
            assert len(server.posts) == 1
            method, target, headers, body = server.posts[0]
            assert method == "POST" and target == "/v1/usernames/validate?urlLocale=ar_001"
            assert body == build_request("qv7m2xp4t").body
            assert headers["Content-Type"] == "application/json"
            assert headers["Accept"] == "application/json" and headers["Accept-Encoding"] == "identity"
            assert headers["Cache-Control"] == "no-cache"
            assert not {h.casefold() for h in headers} & {"cookie", "authorization", "x-csrf-token"}
        finally:
            transport.close()


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 429, 500])
def test_restrictions_redirects_and_csrf_never_retry_or_capture_tokens(status):
    with local_server() as (server, context):
        server.status = status
        server.extra_headers = {
            "Location": "https://example.invalid/",
            "x-csrf-token": "opaque-test-token",
            "Set-Cookie": "session=opaque-test-cookie",
            "Retry-After": "60",
        }
        transport = session(server, context)
        result = transport.fetch("fixture")
        evidence = result_for_signup("fixture", result)
        assert result.status == status and evidence["classification"] == "Unknown"
        assert result.body and len(server.posts) == 1 and transport.connection is None
        assert not retryable_response(result)
        assert "opaque-test" not in json.dumps(evidence)


@pytest.mark.parametrize("kind", ["challenge", "retry_after", "truncated", "too_large", "timeout", "drop"])
def test_bounded_post_failure_evidence_without_replay(kind):
    with local_server() as (server, context):
        if kind == "challenge":
            server.extra_headers = {"rblx-challenge-id": "opaque-test-challenge"}
        elif kind == "retry_after":
            server.extra_headers = {"Retry-After": "60"}
        elif kind == "truncated":
            server.truncate = 10
        elif kind == "too_large":
            server.body = b"a" * (protocol.MAX_BODY + 10)
        elif kind == "timeout":
            server.body_delay = 0.15
        else:
            server.drop_before_headers = True
        transport = session(server, context, timeout=0.05 if kind == "timeout" else 1)
        response = transport.fetch("fixture")
        evidence = result_for_signup("fixture", response)
        assert evidence["classification"] == "Unknown" and len(server.posts) == 1
        assert not retryable_response(response) and transport.connection is None
        assert len(response.body) <= protocol.MAX_BODY + 1
        assert "opaque-test-challenge" not in json.dumps(evidence)
        if kind == "truncated":
            assert response.error == "incomplete_body" and response.body
        if kind == "timeout":
            assert response.error == "timeout"
        if kind == "drop":
            assert response.error == "network_error"


@pytest.mark.parametrize("phase", ["connect", "request", "response"])
def test_post_does_not_offer_automatic_retries_even_before_dispatch(phase):
    attempts = []

    class Connection:
        sock = None

        def connect(self):
            if phase == "connect":
                raise TimeoutError()
            self.sock = object()

        def request(self, *args, **kwargs):
            attempts.append((args, kwargs))
            if phase == "request":
                raise ConnectionResetError()

        def getresponse(self):
            raise http.client.RemoteDisconnected()

        def close(self):
            self.sock = None

    transport = ExperimentalSignupTransport(connection_factory=Connection)
    result = transport.fetch("fixture")
    assert len(attempts) == (0 if phase == "connect" else 1)
    assert result_for_signup("fixture", result)["classification"] == "Unknown"
    assert not retryable_response(result) and transport.connection is None


def test_experimental_path_requires_explicit_factory_and_verified_tls():
    with pytest.raises(TypeError):
        ExperimentalSignupTransport()
    with pytest.raises(TypeError):
        ExperimentalSignupTransport(connection_factory=None)
    with pytest.raises(ValueError, match="Verified TLS"):
        ExperimentalSignupTransport(connection_factory=lambda: None, context=ssl._create_unverified_context())


@pytest.mark.parametrize("kind", ["provider", "plain_http"])
def test_real_connections_cannot_target_provider_or_plain_http(kind):
    factory = (
        (lambda: http.client.HTTPSConnection("auth.roblox.com"))
        if kind == "provider"
        else (lambda: http.client.HTTPConnection("localhost"))
    )
    transport = ExperimentalSignupTransport(connection_factory=factory)
    with pytest.raises(ValueError, match="loopback HTTPS"):
        transport.fetch("fixture")
    transport.close()


def test_get_evidence_and_shared_classifier_are_not_reinterpreted():
    response = protocol.Response(
        status=200,
        body=b'{"code":1,"message":"Username is already in use"}',
        headers={"content-type": "application/json"},
    )
    before = result_for("fert", response, checked_at="fixed")
    assert result_for_signup("fert", response)["classification"] == "Taken"
    assert result_for("fert", response, checked_at="fixed") == before
    assert before["parameters"]["Context"] == 0 and "request_method" not in before
