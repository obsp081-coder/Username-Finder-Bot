import csv
import json
import threading
from collections import Counter
from itertools import islice
from urllib.parse import parse_qs, urlsplit

import pytest
from test_roblox import response as roblox_response
from test_x import Clock
from test_x import response as x_response

from username_finder import roblox, x
from username_finder.generation_ranges import allocations
from username_finder.roblox_runner import RobloxRunner
from username_finder.roblox_storage import RobloxStore
from username_finder.storage import Store
from username_finder.x_runner import XRunner
from username_finder.x_storage import XStore


@pytest.fixture(params=[(roblox, 3, 20), (x, 5, 15)], ids=["roblox", "x"])
def platform(request):
    return request.param


def names(module, config):
    spec = {"generation": config}
    return module.input_values(spec) if module is roblox else module.candidates(spec)


def repository(module, store):
    return RobloxStore(store) if module is roblox else XStore(store)


def prepare(module, repo, config, **kwargs):
    spec = {"generation": config}
    return repo.create_run(spec=spec, **kwargs) if module is roblox else repo.prepare(spec, **kwargs)


def queued(repo, identity):
    prefix = "roblox" if isinstance(repo, RobloxStore) else "x"
    return [
        row[0]
        for row in repo.store.db.execute(
            f"SELECT username FROM {prefix}_queue WHERE run_id=? ORDER BY position", (identity,)
        )
    ]


@pytest.mark.parametrize("quantity", [1, 2, 3, 11, 100])
def test_inclusive_total_balance_dedup_and_platform_syntax(platform, quantity):
    module, lower, upper = platform
    config = module.generation_range_spec(lower, upper, quantity)
    values = list(names(module, config))
    counts = Counter(map(len, values))
    assert len(values) == len({name.casefold() for name in values}) == quantity
    assert set(counts) <= set(range(lower, upper + 1))
    assert max(counts.values()) - min(counts.get(length, 0) for length in range(lower, upper + 1)) <= 1
    if quantity >= upper - lower + 1:
        assert set(counts) == set(range(lower, upper + 1))
    for name in values:
        if module is roblox:
            assert roblox.syntax_error(name) is None
            assert name[0].isalpha() and "_" not in name
        else:
            assert x.protocol.validate_syntax(name)["valid"]
    assert list(names(module, json.loads(json.dumps(config)))) == values


@pytest.mark.parametrize("bad", ["reverse", "low", "high", "zero", "negative", "fraction", "bool"])
def test_invalid_ranges_quantities_and_bounds(platform, bad):
    module, lower, upper = platform
    minimum, maximum, quantity = {
        "reverse": (upper, lower, 10),
        "low": (lower - 1, lower, 10),
        "high": (upper, upper + 1, 10),
        "zero": (lower, upper, 0),
        "negative": (lower, upper, -1),
        "fraction": (lower + 0.5, upper, 10),
        "bool": (lower, upper, True),
    }[bad]
    with pytest.raises(ValueError):
        module.generation_range_spec(minimum, maximum, quantity)


def test_equal_limits_keep_legacy_schema_and_random_sequence(platform, monkeypatch):
    module, lower, _ = platform
    monkeypatch.setattr(module.secrets, "randbelow", lambda _: 0)
    original = module.generation_spec(lower, 3)
    ranged = module.generation_range_spec(lower, lower, 3)
    assert original == ranged
    assert set(ranged) == {"length", "quantity", "multiplier", "offset"}
    assert list(names(module, ranged)) == ["a" * lower, "a" * (lower - 1) + "b", "a" * (lower - 1) + "c"]


def test_old_roblox_length_4_has_identical_candidates():
    old = {"length": 4, "quantity": 3, "multiplier": 1, "offset": 0}
    assert list(roblox.input_values({"generation": old})) == ["aaaa", "aaab", "aaac"]


def test_capacity_redistribution_exact_exhaustion_and_rejection():
    assert allocations(3, 5, 14, lower=3, upper=5, capacity=lambda length: 4 if length == 3 else 100) == {
        3: 4,
        4: 5,
        5: 5,
    }
    assert allocations(3, 5, 11, lower=3, upper=5, capacity=lambda length: {3: 1, 4: 3, 5: 20}[length]) == {
        3: 1,
        4: 3,
        5: 7,
    }
    assert allocations(3, 5, 6, lower=3, upper=5, capacity=lambda length: length - 2) == {3: 1, 4: 2, 5: 3}
    with pytest.raises(ValueError):
        allocations(3, 5, 7, lower=3, upper=5, capacity=lambda length: length - 2)
    first_capacity = 26 * 36**2
    config = roblox.generation_range_spec(3, 4, first_capacity * 2 + 3)
    assert [part["quantity"] for part in config["length_specs"]] == [first_capacity, first_capacity + 3]
    values = list(roblox.input_values({"generation": config}))
    assert len(values) == len(set(values)) == first_capacity * 2 + 3


def test_all_small_capacity_allocations_stay_balanced_after_exhaustion():
    for first in range(1, 5):
        for second in range(1, 5):
            limits = {3: first, 4: second, 5: 7}
            for quantity in range(1, sum(limits.values()) + 1):
                counts = allocations(3, 5, quantity, lower=3, upper=5, capacity=limits.__getitem__)
                assert sum(counts.values()) == quantity
                assert all(counts.get(length, 0) <= capacity for length, capacity in limits.items())
                unsaturated = [
                    counts.get(length, 0) for length in limits if counts.get(length, 0) < limits[length]
                ]
                if unsaturated:
                    assert max(unsaturated) - min(unsaturated) <= 1


def test_large_range_is_lazy_and_bounded(platform):
    module, lower, upper = platform
    config = module.generation_range_spec(lower, upper, 10**9)
    assert sum(part["quantity"] for part in config["length_specs"]) == 10**9
    assert len(list(islice(names(module, config), 20))) == 20
    total = sum(
        (26 * 36 ** (length - 1) if module is roblox else 37**length) for length in range(lower, upper + 1)
    )
    with pytest.raises(ValueError):
        module.generation_range_spec(lower, upper, total + 1)


@pytest.mark.parametrize("legacy", [False, True], ids=["range", "legacy-fixed"])
def test_preparation_restart_retains_exact_candidates_and_config(platform, tmp_path, legacy):
    module, lower, _ = platform
    config = (
        module.generation_spec(lower, 1003)
        if legacy
        else module.generation_range_spec(lower, lower + 2, 1003)
    )
    expected = list(names(module, config))
    path = tmp_path / "prepare.db"
    store = Store(path)
    cancel = threading.Event()
    repo = repository(module, store)
    if module is roblox:
        identity = prepare(module, repo, config, cancel=cancel, notify=lambda _: cancel.set())
    else:

        def checkpoint(identity):
            if repo.progress(identity)["cursor"] >= 1000:
                cancel.set()

        identity = prepare(module, repo, config, cancel=cancel, notify=checkpoint)
    assert repo.progress(identity)["state"] == "PreparationPaused"
    prefix = queued(repo, identity)
    assert prefix and prefix == expected[: len(prefix)]
    field = "preparation" if module is roblox else "spec"
    saved = repo.progress(identity)[field]
    store.close()
    reopened = Store(path)
    try:
        repo = repository(module, reopened)
        repo.recover()
        if module is roblox:
            repo.create_run(run_id=identity)
        else:
            repo.prepare(run_id=identity)
        assert repo.progress(identity)[field] == saved
        assert queued(repo, identity) == expected
        assert repo.progress(identity)["duplicates"] == 0
        assert repo.progress(identity)["state"] == "Ready"
    finally:
        reopened.close()


def test_mixed_checks_cancel_restart_history_skip_filter_export(platform, tmp_path):
    module, lower, _ = platform
    config = module.generation_range_spec(lower, lower + 2, 9)
    expected = list(names(module, config))
    path = tmp_path / "checks.db"
    store = Store(path)
    repo = repository(module, store)
    identity = prepare(module, repo, config)
    calls = []
    clock = Clock()

    def worker(repo, cancel_after=None):
        def fetch(value, *args):
            name = parse_qs(urlsplit(value).query)["Username"][0] if module is roblox else value
            calls.append(name)
            if cancel_after and len(calls) == cancel_after:
                runner.cancel.set()
            return roblox_response() if module is roblox else x_response()

        runner = (
            RobloxRunner(repo, fetcher=fetch, clock=clock.now, wait=clock.wait)
            if module is roblox
            else XRunner(repo, fetcher=fetch, clock=clock.now, wait=clock.wait)
        )
        return runner

    worker(repo, 3).run(identity)
    assert repo.progress(identity)["state"] == "Paused" and calls == expected[:3]
    store.close()
    reopened = Store(path)
    try:
        repo = repository(module, reopened)
        repo.recover()
        worker(repo).run(identity)
        assert calls == expected and len(set(calls)) == 9
        assert repo.progress(identity)["state"] == "Completed"
        expected_class = "Available" if module is roblox else "Taken"
        assert repo.results(expected_class)[1] == 9
        assert all(len(repo.history(name)) == 1 for name in expected)
        repeat = prepare(module, repo, config)
        assert repo.progress(repeat)["skipped"] == 9
        worker(repo).run(repeat)
        assert calls == expected
        target = tmp_path / "results.csv"
        if module is roblox:
            repo.export_csv(target, "Available")
        else:
            repo.export(target, expected_class)
        with target.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert {row["username"] for row in rows} == set(expected)
    finally:
        reopened.close()
