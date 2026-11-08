import csv
import json
import socket

import pytest
from osintgram_fixture import PROFILE, TOOLS, FakeBackend, param
from PySide6.QtCore import QUrl
from PySide6.QtNetwork import QNetworkProxy

from username_finder.osintgram_api import (
    BASE_URL,
    ContractError,
    LocalOsintgramClient,
    build_payload,
    error_message,
    parameter_value,
    validate_tools,
)
from username_finder.osintgram_results import (
    MISSING,
    csv_compatible,
    datasets,
    display_value,
    export_csv,
    export_json,
    json_preview,
    safe_result,
    table_data,
)


@pytest.fixture
def backend():
    service = FakeBackend()
    yield service
    service.close()


@pytest.fixture
def client(qtbot, backend):
    api = LocalOsintgramClient()
    api._url = lambda path: QUrl(backend.url + path)
    yield api
    api.shutdown()
    qtbot.waitUntil(lambda: not api.busy)
    api.deleteLater()


def connect(qtbot, client):
    with qtbot.waitSignal(client.tools_ready, timeout=2000):
        client.discover()
    assert client.connected


def test_fixed_local_transport_and_discovery(client, backend, qtbot):
    untouched = LocalOsintgramClient()
    assert untouched._url("/api/run").toString() == BASE_URL + "/api/run"
    assert untouched.manager.proxy().type() == QNetworkProxy.ProxyType.NoProxy
    untouched.deleteLater()
    connect(qtbot, client)
    assert backend.calls == [("GET", "/api/tools", None)]
    assert validate_tools(TOOLS) == TOOLS


@pytest.mark.parametrize(
    "value", [None, {}, [], [{}], [{"name": "x", "needs_target": False, "params": [param("x", "invalid")]}]]
)
def test_incompatible_discovery_is_explicit(value):
    with pytest.raises(ContractError):
        validate_tools(value)


def test_payload_target_limits_and_unique_ids():
    with pytest.raises(ContractError):
        build_payload(TOOLS, {"get_user_info": {}}, "", 10)
    with pytest.raises(ContractError):
        build_payload(TOOLS, {"search_hashtag": {}}, "", 10)
    for limit in (None, 0, -1, 1001, True):
        with pytest.raises(ContractError):
            build_payload(TOOLS, {"get_user_info": {}}, "test", limit)
    args = {"search_hashtag": {"hashtag": "fixture", "sort": "recent", "limit": "15"}}
    first = build_payload(TOOLS, args, "", 25)
    second = build_payload(TOOLS, args, "", 25)
    assert first["run_id"] != second["run_id"]
    assert first["target"] is None and first["max_items"] == 25 and first["verbose"] is True
    assert first["calls"] == [
        {"name": "search_hashtag", "args": {"hashtag": "fixture", "sort": "recent", "limit": 15}}
    ]
    assert build_payload(TOOLS, {"get_user_info": {}}, " @FIXTURE ", 10)["target"] == "fixture"
    for selected in ({}, {"unknown": {}}, {"get_user_info": {"unknown": "x"}}):
        with pytest.raises(ContractError):
            build_payload(TOOLS, selected, "test", 10)


@pytest.mark.parametrize(
    "spec,text,expected",
    [
        (param("limit"), "12", 12),
        (param("flag", "boolean", False), "false", False),
        (param("value", "number", None), "1.5", 1.5),
        (param("data", "object", None), '{"a": 1}', {"a": 1}),
        (param("data", "array", None), '[1, "x"]', [1, "x"]),
    ],
)
def test_parameter_types(spec, text, expected):
    assert parameter_value(spec, text) == expected


@pytest.mark.parametrize("text", ["all", "none", "0", "-2", "1.5", "100001"])
def test_invalid_integers_cannot_become_unlimited(text):
    with pytest.raises(ContractError):
        parameter_value(param("limit"), text)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 502])
def test_http_errors_are_safe_and_do_not_imply_availability(client, backend, qtbot, status):
    connect(qtbot, client)
    backend.run_status = status
    backend.detail = (
        "login required session_secret=DO_NOT_DISPLAY" if status in (401, 403) else "DO_NOT_DISPLAY"
    )
    with qtbot.waitSignal(client.finished, timeout=2000) as signal:
        client.run(build_payload(TOOLS, {"get_user_info": {}}, "fixture", 10))
    assert signal.args[0] == "failed" and f"HTTP {status}" in signal.args[1]
    assert "DO_NOT_DISPLAY" not in signal.args[1]
    assert client.connected  # HTTP/auth failure is distinct from a refused connection.
    qtbot.waitUntil(lambda: not client.busy)


def test_connection_refusal_and_recovery(client, backend, qtbot):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        unused = probe.getsockname()[1]
    client._url = lambda path: QUrl(f"http://127.0.0.1:{unused}" + path)
    with qtbot.waitSignal(client.connection_error, timeout=6500):
        client.discover()
    assert not client.connected
    client._url = lambda path: QUrl(backend.url + path)
    connect(qtbot, client)


@pytest.mark.parametrize("raw", [b"", b"not json", b"{}", b"[]", b"[{}]"])
def test_malformed_discovery(client, backend, qtbot, raw):
    backend.tools_raw = raw
    with qtbot.waitSignal(client.connection_error, timeout=2000):
        client.discover()
    assert not client.connected


@pytest.mark.parametrize("mode", ["empty", "malformed", "incomplete", "trailing", "mismatch"])
def test_invalid_streams_fail_without_success(client, backend, qtbot, mode):
    connect(qtbot, client)
    backend.mode = mode
    with qtbot.waitSignal(client.finished, timeout=2000) as signal:
        client.run(build_payload(TOOLS, {"get_user_info": {}}, "fixture", 10))
    assert signal.args[0] == "failed"
    qtbot.waitUntil(lambda: not client.busy)


def test_stream_events_payload_and_cancellation(client, backend, qtbot):
    connect(qtbot, client)
    backend.delay = 2
    payload = build_payload(TOOLS, {"get_user_info": {}}, "fixture", 10)
    with qtbot.waitSignal(client.event_received, timeout=2000):
        client.run(payload)
    assert client.busy
    with pytest.raises(ContractError):
        client.run(payload)
    with qtbot.waitSignal(client.finished, timeout=2000) as signal:
        with qtbot.waitSignal(client.cancellation, timeout=2000) as cancelled:
            client.cancel()
        assert cancelled.args[0] is True
    assert signal.args[0] == "cancelled"
    assert ("POST", "/api/run", payload) in backend.calls
    assert ("POST", "/api/cancel", {"run_id": payload["run_id"]}) in backend.calls


def test_timeout_requests_backend_cancel(client, backend, qtbot):
    connect(qtbot, client)
    backend.delay = 2
    client.run_timeout_ms = 100
    with qtbot.waitSignal(client.finished, timeout=2000) as signal:
        client.run(build_payload(TOOLS, {"get_user_info": {}}, "fixture", 10))
    assert signal.args[0] == "failed" and "مهلة" in signal.args[1]
    qtbot.waitUntil(lambda: not client.busy)
    assert any(path == "/api/cancel" for method, path, data in backend.calls)


def test_redirects_are_rejected_without_following(client, backend, qtbot):
    backend.tools_status = 302
    backend.redirect = backend.url + "/must-not-follow"
    with qtbot.waitSignal(client.connection_error, timeout=2000):
        client.discover()
    assert not client.connected and len(backend.calls) == 1


@pytest.mark.parametrize("bound", ["MAX_EVENT", "MAX_RESPONSE"])
def test_oversized_stream_is_bounded_and_cancelled(client, backend, qtbot, monkeypatch, bound):
    import username_finder.osintgram_api as api_module

    connect(qtbot, client)
    attempts = []
    original = client._request

    def request(kind, payload=None):
        attempts.append((kind, payload))
        return original(kind, payload)

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(api_module, bound, 250)
    with qtbot.waitSignal(client.finished, timeout=2000) as signal:
        client.run(build_payload(TOOLS, {"get_user_info": {}}, "fixture", 10))
    assert signal.args[0] == "failed" and "حد" in signal.args[1]
    qtbot.waitUntil(lambda: not client.busy)
    assert any(kind == "cancel" for kind, payload in attempts)


def test_discovery_timeout_is_bounded_and_recovers(client, backend, qtbot):
    client.discovery_timeout_ms = 60
    backend.tools_delay = 0.2
    with qtbot.waitSignal(client.connection_error, timeout=2000) as signal:
        client.discover()
    assert "مهلة" in signal.args[0] and not client.busy
    backend.tools_delay = 0
    client.discovery_timeout_ms = 5000
    connect(qtbot, client)


def test_structured_projection_and_safe_exports(tmp_path):
    value = {"nested": {"rows": [{"id": PROFILE["id"]}, {"other": None}]}, "empty": [], "null": None}
    found = datasets(value)
    assert [name for name, data in found] == ["النتيجة", "nested.rows", "empty"]
    headers, rows = table_data(found[1][1])
    assert headers == ["id", "other"] and rows[0][0] == PROFILE["id"]
    assert rows[0][1] is MISSING and rows[1][0] is MISSING and rows[1][1] is None
    assert len({display_value(v) for v in (None, "", MISSING, [], {})}) == 5
    assert not csv_compatible(found[1][1])
    flat = [{"id": PROFILE["id"], "text": "=1+1", "missing_value": None, "blank": ""}]
    export_csv(tmp_path / "result.csv", flat)
    with (tmp_path / "result.csv").open(encoding="utf-8-sig", newline="") as stream:
        values = list(csv.reader(stream))
    assert [json.loads(cell) for cell in values[1]] == [PROFILE["id"], "=1+1", None, ""]
    export_json(tmp_path / "result.json", value)
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == value
    preview = json_preview([{"long": "x" * 10000}] * 10000)
    assert len(preview) < 66000 and "preview" in preview
    clean = safe_result({"session_token": "DO_NOT_DISPLAY", "error": "login required DO_NOT_DISPLAY"})
    assert "DO_NOT_DISPLAY" not in str(clean) and "جلسة" in clean["error"]
    assert "جلسة" in error_message(status=401)
