"""Read-only output planning; directory creation happens only after generation consent."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
from pathlib import Path

from .file_generator import paths, resource_check
from .file_generator_rules import DIGITS, LOWER, UPPER

OUTPUT_FOLDER = "Username Finder Bot"


def slug(value):
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def descriptive_filename(config):
    rule = config["primary"]
    low, high = rule["minimum"], rule["maximum"]
    parts = [slug(config["platform"]), f"{low}chars" if low == high else f"{low}-{high}chars"]
    identity = config.get("profile", "")
    if identity in {"minecraft_java", "xbox", "xbox_legacy", "steam_url", "riot", "activision", "battletag"}:
        parts.insert(1, {"minecraft_java": "java", "riot": "riot_id"}.get(identity, identity))
    alphabet = set(rule["alphabet"])
    subset = False
    for label, allowed in (("lowercase", LOWER), ("uppercase", UPPER), ("digits", DIGITS)):
        chosen = alphabet & set(allowed)
        if chosen:
            partial = chosen != set(allowed)
            parts.append(label + ("_subset" if partial else ""))
            subset |= partial
    enabled = [(c, limit, places) for c, limit, places in rule.get("specials", []) if limit and places]
    names = {"_": "underscore", ".": "period", "-": "hyphen"}
    for char, limit, places in enabled:
        label = names[char]
        if limit < high:
            label += f"_max{limit}"
        if places == "b":
            label += "_inside"
        elif set(places) != set("sbe"):
            label += "_positions"
        parts.append(label)
    if alphabet == set(DIGITS) and not enabled:
        parts[parts.index("digits")] = "digits_only"
    for field in ("first", "last"):
        if rule.get(field, "any") != "any":
            parts.append(field + "_" + rule[field])
    if rule.get("prefix"):
        parts.append("prefix_" + slug(rule["prefix"])[:12])
    if rule.get("suffix"):
        parts.append("suffix_" + slug(rule["suffix"])[:12])
    if rule.get("no_repeat"):
        parts.append("no_adjacent")
    if config.get("secondary"):
        tag = config["secondary"]
        parts.append(f"tag{tag['minimum']}-{tag['maximum']}")
    fingerprint = hashlib.sha256(
        json.dumps({"primary": rule, "secondary": config.get("secondary")}, sort_keys=True).encode()
    ).hexdigest()[:8]
    stem = "_".join(parts)
    if subset or config.get("mode") == "Custom Rules" or len(stem) > 140:
        stem = stem[:120].rstrip("_") + "_custom_" + fingerprint
    extension = config["format"]
    if extension not in {"txt", "csv"}:
        raise ValueError("اختر صيغة TXT أو CSV.")
    return stem + "." + extension


def output_directory(parent):
    if not str(parent).strip():
        raise ValueError("اختر المجلد الرئيسي لحفظ الملفات أولًا.")
    parent = Path(parent).expanduser().resolve()
    if not parent.is_dir():
        raise ValueError("المجلد الرئيسي غير موجود أو المسار ليس مجلدًا؛ اختر مجلدًا صالحًا.")
    directory = parent if parent.name.casefold() == OUTPUT_FOLDER.casefold() else parent / OUTPUT_FOLDER
    if directory.exists() and not directory.is_dir():
        raise ValueError("يوجد ملف باسم Username Finder Bot؛ اختر مجلدًا رئيسيًا آخر.")
    return directory


def proposed_output(parent, config):
    directory = output_directory(parent)
    filename = Path(descriptive_filename(config))
    number = 1
    while True:
        target = directory / (filename.name if number == 1 else f"{filename.stem}_{number}{filename.suffix}")
        final, partial, journal = paths(target)
        if not any(
            os.path.lexists(p) for p in (final, partial, journal, journal.with_name(journal.name + ".tmp"))
        ):
            return target
        number += 1


def check_capacity(parent, required):
    directory = output_directory(parent)
    # Planning never creates a directory. Inspect its existing volume through the parent.
    existing = directory if directory.is_dir() else directory.parent
    resource_check(existing / "planned-output.txt", required)


def create_output_directory(parent, destination):
    directory = output_directory(parent)
    if Path(destination).resolve().parent != directory:
        raise ValueError("تغير مسار الحفظ؛ أعد حساب اسم الملف قبل التوليد.")
    directory.mkdir(exist_ok=True)


def friendly_error(error):
    if isinstance(error, PermissionError):
        return "لا يوجد إذن للكتابة في المجلد المحدد؛ اختر مجلدًا آخر أو راجع أذوناته."
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        return "تعذر الوصول إلى المجلد؛ تحقق من المسار وأن وحدة التخزين متصلة."
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return "المساحة الحرة غير كافية؛ وفر مساحة أو اختر وحدة تخزين أخرى."
    if isinstance(error, OSError):
        return "تعذر حفظ الملف؛ تحقق من المسار وأذونات الكتابة ووحدة التخزين."
    return str(error)
