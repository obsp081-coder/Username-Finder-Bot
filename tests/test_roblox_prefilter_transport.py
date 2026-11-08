import json
import socket
import ssl
from email.message import Message

import pytest

from username_finder import roblox_prefilter_transport as transport
from username_finder.roblox_bulk_protocol import BULK_URL, response_stop


@pytest.mark.parametrize("failure", [None, "timeout", "reuse_failure", "challenge", "framing"])
def test_owned_connection_reused_sequentially_and_never_retried(monkeypatch, failure):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("No live"))
    instances = []

    class Stream:
        status = 200

        def __init__(self):
            self.sent = False
            self.headers = Message()
            self.headers.add_header("Content-Type", "application/json")
            self.headers.add_header("Content-Length", "11")
            self.headers.add_header("Set-Cookie", "synthetic-not-saved")
            if failure == "challenge":
                self.headers.add_header("rblx-challenge-id", "not-saved")
            if failure == "framing":
                self.headers.add_header("Transfer-Encoding", "chunked")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read1(self, size):
            if failure == "timeout":
                raise TimeoutError
            value = b"" if self.sent else b'{"data":[]}'
            self.sent = True
            return value

    class Connection:
        def __init__(self, host, *, timeout, context):
            assert host == "users.roblox.com" and timeout == 10
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            self.sock = None
            self.connects = 0
            self.requests = 0
            self.closed = False
            instances.append(self)

        def connect(self):
            self.sock = self
            self.connects += 1

        def settimeout(self, t):
            assert 0 < t <= 10

        def request(self, method, path, *, body, headers):
            self.requests += 1
            if failure == "reuse_failure" and self.requests == 2:
                raise OSError("closed peer")
            assert method == "POST" and path == "/v1/usernames/users"
            assert json.loads(body) == {"usernames": ["UserName"], "excludeBannedUsers": False}
            assert "Authorization" not in headers and "Cookie" not in headers

        def getresponse(self):
            return Stream()

        def close(self):
            self.closed = True
            self.sock = None

    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    session = transport.PrefilterTransport()
    request = {"kind": "bulk", "method": "POST", "url": BULK_URL, "usernames": ["UserName"]}
    try:
        first = session(request)
        if failure in (None, "reuse_failure"):
            assert not response_stop(first)
            second = session(request)
            assert bool(second.error) == (failure == "reuse_failure")
            assert len(instances) == 1 and instances[0].connects == 1 and instances[0].requests == 2
        else:
            assert response_stop(first)
        assert "set-cookie" not in first.headers
    finally:
        session.close()
    assert all(c.closed for c in instances)
