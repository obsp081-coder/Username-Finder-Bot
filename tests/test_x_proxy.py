"""Mocked routes and loopback TLS proxies; PEM fixtures are public test-only keys."""

import json
import select
import socket
import socketserver
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_x import Clock, response

from username_finder import x
from username_finder import x_connection as connection
from username_finder import x_protocol as protocol
from username_finder.storage import Store
from username_finder.x_runner import XRunner
from username_finder.x_storage import XStore

CERT = Path(__file__).parent / "fixtures/x_proxy_test_cert.pem"
KEY = CERT.with_name("x_proxy_test_key.pem")


@pytest.mark.parametrize("url", ["http://localhost:8080", "https://proxy.example:443", "http://[::1]:3128"])
def test_supported_proxy_urls(url):
    assert connection.configuration("proxy", url)["proxy_url"] == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "host:123",
        "socks5://host:1080",
        "http://host",
        "http://:80",
        "http://host:0",
        "http://host:65536",
        "http://host:abc",
        "http://host:80/path",
        "http://host:80?token=secret",
        "http://host:80#secret",
        "http://bad host:80",
        "http://-bad:80",
        "http://user:password@host:80",
        "http://user@host:80",
        "http://host%2ecom:80",
        "http://host\\name:80",
    ],
)
def test_bad_proxy_urls_never_store_or_echo_credentials(url):
    with pytest.raises(ValueError) as error:
        connection.configuration("proxy", url)
    assert "password" not in str(error.value) and "secret" not in str(error.value)


def test_neutral_connectivity_reports_route_latency_without_exposing_ips_or_changing_settings():
    calls = []

    def request(url, timeout, route):
        assert url == connection.CONNECTIVITY_URL and "x.com" not in url
        calls.append(route)
        return protocol.Response(
            200, json.dumps({"ip": "203.0.113.10" if route["mode"] == "proxy" else "203.0.113.11"}).encode()
        )

    ticks = iter((1.0, 1.25, 1.5))
    proxy = connection.configuration("proxy", "http://localhost:8080")
    result = connection.test_connection(proxy, compare=True, requester=request, monotonic=lambda: next(ticks))
    assert result["success"] and result["used_proxy"] and result["exit_changed"]
    assert result["latency_seconds"] == 0.25
    assert calls == [proxy, connection.DIRECT]
    assert "203.0.113" not in json.dumps(result)


@pytest.mark.parametrize(
    "sample",
    [
        protocol.Response(error="TimeoutError", complete=False),
        protocol.Response(403),
        protocol.Response(429),
        protocol.Response(200, b"bad JSON"),
        protocol.Response(200, b'{"ip":"invalid"}'),
    ],
)
def test_connectivity_failure_has_no_fallback_or_x_requests(sample):
    calls = []

    def request(url, timeout, route):
        calls.append(route)
        return sample

    result = connection.test_connection(
        connection.configuration("proxy", "https://localhost:8080"), compare=True, requester=request
    )
    assert not result["success"] and len(calls) == 1
    assert result["used_proxy"] and result["latency_seconds"] >= 0


def test_cancel_during_connectivity_test_suppresses_optional_direct_comparison():
    cancelled = threading.Event()
    calls = []

    def request(*args):
        calls.append(args)
        cancelled.set()
        return protocol.Response(200, b'{"ip":"203.0.113.10"}')

    result = connection.test_connection(
        connection.configuration("proxy", "http://localhost:8080"),
        compare=True,
        requester=request,
        cancel=cancelled,
    )
    assert result["cancelled"] and len(calls) == 1


@contextmanager
def loopback_servers(secure_proxy):
    origin_hits, tunnel_hits = [], []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT, KEY)

    class Origin(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            origin_hits.append(self.path)
            body = b'{"ip":"203.0.113.10"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    origin.socket = context.wrap_socket(origin.socket, server_side=True)
    stop = threading.Event()

    class Tunnel(socketserver.BaseRequestHandler):
        def handle(self):
            upstream = None
            try:
                data = b""
                while not data.endswith(b"\r\n\r\n") and len(data) < 8192:
                    part = self.request.recv(1)
                    if not part:
                        return
                    data += part
                target = f"localhost:{origin.server_port}"
                allowed = (target, f"mismatch.invalid:{origin.server_port}")
                assert any(data.startswith(f"CONNECT {name} HTTP/1.1".encode()) for name in allowed)
                assert b"Proxy-Authorization" not in data
                tunnel_hits.append(target)
                upstream = socket.create_connection(("127.0.0.1", origin.server_port), timeout=2)
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                while not stop.is_set():
                    ready, _, _ = select.select([self.request, upstream], [], [], 0.05)
                    for source in ready:
                        chunk = source.recv(65536)
                        if not chunk:
                            return
                        (upstream if source is self.request else self.request).sendall(chunk)
            except OSError:
                pass
            finally:
                if upstream:
                    upstream.close()

    class Proxy(socketserver.ThreadingTCPServer):
        daemon_threads = True

    proxy = Proxy(("127.0.0.1", 0), Tunnel)
    if secure_proxy:
        proxy.socket = context.wrap_socket(proxy.socket, server_side=True)
    threads = [
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        for s in (origin, proxy)
    ]
    for thread in threads:
        thread.start()
    try:
        yield (
            f"https://localhost:{origin.server_port}/neutral",
            proxy.server_address[1],
            origin_hits,
            tunnel_hits,
        )
    finally:
        stop.set()
        for server in (origin, proxy):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(2)


@pytest.mark.parametrize("scheme", ["direct", "http", "https"])
def test_real_loopback_https_direct_and_both_proxy_schemes_with_certificate_verification(monkeypatch, scheme):
    original_context = ssl.create_default_context

    def trusted_context(*args, **kwargs):
        context = original_context(*args, **kwargs)
        context.load_verify_locations(CERT)
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        return context

    monkeypatch.setattr(ssl, "create_default_context", trusted_context)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("HTTPS_PROXY", "http://unreachable.invalid:1")
    with loopback_servers(scheme == "https") as (url, port, origins, tunnels):
        route = (
            connection.DIRECT
            if scheme == "direct"
            else connection.configuration("proxy", f"{scheme}://localhost:{port}")
        )
        result = connection.request(url, 2, route)
        assert result.status == 200, result.error
        assert json.loads(result.body)["ip"] == "203.0.113.10"
        assert origins == ["/neutral"]
        assert len(tunnels) == (0 if scheme == "direct" else 1)


def test_untrusted_proxy_certificate_is_rejected_not_retried_directly():
    with loopback_servers(True) as (url, port, origins, tunnels):
        result = connection.request(url, 2, connection.configuration("proxy", f"https://localhost:{port}"))
        assert result.status is None and result.error == "SSLCertVerificationError"
        assert not origins and not tunnels


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_destination_hostname_is_verified_inside_both_proxy_tunnels(monkeypatch, scheme):
    original_context = ssl.create_default_context

    def trusted_context(*args, **kwargs):
        context = original_context(*args, **kwargs)
        context.load_verify_locations(CERT)
        return context

    monkeypatch.setattr(ssl, "create_default_context", trusted_context)
    with loopback_servers(scheme == "https") as (url, port, origins, tunnels):
        result = connection.request(
            url.replace("localhost", "mismatch.invalid"),
            2,
            connection.configuration("proxy", f"{scheme}://localhost:{port}"),
        )
        assert result.status is None and result.error == "SSLCertVerificationError"
        assert not origins and len(tunnels) == 1


def test_optional_comparison_failure_does_not_rewrite_successful_primary_test():
    calls = []

    def request(url, timeout, route):
        calls.append(route)
        return protocol.Response(200, b'{"ip":"203.0.113.10"}' if route["mode"] == "proxy" else b"bad JSON")

    result = connection.test_connection(
        connection.configuration("proxy", "http://localhost:3128"), requester=request, compare=True
    )
    assert result["success"] and result["comparison_error"]
    assert len(calls) == 2 and result["exit_changed"] is None


@pytest.mark.parametrize("route", [None, connection.DIRECT])
def test_direct_x_transport_still_disables_environment_proxies(monkeypatch, route):
    calls = []

    class Stream:
        status = 200
        headers = {"content-type": "application/json"}

        def read(self, amount):
            return response().body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request.full_url, request.get_method(), timeout))
            return Stream()

    def opener(*handlers):
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], protocol.NoRedirect)
        return Opener()

    monkeypatch.setattr(protocol.urllib.request, "build_opener", opener)
    result = protocol.fetch("fresh_name", 15, connection=route)
    assert protocol.parse(result, "fresh_name")[0] == "Taken"
    assert calls == [(protocol.ENDPOINT + "?username=fresh_name", "GET", 15)]


def test_explicit_proxy_routes_x_fetch_without_touching_direct_transport(monkeypatch):
    calls = []
    proxy = connection.configuration("proxy", "http://localhost:3128")

    def request(url, timeout, route):
        calls.append((url, timeout, route))
        return response()

    monkeypatch.setattr(connection, "request", request)
    monkeypatch.setattr(protocol.urllib.request, "build_opener", lambda *a: pytest.fail("No direct fallback"))
    assert protocol.fetch("fresh_name", 15, connection=proxy).status == 200
    assert calls == [(protocol.ENDPOINT + "?username=fresh_name", 15, proxy)]


def test_proxy_failure_returns_unknown_without_attempting_any_other_route(monkeypatch):
    calls = []

    class FailedConnection:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def set_tunnel(self, *args):
            pass

        def request(self, *args, **kwargs):
            raise TimeoutError("Do not leak a URL or password")

        def close(self):
            pass

    monkeypatch.setattr(connection, "_ProxyConnection", FailedConnection)
    result = connection.request(
        protocol.ENDPOINT, 1, connection.configuration("proxy", "http://localhost:3128")
    )
    assert len(calls) == 1 and result.error == "TimeoutError"
    assert protocol.parse(result, "fresh_name")[0] == "Unknown"


def test_preferences_freeze_per_job_reopen_and_legacy_direct_records(tmp_path, monkeypatch):
    store = Store(tmp_path / "routes.db")
    try:
        repo = XStore(store)
        assert repo.preferred_connection() == connection.DIRECT
        old_job = repo.prepare(x.input_spec("old_name"))
        # Simulate a pre-feature job: no connection field, with unchanged historical state.
        with store.transaction() as db:
            db.execute("UPDATE x_runs SET spec=? WHERE id=?", (json.dumps(x.input_spec("old_name")), old_job))
        original = tuple(store.db.execute("SELECT * FROM x_runs WHERE id=?", (old_job,)).fetchone())
        proxy = connection.configuration("proxy", "https://localhost:3128")
        repo.set_connection(proxy)
        identity = repo.prepare(x.input_spec("first_name\nsecond_name"))
        clock = Clock()
        calls = []

        def fetch(name, timeout, *, connection):
            calls.append((name, connection))
            result = clock.fetch(name, timeout)
            worker.cancel.set()
            return result

        monkeypatch.setattr(protocol, "fetch", fetch)
        worker = XRunner(repo, clock=clock.now, monotonic=lambda: clock.seconds, wait=clock.wait)
        worker.run(identity)
        repo.set_connection(connection.DIRECT)
        store.close()
        store = Store(tmp_path / "routes.db")
        repo = XStore(store)
        assert tuple(store.db.execute("SELECT * FROM x_runs WHERE id=?", (old_job,)).fetchone()) == original
        assert repo.connection_for_run(old_job) == connection.DIRECT
        assert repo.connection_for_run(identity) == proxy
        assert repo.preferred_connection() == connection.DIRECT
        worker = XRunner(repo, clock=clock.now, monotonic=lambda: clock.seconds, wait=clock.wait)
        worker.run(identity)
        assert calls == [("first_name", proxy), ("second_name", proxy)]
        assert clock.waits == [5]
        assert len(repo.history("first_name")) == 1
    finally:
        store.close()


@pytest.mark.parametrize("proxy_url", [None, "http://localhost:3128", "https://localhost:3128"])
def test_changing_connection_never_clears_429_block_or_reissues_request(tmp_path, monkeypatch, proxy_url):
    store = Store(tmp_path / "blocked.db")
    try:
        repo = XStore(store)
        if proxy_url:
            repo.set_connection(connection.configuration("proxy", proxy_url))
        original_route = repo.preferred_connection()
        identity = repo.prepare(x.input_spec("first_name\nsecond_name"))
        monkeypatch.setattr(
            protocol, "fetch", lambda *a, **kw: protocol.Response(429, b"{}", {"retry-after": "120"})
        )
        XRunner(repo).run(identity)
        original = [tuple(r) for r in store.db.execute("SELECT * FROM x_checks")]
        blocked = repo.control()["blocked"]
        deadline = repo.control()["not_before"]
        repo.set_connection(connection.configuration("proxy", "http://localhost:3128"))
        assert repo.control()["blocked"] == blocked and repo.control()["not_before"] == deadline
        assert repo.connection_for_run(identity) == original_route
        with pytest.raises(ValueError):
            XRunner(repo, fetcher=lambda *a: pytest.fail("No bypass")).run(identity)
        assert [tuple(r) for r in store.db.execute("SELECT * FROM x_checks")] == original
    finally:
        store.close()


def test_cancellation_during_failed_proxy_request_records_uncertainty_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "cancelled.db")
    try:
        repo = XStore(store)
        proxy = connection.configuration("proxy", "http://localhost:3128")
        repo.set_connection(proxy)
        identity = repo.prepare(x.input_spec("first_name\nsecond_name"))
        calls = []

        def failed_request(name, timeout, *, connection):
            calls.append(connection)
            worker.cancel.set()
            return protocol.Response(error="TimeoutError", complete=False)

        monkeypatch.setattr(protocol, "fetch", failed_request)
        worker = XRunner(repo)
        worker.run(identity)
        assert calls == [proxy] and repo.control()["blocked"]
        evidence = json.loads(repo.history("first_name")[0]["evidence"])
        assert evidence["delivery_uncertain"] and evidence["connection"] == proxy
        assert evidence["classification"] == "Unknown" and repo.progress(identity)["pending"] == 1
    finally:
        store.close()


def test_proxy_crash_recovery_retains_frozen_route_even_after_preference_change(tmp_path):
    store = Store(tmp_path / "recovery.db")
    try:
        repo = XStore(store)
        proxy = connection.configuration("proxy", "https://localhost:3128")
        repo.set_connection(proxy)
        identity = repo.prepare(x.input_spec("first_name\nsecond_name"))
        repo.start(identity)
        repo.claim(identity)
        repo.set_connection(connection.DIRECT)
        repo.recover()
        evidence = json.loads(repo.history("first_name")[0]["evidence"])
        assert evidence["connection"] == proxy and evidence["delivery_uncertain"]
        assert repo.control()["blocked"] == "interrupted_delivery_uncertain"
        assert repo.connection_for_run(identity) == proxy
        repo.recover()
        assert len(repo.history("first_name")) == 1
    finally:
        store.close()
