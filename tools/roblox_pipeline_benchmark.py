"""Offline-only paired pipeline benchmark. All sockets are loopback; no provider requests.

Run with the same arguments before/after changes, using different --label values.
Generated TLS keys/databases are temporary and are never part of the report.
"""

from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
bootstrap = argparse.ArgumentParser(add_help=False)
bootstrap.add_argument("--source", type=Path, default=ROOT)
SOURCE = bootstrap.parse_known_args()[0].source.resolve()
if not (SOURCE / "username_finder" / "roblox_protocol.py").is_file():
    bootstrap.error("Source must be an existing application checkout")
sys.path.insert(0, str(SOURCE))

from username_finder import roblox_protocol as protocol  # noqa: E402
from username_finder.overnight import OvernightStore  # noqa: E402
from username_finder.roblox import result_for  # noqa: E402
from username_finder.roblox_runner import RobloxRunner  # noqa: E402
from username_finder.roblox_storage import RobloxStore  # noqa: E402
from username_finder.storage import Store  # noqa: E402


class MeasuredConnection(sqlite3.Connection):
    def commit(self):
        start = time.perf_counter()
        try:
            return super().commit()
        finally:
            if hasattr(self, "commit_times"):
                self.commit_times.append(time.perf_counter() - start)


def distribution(values):
    values = sorted(values)
    return {
        "count": len(values),
        "total_s": sum(values),
        "mean_s": statistics.mean(values) if values else 0,
        "median_s": statistics.median(values) if values else 0,
        "p95_s": values[max(0, math.ceil(len(values) * 0.95) - 1)] if values else 0,
    }


def memory():
    if os.name != "nt":
        return {}

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in ("peak", "working", "pp", "p", "np", "n", "page", "peakpage")
        ]

    value = Counters()
    value.cb = ctypes.sizeof(value)
    handle = ctypes.windll.kernel32.GetCurrentProcess
    handle.restype = ctypes.c_void_p
    query = ctypes.windll.psapi.GetProcessMemoryInfo
    query.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    if not query(handle(), ctypes.byref(value), value.cb):
        raise ctypes.WinError()
    return {"working_bytes": value.working, "process_peak_bytes": value.peak}


@contextmanager
def loopback_network():
    original = socket.create_connection

    def connect(address, *args, **kwargs):
        if address[0] not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Benchmark forbids external network destinations")
        return original(address, *args, **kwargs)

    socket.create_connection = connect
    try:
        yield
    finally:
        socket.create_connection = original


def certificate(folder):
    openssl = shutil.which("openssl") or r"C:\Program Files\Git\usr\bin\openssl.exe"
    if not Path(openssl).is_file():
        raise RuntimeError("OpenSSL is required only for ephemeral localhost test certificates")
    cert, key = Path(folder) / "cert.pem", Path(folder) / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.create_default_context(cafile=str(cert))
    client.set_alpn_protocols(["http/1.1"])
    return server, client


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        sock, address = super().get_request()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        start = time.perf_counter()
        time.sleep(self.connect_delay)
        try:
            sock = self.tls.wrap_socket(sock, server_side=True)
        except BaseException:
            sock.close()
            raise
        self.connect_times.append(time.perf_counter() - start)
        return sock, address

    def handle_error(self, request, address):
        # Tests intentionally close sockets; no request contents are logged.
        self.errors += 1

    def shutdown_request(self, request):
        super().shutdown_request(request)
        self.closed.set()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        index = int(query["Username"][0][-8:])
        self.server.requests.append(index)
        if getattr(self.server, "drop_before_headers", False):
            self.close_connection = True
            return
        time.sleep(self.server.response_delay)
        bodies = (
            b'{"code":0,"message":"Username is valid"}',
            b'{"code":1,"message":"Username is already in use"}',
            b'{"code":12,"message":"Reserved username"}',
            b'{"code":3,"message":"Invalid length"}',
        )
        body = bodies[index % 4] + b" " * (0, 16, 64, 192)[index % 4]
        body = getattr(self.server, "body", body)
        status = self.server.status
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        chunked = getattr(self.server, "chunked", False)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(body) + self.server.truncate))
        for key, value in self.server.extra_headers.items():
            self.send_header(key, value)
        if self.server.close_every and len(self.server.requests) % self.server.close_every == 0:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        time.sleep(getattr(self.server, "body_delay", 0))
        self.wfile.write((f"{len(body):X}\r\n".encode() + body + b"\r\n0\r\n\r\n") if chunked else body)
        self.wfile.flush()
        if self.server.truncate:
            self.close_connection = True
        if getattr(self.server, "silent_close", False):
            self.close_connection = True


@contextmanager
def local_server(*, connect_delay=0, response_delay=0, close_every=0):
    with tempfile.TemporaryDirectory(prefix="ufb-loopback-tls-") as folder:
        tls, client = certificate(folder)
        server = LocalServer(("127.0.0.1", 0), Handler)
        server.tls, server.connect_delay, server.response_delay = tls, connect_delay, response_delay
        server.close_every, server.connect_times, server.requests = close_every, [], []
        server.errors, server.status, server.extra_headers, server.truncate = 0, 200, {}, 0
        server.closed = threading.Event()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, client
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


@contextmanager
def profile(store, repo, runner):
    timings, sql = defaultdict(list), Counter()
    originals = []

    def timed(owner, name, label):
        original = getattr(owner, name)
        originals.append((owner, name, original))

        def call(*args, **kwargs):
            start = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                timings[label].append(time.perf_counter() - start)

        setattr(owner, name, call)

    for owner, names in (
        (repo, ("claim_next", "finish_check", "_insert", "progress", "results", "counts")),
        (runner.night, ("requesting", "success", "get")),
    ):
        for name in names:
            timed(owner, name, ("night." if owner is runner.night else "repo.") + name)
    timed(protocol, "classify", "classification")
    import username_finder.roblox_runner as runner_module

    timed(runner_module, "result_for", "evidence_serialization")
    store.db.set_trace_callback(lambda query: sql.update([query.split()[0].upper()]))
    try:
        yield timings, sql
    finally:
        store.db.set_trace_callback(None)
        for owner, name, original in reversed(originals):
            setattr(owner, name, original)


def pipeline(folder, *, count, queue, history, night, gui, fetcher, existing=0, results_visible=False):
    original_connect = sqlite3.connect
    sqlite3.connect = lambda *a, **kw: original_connect(*a, factory=MeasuredConnection, **kw)
    try:
        store = Store(Path(folder) / "benchmark.db")
    finally:
        sqlite3.connect = original_connect
    try:
        return _pipeline(
            store,
            folder,
            count=count,
            queue=queue,
            history=history,
            night=night,
            gui=gui,
            fetcher=fetcher,
            existing=existing,
            results_visible=results_visible,
        )
    finally:
        store.close()


def _pipeline(store, folder, *, count, queue, history, night, gui, fetcher, existing, results_visible):
    repo = RobloxStore(store)
    repo.set_preferred_interval(0)
    sample = protocol.Response(200, b'{"code":1,"message":"Taken"}', {"content-type": "application/json"})
    # Older observations are deterministic; import/history code is real, not a special fast DB.
    with store.transaction() as db:
        for i in range(existing):
            repo._insert(db, result_for(f"Prior{i:08d}", sample), "synthetic-prior", str(i))
        for i in range(count if history else 0):
            for j in range(history):
                evidence = result_for(f"Bench{i:08d}", sample, checked_at=f"2026-01-01T00:00:{j:02d}+00:00")
                repo._insert(db, evidence, "synthetic", f"{i}:{j}")
    identity = repo.create_run((f"Bench{i:08d}" for i in range(queue)), skip=False)
    # Real file-prepared queues have a checkpoint; match that shape without a private file.
    with store.transaction() as db:
        db.execute("UPDATE roblox_runs SET preparation=? WHERE id=?", ('{"benchmark":true}', identity))
    night_store = OvernightStore(store, "Roblox")
    night_store.enable(identity, night)
    latencies, requests = [], []
    gui_times, gaps = [], []
    panel, app = None, None
    if gui:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from username_finder.gui import MainWindow
        from username_finder.roblox_ui import RobloxPanel

        app = QApplication.instance() or QApplication([])
        panel = RobloxPanel(store, MainWindow.make_table, MainWindow.fill_table, fetcher=fetcher)
        panel.current_run = identity
        if results_visible:
            panel.workspace.setCurrentIndex(1)
        panel.show()
        original = panel.refresh

        def refresh():
            start = time.perf_counter()
            original()
            gui_times.append(time.perf_counter() - start)

        panel.refresh_timer.timeout.disconnect()
        panel.refresh_timer.timeout.connect(refresh)

    def fetch(url):
        start = time.perf_counter()
        response = fetcher(url)
        latencies.append(time.perf_counter() - start)
        requests.append(url)
        return response

    def updated():
        if panel:
            panel.progress_changed.emit()
        if len(requests) >= count:
            runner.cancel.set()

    runner = RobloxRunner(repo, fetcher=fetch, updated=updated)
    if panel:
        panel.busy, panel.runner = True, runner
    store.db.commit_times = []
    before_memory = memory()
    with profile(store, repo, runner) as (timings, sql):
        start = time.perf_counter()
        if panel:
            thread = threading.Thread(target=runner.run, args=(identity,))
            thread.start()
            last = time.perf_counter()
            while thread.is_alive():
                app.processEvents()
                at = time.perf_counter()
                gaps.append(at - last)
                last = at
                time.sleep(0.001)
            thread.join()
            app.processEvents()
        else:
            runner.run(identity)
        elapsed = time.perf_counter() - start
    assert len(requests) == count and len(set(requests)) == count, repo.progress(identity)
    assert repo.progress(identity)["counts"]["Done"] == count
    assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    report = {
        "count": count,
        "existing_history_names": existing,
        "results_visible": results_visible,
        "queue": queue,
        "history_per_name": history,
        "night": night,
        "gui": gui,
        "elapsed_s": elapsed,
        "checks_per_s": count / elapsed,
        "network": distribution(latencies),
        "components": {name: distribution(values) for name, values in timings.items()},
        "sql_statements": dict(sql),
        "gui_refresh": distribution(gui_times),
        "sqlite_commit": distribution(store.db.commit_times),
        "gui_event_gap": distribution(gaps),
        "memory_before": before_memory,
        "memory_after": memory(),
        "database_bytes": sum(p.stat().st_size for p in Path(folder).glob("benchmark.db*")),
        "classifications": repo.counts(),
        "durability": "WAL/FULL",
    }
    if panel:
        panel.busy = False
        panel.shutdown()
        panel.close()
    return report


@contextmanager
def connection_profile():
    connection_times, tcp_times = [], []
    original = http.client.HTTPSConnection.connect

    def measured_connect(connection):
        create = connection._create_connection

        def tcp(*args, **kwargs):
            start = time.perf_counter()
            try:
                return create(*args, **kwargs)
            finally:
                tcp_times.append(time.perf_counter() - start)

        connection._create_connection = tcp
        start = time.perf_counter()
        try:
            return original(connection)
        finally:
            connection._create_connection = create
            connection_times.append(time.perf_counter() - start)

    http.client.HTTPSConnection.connect = measured_connect
    try:
        yield connection_times, tcp_times
    finally:
        http.client.HTTPSConnection.connect = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--queue", type=int, default=100000)
    parser.add_argument("--case", action="append", help="Run only named cases; repeat for multiple cases")
    parser.add_argument("--transport", choices=("legacy", "persistent"), default="legacy")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.label):
        parser.error("Label must contain only letters, digits, underscores or hyphens")
    if not 4 <= args.count <= args.queue or args.queue > 2000000:
        parser.error("Require 4 <= count <= queue <= 2000000")
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / (args.label + ".json")
    if path.exists():
        parser.error("Refusing to overwrite earlier measurements")
    report = {
        "label": args.label,
        "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "local_only": True,
        "transport": args.transport,
        "source": str(SOURCE),
        "contract": dict(
            endpoint=protocol.ENDPOINT,
            method="GET",
            birthday=protocol.BIRTHDAY,
            context=protocol.CONTEXT,
            interval_seconds=0,
            concurrency=1,
        ),
        "cases": {},
    }
    # The 0.45-s scenarios bracket unknown connection/server shares of the observed total.
    cases = (
        ("sqlite_fresh", 0, 0, False, False, 0),
        ("sqlite_history10", 0, 0, False, False, 10),
        ("sqlite_night", 0, 0, True, False, 0),
        ("sqlite_history50k", 0, 0, True, False, 0),
        ("gui_active", 0, 0, True, True, 0),
        ("gui_history50k", 0, 0, True, True, 0),
        ("gui_results_history50k", 0, 0, True, True, 0),
        ("gui_tls_server_bound_450ms", 0, 0.45, True, True, 0),
        ("tls_local", 0, 0, False, False, 0),
        ("tls_transport_only", 0, 0, False, False, 0),
        ("server_bound_450ms", 0, 0.45, True, False, 0),
        ("connection_150_response_300ms", 0.15, 0.30, True, False, 0),
        ("connection_300_response_150ms", 0.30, 0.15, True, False, 0),
    )
    if args.case and set(args.case) - {row[0] for row in cases}:
        parser.error("Unknown benchmark case")
    for name, connect_delay, response_delay, night, gui, history in cases:
        if args.case and name not in args.case:
            continue
        mock_case = name.startswith("sqlite") or (gui and not name.startswith("gui_tls"))
        count = min(args.queue, max(args.count, 400)) if mock_case else args.count
        with tempfile.TemporaryDirectory(prefix="ufb-roblox-profile-") as folder:
            if mock_case:

                def fetch(url):
                    return protocol.Response(
                        200, b'{"code":1,"message":"Taken"}', {"content-type": "application/json"}
                    )

                result = pipeline(
                    folder,
                    count=count,
                    queue=args.queue,
                    history=history,
                    night=night,
                    gui=gui,
                    fetcher=fetch,
                    existing=50000 if name.endswith("history50k") else 0,
                    results_visible=name == "gui_results_history50k",
                )
            else:
                with (
                    local_server(connect_delay=connect_delay, response_delay=response_delay) as (
                        server,
                        context,
                    ),
                    connection_profile() as (connection_times, tcp_times),
                ):
                    factory = (
                        getattr(protocol, "PersistentTransport", None)
                        if args.transport == "persistent"
                        else None
                    )
                    if args.transport == "persistent" and factory is None:
                        raise RuntimeError("Selected source has no persistent transport")
                    transport = factory(context=context) if factory else None
                    if transport:
                        transport.connection_factory = lambda: http.client.HTTPSConnection(
                            "localhost", server.server_port, context=context, timeout=10
                        )
                    old_context = ssl._create_default_https_context
                    ssl._create_default_https_context = lambda *a, **kw: context

                    def fetch(url, transport=transport):
                        parsed = urllib.parse.urlsplit(url)
                        local = f"https://localhost:{server.server_port}{parsed.path}?{parsed.query}"
                        if transport:
                            return transport.fetch(url)
                        return protocol.fetch(local)

                    try:
                        if name == "tls_transport_only":
                            start = time.perf_counter()
                            results = [fetch(protocol.request_url(f"Bench{i:08d}")) for i in range(count)]
                            elapsed = time.perf_counter() - start
                            counts = Counter(protocol.classify(r)[0] for r in results)
                            assert not counts.get("Unknown")
                            result = dict(
                                count=count,
                                elapsed_s=elapsed,
                                checks_per_s=count / elapsed,
                                network=distribution([r.elapsed for r in results]),
                                classifications=dict(counts),
                                memory_after=memory(),
                            )
                        else:
                            result = pipeline(
                                folder,
                                count=count,
                                queue=args.queue,
                                history=history,
                                night=night,
                                gui=gui,
                                fetcher=fetch,
                                existing=50000 if gui else 0,
                            )
                    finally:
                        ssl._create_default_https_context = old_context
                        if transport:
                            transport.close()
                    result.update(
                        connections=len(server.connect_times),
                        server_tls=distribution(server.connect_times),
                        client_tcp=distribution(tcp_times),
                        client_tcp_tls=distribution(connection_times),
                        client_tls=distribution(
                            [a - b for a, b in zip(connection_times, tcp_times, strict=True)]
                        ),
                        modeled_connection_s=connect_delay,
                        modeled_response_s=response_delay,
                    )
            report["cases"][name] = result
            temporary = path.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(report, indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            print(name, f"{result['checks_per_s']:.3f}/s", flush=True)
    print(path)


if __name__ == "__main__":
    with loopback_network():
        main()
