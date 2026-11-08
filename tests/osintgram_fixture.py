"""Synthetic local HTTP fixture. Never imports Osintgram or accesses Instagram."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def param(name, kind="integer", default=200, **extra):
    return {"name": name, "type": kind, "default": default, "description": "Fixture parameter", **extra}


TOOLS = [
    {"name": "get_user_info", "needs_target": True, "params": []},
    {"name": "get_followers", "needs_target": True, "params": [param("limit")]},
    {
        "name": "search_hashtag",
        "needs_target": False,
        "params": [
            param("hashtag", "string", ""),
            param("sort", "string", "top", choices=["top", "recent"]),
            param("limit"),
        ],
    },
    {
        "name": "search_location",
        "needs_target": False,
        "params": [param("place", "string", ""), param("limit")],
    },
    {
        "name": "get_post_comments",
        "needs_target": False,
        "params": [param("post", "string", ""), param("limit")],
    },
    {
        "name": "compare_with",
        "needs_target": True,
        "params": [
            param("other_username", "string", ""),
            param("scope", "string", "both", choices=["both", "followers", "followings"]),
            param("limit"),
        ],
    },
    {
        "name": "get_followers_email",
        "needs_target": True,
        "params": [param("match_limit", default=20), param("max_checks", default=1000)],
    },
    {"name": "future_complex_tool", "needs_target": False, "params": []},
]

PROFILE = {
    "id": 9876543210123456789,
    "username": "fixture_user",
    "full_name": "حساب اختباري",
    "biography": "<img src='https://example.invalid/never-load'>",
    "follower_count": 42,
    "following_count": 0,
    "media_count": None,
    "is_verified": False,
    "is_private": True,
    "external_url": "https://example.invalid/profile",
    "bio_links": [{"title": "", "url": "https://example.invalid/link"}],
}


class MemorySettings:
    def __init__(self, **values):
        self.values = values

    def value(self, name, default=None):
        return self.values.get(name, default)

    def setValue(self, name, value):
        self.values[name] = value

    def remove(self, name):
        self.values.pop(name, None)

    def sync(self):
        pass


class MemoryCredentials:
    def __init__(self, account=None):
        self.account = account

    def load(self):
        return self.account

    def save(self, username, password):
        self.account = (username, password)

    def delete(self):
        self.account = None


class FakeBackend:
    def __init__(self):
        self.tools = TOOLS
        self.tools_raw = None
        self.tools_status = 200
        self.redirect = None
        self.about = {"repository": "https://github.com/Datalux/Osintgram", "version": "fixture"}
        self.tools_delay = 0
        self.run_status = 200
        self.cancel_status = 200
        self.cancel_raw = None
        self.detail = "Fixture error"
        self.mode = "normal"
        self.identity_backend = "fixture"
        self.delay = 0
        self.build_delay = 0
        self.calls = []
        self.cancelled = threading.Event()
        self.active = None
        self.results = {
            "get_user_info": PROFILE,
            "get_followers": [{"id": 1234567890123456789, "username": "fixture_follower"}],
            "future_complex_tool": {
                "summary": {"count": 2},
                "nested": {"rows": [{"x": 1}, {"y": None}]},
                "empty": [],
            },
        }
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send_json(self, status, value, raw=None):
                body = json.dumps(value, ensure_ascii=False).encode() if raw is None else raw
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if backend.redirect:
                    self.send_header("Location", backend.redirect)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def do_GET(self):
                backend.calls.append(("GET", self.path, None))
                backend.cancelled.wait(backend.tools_delay)
                self.send_json(
                    backend.tools_status,
                    backend.about if self.path == "/api/about" else backend.tools,
                    backend.tools_raw,
                )

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                backend.calls.append(("POST", self.path, payload))
                if self.path == "/api/cancel":
                    if backend.cancel_status != 200 or backend.cancel_raw is not None:
                        self.send_json(backend.cancel_status, {}, backend.cancel_raw)
                        return
                    active = backend.active == payload.get("run_id")
                    if active:
                        backend.cancelled.set()
                    self.send_json(200, {"cancelled": active})
                    return
                if self.path != "/api/run":
                    self.send_json(404, {})
                    return
                backend.cancelled.wait(backend.build_delay)
                if backend.run_status != 200:
                    self.send_json(backend.run_status, {"detail": backend.detail})
                    return
                backend.active = payload["run_id"]
                backend.cancelled.clear()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def emit(event):
                    self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())
                    self.wfile.flush()

                try:
                    if backend.mode == "empty":
                        return
                    if backend.mode == "malformed":
                        self.wfile.write(b"data: {broken}\n\n")
                        return
                    emit(
                        {
                            "type": "start",
                            "backend": backend.identity_backend,
                            "target": payload.get("target"),
                            "target_id": PROFILE["id"],
                            "api_calls_total": 1,
                        }
                    )
                    for call in payload["calls"]:
                        if backend.cancelled.is_set():
                            break
                        emit({"type": "tool_call", "name": call["name"], "input": call["args"]})
                        if backend.mode == "incomplete":
                            break
                        if backend.cancelled.wait(backend.delay):
                            break
                        emit({"type": "api_call", "method": "fixture", "total": 2, "status": "ok"})
                        if backend.mode == "stream_error":
                            emit({"type": "error", "detail": "login required; session_secret=DO_NOT_DISPLAY"})
                            break
                        result = backend.results.get(call["name"], [])
                        emit(
                            {
                                "type": "tool_result",
                                "name": "wrong_name" if backend.mode == "mismatch" else call["name"],
                                "result": result,
                                "is_error": isinstance(result, dict) and "error" in result,
                                "elapsed_ms": 12,
                                "api_calls_delta": 1,
                                "api_calls_total": 2,
                            }
                        )
                    if backend.mode == "trailing":
                        self.wfile.write(b"data: {")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    backend.active = None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.cancelled.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
