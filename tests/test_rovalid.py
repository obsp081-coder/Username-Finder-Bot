"""Offline tests of the unmodified engine behind the managed integration boundary."""

import asyncio
import csv
import hashlib
import io
import json
import queue
import sys
from unittest.mock import MagicMock

import pytest

from username_finder import rovalid, rovalid_child, rovalid_review
from username_finder.roblox_activity import RobloxActivity
from username_finder.storage import Store


@pytest.fixture
def database(tmp_path, monkeypatch):
    # Simulate a future separately reviewed installation, only against temporary data/mocks.
    store = Store(tmp_path / "legacy.db")
    for mode in rovalid_review.MODES:
        for connection in rovalid_review.CONNECTIONS:
            state = rovalid.permission(store.path, mode=mode, connection=connection)
            rovalid_review.record_review(
                store.path,
                mode=mode,
                connection=connection,
                decision="authorize",
                reviewer="Offline test",
                evidence="Synthetic temporary fixture only",
                fingerprint=state["fingerprint"],
            )
    yield store.path
    store.close()


@pytest.fixture
def engine(monkeypatch):
    names = ("checker", "config", "wizard", "engine", "proxy", "diagnostics", "storage", "ui")
    before = {name: sys.modules.get(name) for name in names}
    paths = sys.path[:]
    ssl = asyncio.sslproto.SSLProtocol.eof_received
    try:
        import asyncio.proactor_events as proactor

        lost = proactor._ProactorBasePipeTransport._call_connection_lost
    except ImportError:
        proactor = None
    for name in names:
        sys.modules.pop(name, None)
    modules = rovalid_child.engine_modules()
    yield modules
    asyncio.sslproto.SSLProtocol.eof_received = ssl
    if proactor:
        proactor._ProactorBasePipeTransport._call_connection_lost = lost
    sys.path[:] = paths
    for name, module in before.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def test_source_hashes_and_required_files():
    manifest = rovalid.verify_source()
    assert manifest["revision"] == rovalid.REVISION
    assert set(manifest["files"]) == {
        "checker.py",
        "config.py",
        "engine.py",
        "storage.py",
        "proxy.py",
        "diagnostics.py",
        "wizard.py",
        "ui.py",
        "LICENSE",
        "requirements.txt",
    }
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((rovalid.SOURCE / name).read_bytes()).hexdigest() == digest
    assert "GNU GENERAL PUBLIC LICENSE" in (rovalid.SOURCE / "LICENSE").read_text()


@pytest.mark.parametrize("mode", ["two_stage", "validator"])
def test_new_429_invalidates_review_and_requests_managed_stop(tmp_path, database, engine, monkeypatch, mode):
    checker, config, _ = engine
    directory, _ = prepared(tmp_path, engine, options=dict(mode=mode))

    async def restricted(*args):
        stop = checker._StopRequest()
        checker.live_card(ratelimited=1, requests=1)
        assert stop.requested and stop.reason == "provider_restriction_review"

    monkeypatch.setattr(checker, "_run_rounds_owned", restricted)
    monkeypatch.setattr(checker.aiohttp, "ClientSession", lambda **kw: pytest.fail("No dispatch"))
    monkeypatch.setattr(sys, "stdin", Commands())
    monkeypatch.setattr(rovalid_child, "emit", lambda *a, **kw: None)
    rovalid_child.run_engine(directory, database, {}, checker, config)
    assert RobloxActivity(database).block.read_bytes() == b"rovalid_review_required"
    assert rovalid.read_json(directory / "run.json")["state"] == "Stopped"
    for other in rovalid_review.MODES:
        assert not rovalid.permission(database, mode=other)["allowed"]


def test_shipped_integration_requires_historical_review_even_with_fresh_runtime(tmp_path):
    assert not rovalid.permission(tmp_path / "fresh.db")["allowed"]


@pytest.mark.parametrize(
    "changes",
    [
        dict(length=2),
        dict(count=0),
        dict(count=True),
        dict(gap=float("nan")),
        dict(gap=-1),
        dict(mode="other"),
        dict(source="file", repeat=True),
        dict(concurrency=2001),
        dict(timeout=0),
        dict(proxy="secret"),
    ],
)
def test_reject_malformed_settings(changes):
    with pytest.raises(ValueError):
        rovalid.settings(changes)


def test_defaults_and_setting_mapping(engine):
    _, config, _ = engine
    data = rovalid.DEFAULTS | dict(
        mode="validator",
        length=6,
        count=73,
        underscore=True,
        repeat=True,
        concurrency=4,
        timeout=17,
        gap=0.375,
        diagnostics=True,
        stop_on_429=True,
        threshold=2,
        window=30.0,
        remove_bad=False,
    )
    cfg, app = rovalid_child.configure(
        {"settings": data},
        ["Example"],
        dict(proxies=["http://user:private@localhost:8080"], webhook=None, template=None),
        config,
    )
    assert (cfg.two_stage, cfg.concurrency, cfg.timeout) == (False, 4, 17)
    assert (cfg.gen_length, cfg.gen_count, cfg.gen_underscore, cfg.repeat_until_found) == (6, 73, True, True)
    assert not cfg.remove_bad_proxies
    assert (app.proxyless_min_gap, app.diag, app.stop_on_429, app.rate_limit_threshold) == (
        0.375,
        True,
        True,
        2,
    )
    assert app.rate_limit_window == 30 and not app.stream


def test_initial_direct_settings_match_source_wizard(engine, monkeypatch):
    _, _, wizard = engine
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **kw: kw["default"])
    monkeypatch.setattr(wizard.IntPrompt, "ask", lambda *a, **kw: kw["default"])
    workers, timeout, mode = wizard._step_speed([])
    assert (workers, timeout, mode) == (2, 15, True)
    assert (rovalid.DEFAULTS["concurrency"], rovalid.DEFAULTS["timeout"]) == (workers, timeout)


def prepared(
    tmp_path, engine, *, options=None, text="TakenName\nFreeName\nCensoredName\nInvalidName\nUnknownName\n"
):
    _, config, wizard = engine
    directory = tmp_path / "runs" / ("a" * 32)
    directory.mkdir(parents=True)
    source = tmp_path / "names.txt"
    source.write_text(text, encoding="utf-8")
    record = rovalid_child.prepare(
        directory,
        dict(settings=rovalid.DEFAULTS | dict(source="file") | (options or {}), input_path=str(source)),
        config,
        wizard,
    )
    return directory, record


def test_file_validation_and_generation(tmp_path, engine):
    directory, record = prepared(tmp_path, engine, text="Example\nEXAMPLE\na!\n\nSecondName\n")
    assert (record["valid"], record["invalid"], record["duplicates"]) == (2, 1, 1)
    assert (directory / "input.txt").read_text().splitlines() == ["Example", "SecondName"]
    checker, config, wizard = engine
    for length in (3, 4, 5, 6):
        out = tmp_path / str(length)
        out.mkdir()
        generated = rovalid_child.prepare(
            out, dict(settings=rovalid.DEFAULTS | dict(length=length, count=17)), config, wizard
        )
        names = config.load_lines(out / "input.txt")
        assert generated["valid"] == 17 and len(set(names)) == 17
        assert all(config.is_valid_username(name) for name in names)


def test_original_underscore_generation_semantics_are_not_rewritten(engine, monkeypatch):
    _, config, wizard = engine
    monkeypatch.setattr(wizard.random, "choices", lambda *a, **kw: list("abcdef"))
    monkeypatch.setattr(wizard.random, "random", lambda: 0)
    monkeypatch.setattr(wizard.random, "randint", lambda *a: 2)
    names = wizard.generate_usernames(6, 1, True)
    assert names == ["ab_cdef"] and config.is_valid_username(names[0])


@pytest.mark.parametrize("text", ["", "a!\n_\n"])
def test_empty_invalid_input_fails_without_run(tmp_path, engine, text):
    with pytest.raises(ValueError):
        prepared(tmp_path, engine, text=text)


class Commands:
    def __init__(self):
        self.queue = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self):
        return self.queue.get()


class Response:
    status = 200
    headers = {}

    def __init__(self, payload, wait=None):
        self.payload, self.wait = payload, wait

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        if self.wait:
            await self.wait()
        return self.payload


def transport(monkeypatch, checker, *, callback=None):
    calls = []

    class Session:
        def __init__(self, **kwargs):
            assert not kwargs["trust_env"]

        def post(self, endpoint, **kwargs):
            calls.append(("bulk", list(kwargs["json"]["usernames"])))
            return Response({"data": [{"requestedUsername": "TakenName", "name": "TakenName", "id": 1}]})

        def get(self, endpoint, **kwargs):
            name = kwargs["params"]["request.username"]
            assert kwargs["params"]["request.context"] == "Signup"
            assert kwargs["params"]["request.birthday"] == "2000-01-01T00:00:00.000Z"
            calls.append(("validator", name))
            if callback:
                return callback(name)
            return Response(
                {
                    "code": {
                        "TakenName": 1,
                        "FreeName": 0,
                        "CensoredName": 2,
                        "InvalidName": 7,
                        "UnknownName": 987,
                    }[name]
                }
            )

        async def close(self):
            pass

    monkeypatch.setattr(checker.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(checker.aiohttp, "TCPConnector", MagicMock())
    return calls


@pytest.mark.parametrize("mode", ["two_stage", "validator"])
def test_real_engine_modes_structured_counts_and_durable_results(
    tmp_path, database, engine, monkeypatch, mode
):
    checker, config, _ = engine
    directory, _ = prepared(tmp_path, engine, options=dict(mode=mode, concurrency=1))
    calls = transport(monkeypatch, checker)
    commands = Commands()
    monkeypatch.setattr(sys, "stdin", commands)
    events = []
    monkeypatch.setattr(rovalid_child, "emit", lambda event, **data: events.append((event, data)))
    before = database.read_bytes()
    rovalid_child.run_engine(directory, database, {}, checker, config)
    commands.queue.put("stop\n")
    saved = rovalid.read_json(directory / "run.json")
    assert saved["state"] == "Completed"
    summary = saved["summary"]
    assert (
        summary["works"],
        summary["taken"],
        summary["censored"],
        summary["invalid"],
        summary["unresolved"],
    ) == (1, 1, 1, 1, 1)
    assert summary["requests"] == 5
    assert any(c[0] == "bulk" for c in calls) == (mode == "two_stage")
    assert (directory / "results" / "hits.txt").read_text().splitlines() == ["FreeName"]
    assert (directory / "results" / "unresolved.txt").read_text().splitlines() == ["UnknownName"]
    assert events[-1][0] == "finished"
    assert database.read_bytes() == before
    assert RobloxActivity(database).pending.read_bytes().endswith(b"Clear\n")
    assert not any("private" in json.dumps(event) for event in events)
    final_progress = [event[1]["data"] for event in events if event[0] == "progress"][-1]
    assert final_progress["resolved"] == 4 and final_progress["requests"] == 5
    assert final_progress["rps"] == pytest.approx(5 / final_progress["elapsed"])
    assert final_progress["checks_rps"] == pytest.approx(4 / final_progress["elapsed"])


def test_stop_save_and_explicit_unresolved_recovery_no_completed_replay(
    tmp_path, database, engine, monkeypatch
):
    checker, config, _ = engine
    directory, _ = prepared(
        tmp_path, engine, options=dict(mode="validator", concurrency=1), text="TakenName\nFreeName\n"
    )
    commands = Commands()

    async def block():
        commands.queue.put("stop\n")
        await asyncio.sleep(10)

    calls = transport(
        monkeypatch,
        checker,
        callback=lambda name: Response({"code": 1}) if name == "TakenName" else Response({"code": 0}, block),
    )
    monkeypatch.setattr(sys, "stdin", commands)
    monkeypatch.setattr(rovalid_child, "emit", lambda *args, **kwargs: None)
    rovalid_child.run_engine(directory, database, {}, checker, config)
    assert rovalid.read_json(directory / "run.json")["state"] == "Stopped"
    assert (directory / "results" / "unresolved.txt").read_text().splitlines() == ["FreeName"]
    # The old stop class is restored by the test before constructing a second process-equivalent invocation.
    checker._StopRequest = checker._StopRequest.__bases__[0]
    second = Commands()
    monkeypatch.setattr(sys, "stdin", second)
    calls2 = transport(monkeypatch, checker)
    state = rovalid.permission(database, mode="validator")
    assert not state["allowed"]  # Durable marker history changed after the first session.
    rovalid_review.record_review(
        database,
        mode="validator",
        connection="direct",
        decision="authorize",
        reviewer="Offline test",
        evidence="Reviewed completed cleanup and saved stop",
        fingerprint=state["fingerprint"],
    )
    rovalid_child.run_engine(directory, database, dict(resume=True), checker, config)
    second.queue.put("stop\n")
    assert calls == [("validator", "TakenName"), ("validator", "FreeName")]
    assert calls2 == [("validator", "FreeName")]
    assert rovalid.read_json(directory / "run.json")["summary"]["works"] == 1


@pytest.mark.parametrize("marker", ["block", "pending", "cooldown", "control"])
def test_legacy_guards_block_without_network_or_evidence_mutation(
    tmp_path, database, engine, monkeypatch, marker
):
    checker, config, _ = engine
    directory, _ = prepared(tmp_path, engine)
    guard = RobloxActivity(database)
    if marker == "block":
        guard.block.write_bytes(b"http_429")
    elif marker == "pending":
        guard.pending.write_bytes(b"Pending:another\n")
    elif marker == "cooldown":
        guard.cooldown.write_bytes(b"malformed\n")
    else:
        import sqlite3

        with sqlite3.connect(database) as db:
            db.execute("UPDATE roblox_control SET value=?", (json.dumps(dict(blocked="review")),))
    evidence = {
        path: path.read_bytes() for path in (guard.block, guard.pending, guard.cooldown) if path.exists()
    }
    monkeypatch.setattr(checker.aiohttp, "ClientSession", lambda **kwargs: pytest.fail("No dispatch"))
    assert not rovalid.permission(database)["allowed"]
    with pytest.raises(ValueError):
        rovalid_child.run_engine(directory, database, {}, checker, config)
    assert all(path.read_bytes() == data for path, data in evidence.items())
    assert rovalid.read_json(directory / "run.json")["state"] == "Prepared"


def test_engine_failure_keeps_pending_and_refuses_automatic_resume(tmp_path, database, engine, monkeypatch):
    checker, config, _ = engine
    directory, _ = prepared(tmp_path, engine)

    async def failure(*args, **kwargs):
        raise RuntimeError("private proxy credentials must not be forwarded")

    monkeypatch.setattr(checker, "_run_rounds_owned", failure)
    monkeypatch.setattr(sys, "stdin", io.StringIO("stop\n"))
    with pytest.raises(RuntimeError):
        rovalid_child.run_engine(directory, database, {}, checker, config)
    assert rovalid.read_json(directory / "run.json")["state"] == "ReviewRequired"
    assert not rovalid.permission(database)["allowed"]
    assert RobloxActivity(database).pending.read_bytes().startswith(b"Pending:rovalid")
    with pytest.raises(ValueError):
        rovalid_child.run_engine(directory, database, dict(resume=True), checker, config)


def test_results_filters_export_evidence_and_unexpected_crash_history(tmp_path):
    directory = rovalid.run_directory(tmp_path, "a" * 32)
    (directory / "results").mkdir(parents=True)
    (directory / "results" / "hits.txt").write_text("Example\n", encoding="utf8")
    unresolved = directory / "results" / "unresolved.txt"
    unresolved.write_text("OtherName\n=CMD()\n", encoding="utf8")
    before = unresolved.read_bytes()
    rows, total = rovalid.result_rows(directory, classification="Taken")
    assert rows == [] and total == 0
    assert rovalid.result_rows(directory, query="EXAM")[0][0][1] == "Available"
    assert rovalid.result_rows(directory, limit=1)[1] == 3
    out = tmp_path / "export.csv"
    rovalid.export_results(directory, out)
    with out.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 3 and rows[-1]["classification"] == "Unknown"
    assert json.loads(rows[-1]["username"]) == "=CMD()"
    assert json.loads(rows[-1]["json_encoded_fields"]) == ["username"]
    assert unresolved.read_bytes() == before
    rovalid.atomic_json(
        directory / "run.json",
        dict(
            version=1,
            revision=rovalid.REVISION,
            valid=3,
            settings=rovalid.DEFAULTS,
            created_at="2026",
            state="Running",
        ),
    )
    assert rovalid.histories(tmp_path)[0]["state"] == "Interrupted"
    assert rovalid.read_json(directory / "run.json")["state"] == "Running"


def test_malformed_history_remains_visible_and_cannot_resume(tmp_path):
    directory = rovalid.run_directory(tmp_path, "a" * 32)
    rovalid.atomic_json(directory / "run.json", dict(created_at=[], settings="bad", state="Completed"))
    raw = (directory / "run.json").read_bytes()
    rows = rovalid.histories(tmp_path)
    assert rows[0]["state"] == "ReviewRequired"
    assert (directory / "run.json").read_bytes() == raw


def test_incomplete_or_invalid_hit_is_unknown_not_available(tmp_path):
    (tmp_path / "results").mkdir()
    hits = tmp_path / "results" / "hits.txt"
    hits.write_text("=CMD()\nPartialName", encoding="utf8")
    raw = hits.read_bytes()
    assert rovalid.result_rows(tmp_path, classification="Available") == ([], 0)
    rows, count = rovalid.result_rows(tmp_path, classification="Unknown")
    assert count == 2 and all(row[1] == "Unknown" for row in rows)
    assert hits.read_bytes() == raw
    with pytest.raises(ValueError):
        rovalid.export_results(tmp_path, hits)
    with pytest.raises(ValueError):
        rovalid.export_results(tmp_path, tmp_path / "results" / "replace.csv")
    assert hits.read_bytes() == raw


@pytest.mark.parametrize(
    "proxy", ["http://user:secret@localhost:8888", "localhost:8888", "https://localhost:8"]
)
def test_supported_secret_proxy_input(proxy):
    assert rovalid.secrets_payload([proxy], None, None)["proxies"] == [proxy]


@pytest.mark.parametrize(
    "proxy", ["socks5://localhost:88", "https://localhost/no", "localhost", "http://localhost:bad"]
)
def test_unsupported_proxy_rejected_without_echo(proxy):
    with pytest.raises(ValueError) as error:
        rovalid.secrets_payload([proxy], None, None)
    assert proxy not in str(error.value)
