"""Owned sequential bulk HTTPS connection, adapted from the reviewed transport.

No automatic replay when a reused connection fails. Caller always closes the session.
"""

import http.client
import ssl
import time
from urllib.parse import urlsplit

from . import roblox_protocol as protocol
from .roblox_bulk_protocol import SAFE_HEADERS, TIMEOUT_SECONDS, encoded

MAX_BATCH_SIZE = 1000


class PrefilterTransport:
    """One direct HTTPS request, no redirects/proxies/cookies/auth/replay."""

    def __init__(self):
        self.connection = None
        self.context = ssl.create_default_context()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

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
            names = request["usernames"]
            if (
                type(names) is not list
                or not 1 <= len(names) <= MAX_BATCH_SIZE
                or any(type(name) is not str or not 3 <= len(name) <= 20 for name in names)
            ):
                raise ValueError("Bulk payload must contain 1-1000 bounded usernames")
            headers["Content-Type"] = "application/json"
            body = encoded({"usernames": names, "excludeBannedUsers": False})
        response = protocol.Response()
        started = time.monotonic()
        if request["kind"] != "bulk" or target.query:
            raise ValueError("Only the fixed bulk endpoint is allowed")
        if self.connection is None:
            self.connection = http.client.HTTPSConnection(
                target.hostname, timeout=TIMEOUT_SECONDS, context=self.context
            )
        connection = self.connection

        def remaining_timeout():
            remaining = TIMEOUT_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            return remaining

        try:
            if connection.sock is None:
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
                if response.error:
                    self.close()
            except OSError:
                response.error = response.error or "cleanup_error"
            response.complete = not bool(response.error)
            response.elapsed = time.monotonic() - started
        return response
