"""Proxy preparation uses mocks only; public-source reachability never establishes availability."""

import json
import sys
from unittest.mock import AsyncMock

import pytest
from test_rovalid import database as database
from test_rovalid import engine as engine

from username_finder import rovalid, rovalid_child
from username_finder.roblox_activity import RobloxActivity


def test_proxy_format_dedup_keeps_distinct_credentials_private():
    names = ["HOST:80", "http://host:80", "http://user:Pass@host:80", "http://user:pass@host:80"]
    pool, counts = rovalid.proxy_entries(names)
    assert len(pool) == 3 and counts == dict(source_count=4, duplicates=1, accepted=3)
    assert pool[-2:] == names[-2:]


@pytest.mark.parametrize(
    "name",
    [
        "http://u:secret@host:80/path",
        "http://u:secret@host:80?token=x",
        "http://u:secret@host:80#x",
        "http://u:secret@host:99999",
        "http://u:secret@host:80\tX",
        "http://u:secret@[broken:80",
        "socks5://user:secret@host:80",
    ],
)
def test_bad_proxy_errors_never_echo_credentials(name):
    with pytest.raises(ValueError) as error:
        rovalid.proxy_entries([name])
    assert "secret" not in str(error.value) and name not in str(error.value)


def test_mocked_fetch_calls_original_entrypoints_and_preserves_free_semantics(database, engine, monkeypatch):
    _, config, wizard = engine
    scrape = AsyncMock(return_value=["host:80", "http://HOST:80", "other:81"])
    screen = AsyncMock(return_value=["http://host:80"])
    monkeypatch.setattr(wizard, "_scrape_proxies", scrape)
    monkeypatch.setattr(sys.modules["proxy"], "prescreen", screen)
    result = rovalid_child.fetch_pool(database, wizard)
    assert result == dict(
        proxies=["http://host:80"],
        scraped=True,
        source_count=3,
        duplicates=1,
        screened=2,
        accepted=1,
        failures=1,
    )
    screen.assert_awaited_once_with(["host:80", "other:81"])
    cfg, _ = rovalid_child.configure(dict(settings=rovalid.DEFAULTS), ["Example"], result, config)
    assert cfg.scraped and cfg.proxies == result["proxies"]
    cfg, _ = rovalid_child.configure(
        dict(settings=rovalid.DEFAULTS), ["Example"], dict(proxies=["host:80"]), config
    )
    assert not cfg.scraped
    assert not RobloxActivity(database).pending.exists()


@pytest.mark.parametrize("kind", ["historical", "pending", "cooldown", "policy"])
def test_fetch_rejected_before_any_source_or_screen_call(database, engine, monkeypatch, kind):
    _, _, wizard = engine
    guard = RobloxActivity(database)
    if kind == "historical":
        guard.block.write_bytes(b"http_429")
    elif kind == "pending":
        guard.pending.write_bytes(b"Pending:other\n")
    elif kind == "cooldown":
        guard.cooldown.write_bytes(b"bad\n")
    else:
        monkeypatch.setattr(rovalid, "HISTORICAL_REVIEW_REQUIRED", True)
    original = {p: p.read_bytes() for p in (guard.block, guard.pending, guard.cooldown) if p.exists()}
    scrape = AsyncMock(side_effect=AssertionError("No external calls"))
    monkeypatch.setattr(wizard, "_scrape_proxies", scrape)
    with pytest.raises(ValueError):
        rovalid_child.fetch_pool(database, wizard)
    scrape.assert_not_awaited()
    assert original == {p: p.read_bytes() for p in original}


def test_authenticated_public_pool_never_emitted(database, engine, monkeypatch):
    _, _, wizard = engine
    monkeypatch.setattr(wizard, "_scrape_proxies", AsyncMock(return_value=["http://user:secret@host:80"]))
    screen = AsyncMock()
    monkeypatch.setattr(sys.modules["proxy"], "prescreen", screen)
    with pytest.raises(ValueError) as error:
        rovalid_child.fetch_pool(database, wizard)
    screen.assert_not_awaited()
    assert "secret" not in str(error.value)


def test_source_count_claims_are_only_available_after_screen(database, engine, monkeypatch):
    _, _, wizard = engine
    monkeypatch.setattr(wizard, "_scrape_proxies", AsyncMock(return_value=[]))
    monkeypatch.setattr(sys.modules["proxy"], "prescreen", AsyncMock(return_value=[]))
    data = rovalid_child.fetch_pool(database, wizard)
    assert data["source_count"] == data["screened"] == data["accepted"] == data["failures"] == 0
    assert "secret" not in json.dumps(data)
