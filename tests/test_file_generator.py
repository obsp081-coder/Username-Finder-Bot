import csv
import itertools
import json
import threading
import tracemalloc
from dataclasses import replace

import pytest

from username_finder import file_generator as generator
from username_finder.file_generator_rules import (
    ALIASES,
    GAMING,
    PROFILES,
    SOCIAL,
    Grammar,
    variants,
    violations,
)


def config(primary=None, secondary=None, format="txt"):
    return {
        "primary": (primary or Grammar(2, 3, "ab")).data(),
        "secondary": secondary.data() if secondary else None,
        "format": format,
    }


def reference(rule):
    alphabet = "".join(dict.fromkeys(rule.alphabet + "".join(row[0] for row in rule.specials)))
    for size in range(rule.minimum, rule.maximum + 1):
        for chars in itertools.product(alphabet, repeat=size):
            value = "".join(chars)
            if not value.startswith(rule.prefix) or not value.endswith(rule.suffix):
                continue
            if not generator.category(value[0], rule.first) or not generator.category(value[-1], rule.last):
                continue
            if any(c + c in value for c in rule.no_repeat):
                continue
            valid = True
            for char, maximum, positions in rule.specials:
                if value.count(char) > maximum:
                    valid = False
                for index, current in enumerate(value):
                    if current != char:
                        continue
                    places = ("s" if index == 0 else "") + ("e" if index == size - 1 else "")
                    if not set(places or "b") <= set(positions):
                        valid = False
            if valid:
                yield value


@pytest.mark.parametrize(
    "rule",
    [
        Grammar(1, 4, "aab1", (("_", 1, "b"),)),
        Grammar(1, 4, "ab", ((".", 3, "sbe"),), no_repeat="."),
        Grammar(2, 4, "ab1", (("_", 2, "sbe"), ("-", 1, "be")), "letter", "alnum"),
        Grammar(1, 4, "ab", prefix="a", suffix="b"),
        Grammar(1, 3, "a", (("_", 0, "sbe"),)),
        Grammar(1, 4, "ab", prefix="aba", suffix="bab"),
    ],
)
def test_exact_counts_unique_multilength_and_every_resume_rank_against_bruteforce(rule):
    space = generator.GrammarSpace(rule)
    expected = list(reference(rule))
    actual = list(space.iter_from())
    assert set(actual) == set(expected)
    assert len(actual) == len(set(actual)) == space.total
    assert space.characters == sum(map(len, actual))
    for rank in range(space.total + 1):
        assert list(space.iter_from(rank)) == actual[rank:]


@pytest.mark.parametrize("platform", SOCIAL + GAMING)
def test_every_approved_profile_loads_with_confidence_and_sources(platform):
    for profile in variants(platform):
        assert profile.confidence in {"Verified", "Provisional", "Incomplete"}
        assert profile.identifier and profile.uniqueness and profile.note and profile.source
        assert generator.GrammarSpace(profile.grammar).total > 0
        if profile.tagline:
            assert generator.GrammarSpace(profile.tagline).total > 0


def test_exact_platform_lists_and_shared_aliases():
    assert len(SOCIAL) == 12 and len(GAMING) == 23
    assert len(set(SOCIAL)) == 12 and len(set(GAMING)) == 23
    for group in (
        ("Valorant", "League of Legends"),
        ("Fortnite", "Rocket League"),
        ("FIFA", "Origin", "Apex Legends"),
        ("GTA V", "Rockstar"),
        ("Steam", "CS:GO"),
    ):
        assert len({ALIASES[p] for p in group}) == 1
    assert [p.key for p in variants("Minecraft")] == ["minecraft_java", "xbox"]
    assert variants("Discord")[0] is PROFILES["discord"]


def test_known_rule_violations_and_incomplete_warnings():
    profile = PROFILES["roblox"]
    assert not violations(profile, profile.grammar)
    custom = replace(profile.grammar, minimum=1, specials=(("_", 3, "sbe"),))
    assert len(violations(profile, custom)) >= 2
    discord = PROFILES["discord"]
    assert violations(discord, replace(discord.grammar, no_repeat=""))
    assert violations(PROFILES["tellonym"], PROFILES["tellonym"].grammar)


def test_compound_identifiers_count_bytes_and_resume_order():
    cfg = config(Grammar(1, 2, "ab"), Grammar(1, 2, "12"))
    space = generator.IdentifierSpace(cfg)
    actual = list(space.iter_from())
    assert space.total == 36 == len(set(actual))
    assert space.bytes == sum(len(value) + 1 for value in actual)
    assert all(value.count("#") == 1 for value in actual)
    for rank in range(37):
        assert list(space.iter_from(rank)) == actual[rank:]
        assert space.offset(rank) == sum(len(value) + 1 for value in actual[:rank])


@pytest.mark.parametrize("format", ["txt", "csv"])
def test_output_formats_and_exact_size(tmp_path, format):
    cfg = config(format=format)
    path = tmp_path / f"names.{format}"
    record = generator.generate_file(cfg, path)
    assert record["state"] == "Completed"
    assert path.stat().st_size == generator.IdentifierSpace(cfg).bytes
    with path.open(encoding="utf-8", newline="") as stream:
        actual = [row[0] for row in csv.reader(stream)] if format == "csv" else stream.read().splitlines()
    assert actual == (["identifier"] if format == "csv" else []) + list(
        generator.IdentifierSpace(cfg).iter_from()
    )
    assert generator.generate_file(cfg, path, resume=True)["state"] == "Completed"


def test_large_stream_cancellation_uncommitted_tail_and_exact_resume(tmp_path):
    cfg = config(Grammar(5, 5, "0123456789"))
    path = tmp_path / "large.txt"
    cancel = threading.Event()
    tracemalloc.start()
    try:
        record = generator.generate_file(cfg, path, cancel=cancel, notify=lambda _: cancel.set())
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert 0 < record["index"] < 100_000 and not path.exists()
    assert peak < 8 * 1024 * 1024
    _, partial, _ = generator.paths(path)
    with partial.open("ab") as stream:
        stream.write(b"uncommitted bytes after simulated crash\n")
    complete = generator.generate_file(cfg, path, resume=True)
    assert complete["index"] == 100_000 and complete["state"] == "Completed"
    with path.open() as stream:
        for index, line in enumerate(stream):
            assert line == f"{index:05d}\n"
    assert index == 99_999


def test_checkpoint_failure_keeps_previous_commit_and_can_resume(tmp_path, monkeypatch):
    cfg = config(Grammar(5, 5, "0123456789"))
    path = tmp_path / "failure.txt"
    save = generator.save_journal

    def fail_after_write(journal, record):
        if record["index"]:
            raise OSError("disk full")
        save(journal, record)

    monkeypatch.setattr(generator, "save_journal", fail_after_write)
    with pytest.raises(OSError, match="disk full"):
        generator.generate_file(cfg, path)
    assert generator.read_journal(path)["index"] == 0
    assert not path.exists()
    monkeypatch.setattr(generator, "save_journal", save)
    assert generator.generate_file(cfg, path, resume=True)["index"] == 100_000
    assert path.stat().st_size == 600_000


def test_corrupt_output_or_changed_config_cannot_resume(tmp_path):
    cfg = config(Grammar(5, 5, "0123456789"))
    path = tmp_path / "corrupt.txt"
    cancel = threading.Event()
    generator.generate_file(cfg, path, cancel=cancel, notify=lambda _: cancel.set())
    with pytest.raises(ValueError):
        generator.generate_file(config(), path, resume=True)
    partial = generator.paths(path)[1]
    with partial.open("r+b") as stream:
        stream.write(b"XXXXX")
    before = partial.read_bytes()
    with pytest.raises(ValueError):
        generator.generate_file(cfg, path, resume=True)
    assert partial.read_bytes() == before


def test_existing_destination_and_insufficient_capacity_never_overwrite(tmp_path, monkeypatch):
    path = tmp_path / "existing.txt"
    path.write_text("preserve")
    with pytest.raises(ValueError):
        generator.generate_file(config(), path)
    assert path.read_text() == "preserve"
    monkeypatch.setattr(generator.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 100})())
    other = tmp_path / "new.txt"
    with pytest.raises(ValueError):
        generator.generate_file(config(), other)
    assert not generator.paths(other)[1].exists()


def test_resume_after_finalization_failure(tmp_path, monkeypatch):
    path = tmp_path / "final.txt"
    original = generator.save_journal

    def finalization_failure(journal, record):
        if record["state"] == "Completed":
            raise OSError("journal unavailable")
        original(journal, record)

    monkeypatch.setattr(generator, "save_journal", finalization_failure)
    with pytest.raises(OSError):
        generator.generate_file(config(), path)
    before = path.read_bytes()
    monkeypatch.setattr(generator, "save_journal", original)
    assert generator.generate_file(config(), path, resume=True)["state"] == "Completed"
    assert path.read_bytes() == before
    assert json.loads(generator.paths(path)[2].read_text())["state"] == "Completed"


def test_inconsistent_checkpoint_index_is_rejected_before_modification(tmp_path):
    cfg = config(Grammar(5, 5, "0123456789"))
    path = tmp_path / "inconsistent.txt"
    cancel = threading.Event()
    generator.generate_file(cfg, path, cancel=cancel, notify=lambda _: cancel.set())
    record = generator.read_journal(path)
    record["index"] += 1
    generator.save_journal(generator.paths(path)[2], record)
    before = generator.paths(path)[1].read_bytes()
    with pytest.raises(ValueError):
        generator.generate_file(cfg, path, resume=True)
    assert generator.paths(path)[1].read_bytes() == before


def test_counting_cancellation_and_grammar_resource_budget(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ValueError):
        generator.IdentifierSpace(config(), cancel)
    monkeypatch.setattr(generator, "STATE_BUDGET", 1)
    with pytest.raises(ValueError):
        generator.IdentifierSpace(config())
