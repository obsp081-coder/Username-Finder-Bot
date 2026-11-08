import errno
import re
from pathlib import Path

import pytest

from username_finder.file_generator import generate_file, read_journal
from username_finder.file_generator_output import (
    OUTPUT_FOLDER,
    check_capacity,
    create_output_directory,
    descriptive_filename,
    friendly_error,
    output_directory,
    proposed_output,
)
from username_finder.file_generator_rules import DIGITS, LOWER, Grammar


def config(rule=None, **kwargs):
    return dict(
        platform="Roblox",
        profile="roblox",
        mode="Platform Rules",
        primary=(rule or Grammar(3, 3, LOWER)).data(),
        secondary=None,
        format="txt",
        **kwargs,
    )


def test_descriptive_common_names_ranges_and_formats():
    assert descriptive_filename(config()) == "roblox_3chars_lowercase.txt"
    assert descriptive_filename(config(Grammar(3, 3, LOWER + DIGITS))) == "roblox_3chars_lowercase_digits.txt"
    assert descriptive_filename(config(Grammar(4, 4, DIGITS))) == "roblox_4chars_digits_only.txt"
    cfg = config(Grammar(3, 5, LOWER + DIGITS, (("_", 5, "sbe"),)))
    cfg.update(platform="Instagram", profile="instagram", format="csv")
    assert descriptive_filename(cfg) == "instagram_3-5chars_lowercase_digits_underscore.csv"


def test_subsets_and_advanced_restrictions_are_not_mislabeled():
    rule = Grammar(3, 4, "ab12", (("_", 1, "b"),), first="letter", prefix="a", no_repeat="_")
    cfg = config(rule)
    filename = descriptive_filename(cfg)
    assert "lowercase_subset" in filename and "digits_subset" in filename
    assert "underscore_max1_inside" in filename
    assert "custom_" in filename
    assert re.fullmatch(r"[a-z0-9_.-]+", filename) and len(filename) < 150
    other = config(Grammar(3, 4, "ac12", (("_", 1, "b"),), first="letter", prefix="a", no_repeat="_"))
    assert descriptive_filename(other) != filename


def test_identity_type_and_compound_settings_are_described():
    cfg = config()
    cfg.update(platform="Minecraft", profile="minecraft_java")
    assert "minecraft_java" in descriptive_filename(cfg)
    cfg.update(platform="Valorant", profile="riot", secondary=Grammar(3, 5, DIGITS).data())
    assert "riot_id" in descriptive_filename(cfg) and "tag3-5" in descriptive_filename(cfg)


def test_directory_planning_never_creates_files_or_repeated_subfolders(tmp_path):
    cfg = config(Grammar(1, 1, "ab"))
    proposed = proposed_output(tmp_path, cfg)
    assert proposed.parent == tmp_path / OUTPUT_FOLDER
    assert list(tmp_path.iterdir()) == []
    check_capacity(tmp_path, 4)
    assert list(tmp_path.iterdir()) == []
    create_output_directory(tmp_path, proposed)
    assert output_directory(proposed.parent) == proposed.parent
    assert proposed_output(proposed.parent, cfg).parent == proposed.parent
    record = generate_file(cfg, proposed)
    assert record["destination"] == str(proposed)
    assert read_journal(proposed)["destination"] == str(proposed)
    assert proposed.read_text() == "a\nb\n"


@pytest.mark.parametrize("suffix", ["", ".part", ".generation.json", ".generation.json.tmp"])
def test_every_existing_output_or_recovery_artifact_reserves_filename(tmp_path, suffix):
    cfg = config()
    first = proposed_output(tmp_path, cfg)
    first.parent.mkdir()
    existing = Path(str(first) + suffix)
    existing.write_text("preserve")
    second = proposed_output(tmp_path, cfg)
    assert second == first.with_name(first.stem + "_2.txt")
    assert existing.read_text() == "preserve"
    second.write_text("preserve second")
    assert proposed_output(tmp_path, cfg).name.endswith("_3.txt")


def test_invalid_parent_blocking_file_and_permission_failure(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        output_directory(tmp_path / "missing")
    blocker = tmp_path / OUTPUT_FOLDER
    blocker.write_text("preserve")
    with pytest.raises(ValueError):
        proposed_output(tmp_path, config())
    assert blocker.read_text() == "preserve"
    parent = tmp_path / "other"
    parent.mkdir()
    target = proposed_output(parent, config())
    original = Path.mkdir

    def denied(path, *args, **kwargs):
        if path == target.parent:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(PermissionError) as failure:
        create_output_directory(parent, target)
    assert "إذن" in friendly_error(failure.value)
    assert not target.exists()


def test_capacity_errors_and_invalid_format_are_clear(tmp_path, monkeypatch):
    from username_finder import file_generator

    monkeypatch.setattr(file_generator.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 0})())
    with pytest.raises(ValueError, match="المساحة"):
        check_capacity(tmp_path, 100)
    assert "المساحة" in friendly_error(OSError(errno.ENOSPC, "full"))
    assert "المجلد" in friendly_error(FileNotFoundError())
    assert "حفظ" in friendly_error(OSError("other"))
    cfg = config()
    cfg["format"] = "invalid"
    with pytest.raises(ValueError):
        descriptive_filename(cfg)
