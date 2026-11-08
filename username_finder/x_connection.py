"""Explicit credential-free X routes and one-shot neutral HTTPS connectivity tests."""

from __future__ import annotations

import http.client
import io
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

from . import x_protocol as protocol

CONNECTIVITY_URL = "https://api.ipify.org?format=json"
DIRECT = {"mode": "direct", "proxy_url": ""}


def configuration(mode="direct", proxy_url=""):
    if mode == "direct":
        return dict(DIRECT)
    if mode != "proxy":
        raise ValueError("اختر اتصالًا مباشرًا أو بروكسي ثابتًا.")
    if not isinstance(proxy_url, str) or not proxy_url or re.search(r"\s|[\\%]", proxy_url):
        raise ValueError("أدخل رابط بروكسي صالحًا دون فراغات: http://HOST:PORT أو https://HOST:PORT.")
    try:
        parsed = urlsplit(proxy_url)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("لا ندعم بيانات دخول البروكسي؛ لا تُدخل اسم مستخدم أو كلمة مرور.")
        if parsed.scheme not in ("http", "https"):
            raise ValueError("ندعم HTTP وHTTPS فقط؛ SOCKS غير مدعوم.")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("رابط البروكسي يحتاج المضيف والمنفذ فقط، دون مسار أو استعلام.")
        host, port = parsed.hostname, parsed.port
        if not host or port is None or not 1 <= port <= 65535:
            raise ValueError("أدخل مضيفًا ومنفذًا بين 1 و65535.")
        if ":" in host:
            host = "[" + str(ipaddress.IPv6Address(host)) + "]"
        else:
            host = host.encode("idna").decode("ascii").lower()
            if re.fullmatch(r"[0-9.]+", host) and "." in host:
                ipaddress.IPv4Address(host)
            labels = host.rstrip(".").split(".")
            if len(host) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
            ):
                raise ValueError("اسم مضيف البروكسي غير صالح.")
        return {"mode": "proxy", "proxy_url": f"{parsed.scheme}://{host}:{port}"}
    except (UnicodeError, ValueError) as error:
        # Never echo a malformed URL, which could contain credentials.
        if str(error).startswith(("لا ", "ندعم", "رابط", "أدخل", "اسم")):
            raise ValueError(str(error)) from None
        raise ValueError("رابط البروكسي أو المضيف أو المنفذ غير صالح.") from None


def validate(value=None):
    if value is None:
        return dict(DIRECT)
    if not isinstance(value, dict) or set(value) != {"mode", "proxy_url"}:
        raise ValueError("إعداد الاتصال غير صالح؛ راجعه قبل التشغيل.")
    return configuration(value["mode"], value["proxy_url"])


def label(value):
    route = validate(value)
    return "مباشر" if route["mode"] == "direct" else "بروكسي ثابت: " + route["proxy_url"]


class _TLSReader(io.RawIOBase):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        owner.readers += 1

    def readable(self):
        return True

    def readinto(self, buffer):
        data = self.owner.recv(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def close(self):
        if not self.closed:
            self.owner.readers -= 1
            if self.owner.closed and not self.owner.readers:
                self.owner.outer.close()
        super().close()


class _NestedTLS:
    """TLS to the destination carried inside the verified TLS proxy connection."""

    def __init__(self, outer, context, hostname):
        self.outer = outer
        self.incoming, self.outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.inner = context.wrap_bio(self.incoming, self.outgoing, server_hostname=hostname)
        self.readers, self.closed = 0, False
        self._call(self.inner.do_handshake)

    def _flush(self):
        while self.outgoing.pending:
            self.outer.sendall(self.outgoing.read())

    def _call(self, operation, *args):
        while True:
            try:
                result = operation(*args)
                self._flush()
                return result
            except ssl.SSLWantReadError:
                self._flush()
                data = self.outer.recv(65536)
                if data:
                    self.incoming.write(data)
                else:
                    self.incoming.write_eof()
            except ssl.SSLWantWriteError:
                self._flush()

    def sendall(self, data):
        remaining = memoryview(data)
        while remaining:
            written = self._call(self.inner.write, remaining)
            remaining = remaining[written:]

    def recv(self, count):
        return self._call(self.inner.read, count)

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError("Only binary HTTP response reading is supported")
        return io.BufferedReader(_TLSReader(self))

    def close(self):
        self.closed = True
        if not self.readers:
            self.outer.close()


class _ProxyConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, *, secure_proxy, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.secure_proxy = secure_proxy

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
            if self.secure_proxy:
                self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
            self._tunnel()
            if self.secure_proxy:
                self.sock = _NestedTLS(self.sock, self._context, self._tunnel_host)
            else:
                self.sock = self._context.wrap_socket(self.sock, server_hostname=self._tunnel_host)
        except BaseException:
            self.close()
            raise


def request(url, timeout, route):
    """Exactly one HTTPS request; explicit route ignores environment bypass/proxy lists."""
    route = validate(route)
    target = urlsplit(url)
    if target.scheme != "https" or not target.hostname or target.username or target.password:
        raise ValueError("Only credential-free HTTPS targets are supported")
    if route["mode"] == "proxy":
        proxy = urlsplit(route["proxy_url"])
        connection = _ProxyConnection(
            proxy.hostname, proxy.port, secure_proxy=proxy.scheme == "https", timeout=timeout
        )
        connection.set_tunnel(target.hostname, target.port or 443)
    else:
        connection = http.client.HTTPSConnection(
            target.hostname, target.port or 443, timeout=timeout, context=ssl.create_default_context()
        )
    received_response = False
    try:
        path = (target.path or "/") + ("?" + target.query if target.query else "")
        connection.request(
            "GET", path, headers={"Accept": "application/json", "User-Agent": "XUsernameResearch/0.1"}
        )
        with connection.getresponse() as response:
            received_response = True
            headers = {
                key.lower(): value for key, value in response.getheaders() if key.lower() in protocol.HEADERS
            }
            if any(
                re.search(r"challenge|captcha|cf-mitigated", key, re.I) for key, _ in response.getheaders()
            ):
                headers["challenge-indicator"] = "present"
            body = response.read(protocol.MAX_BODY + 1)
            return protocol.Response(
                response.status, body[: protocol.MAX_BODY], headers, complete=len(body) <= protocol.MAX_BODY
            )
    except (OSError, http.client.HTTPException) as error:
        from .overnight import transport_failure

        response = protocol.Response(error=type(error).__name__, complete=False)
        return response if received_response else transport_failure(response, error)
    finally:
        connection.close()


def test_connection(route, *, compare=False, requester=None, monotonic=time.monotonic, cancel=None):
    route = validate(route)
    requester = requester or request
    started = monotonic()
    result = {"success": False, "used_proxy": route["mode"] == "proxy", "exit_changed": None}
    try:
        if cancel and cancel.is_set():
            result["cancelled"] = True
            return result
        response = requester(CONNECTIVITY_URL, 10.0, route)
        result["latency_seconds"] = monotonic() - started
        if response.error or response.status != 200 or not response.complete:
            result["error"] = response.error or f"HTTP {response.status} / incomplete response"
            return result
        payload = json.loads(response.body)
        address = ipaddress.ip_address(payload["ip"])
        result["success"] = True
        if cancel and cancel.is_set():
            result["cancelled"] = True
        elif compare and result["used_proxy"]:
            try:
                direct = requester(CONNECTIVITY_URL, 10.0, dict(DIRECT))
                if direct.status != 200 or direct.error or not direct.complete:
                    raise ValueError("Comparison failed")
                result["exit_changed"] = address != ipaddress.ip_address(json.loads(direct.body)["ip"])
            except (OSError, ValueError, KeyError, TypeError):
                result["comparison_error"] = "direct_comparison_failed"
    except (ValueError, KeyError, TypeError, OSError):
        result["error"] = "invalid_or_failed_connectivity_response"
    finally:
        result.setdefault("latency_seconds", monotonic() - started)
    return result
