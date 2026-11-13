"""RoValid integration records and read-only legacy safety checks; no engine imports."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from .roblox_local_selection import spreadsheet_row
from .roblox_storage import RobloxStore
from .rovalid_filters import parse_filters
from .vendor.rovalid.config import is_valid_username

SOURCE = Path(__file__).parent / "vendor" / "rovalid"
REVISION = "d9cba38980fa13a33cc5b874c989105f8b9ff27f"
DEFAULTS = dict(
    source="generate",
    length=4,
    count=100,
    underscore=False,
    repeat=False,
    mode="two_stage",
    concurrency=2,
    timeout=15,
    gap=0.0,
    diagnostics=False,
    integration_monitoring=True,
    stop_on_429=False,
    threshold=5,
    window=60.0,
    remove_bad=True,
)
LABELS = {
    "Available": "متاح أوليًا",
    "Taken": "مستخدم",
    "Censored": "مرفوض رقابيًا",
    "Invalid": "غير صالح",
    "Unknown": "غير محسوم",
}


def timestamp():
    return datetime.now(UTC).isoformat()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".next")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, allow_nan=False)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def read_json(path):
    with Path(path).open(encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError("Invalid integration record")
    return value


def settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("Unsupported RoValid settings")
    data = DEFAULTS | value
    for field in (
        "underscore",
        "repeat",
        "diagnostics",
        "integration_monitoring",
        "stop_on_429",
        "remove_bad",
    ):
        if type(data[field]) is not bool:
            raise ValueError("Invalid Boolean setting")
    for field, low, high in (
        ("length", 3, 6),
        ("count", 1, 1_000_000),
        ("concurrency", 1, 2000),
        ("timeout", 1, 300),
        ("threshold", 1, 10000),
    ):
        if type(data[field]) is not int or not low <= data[field] <= high:
            raise ValueError("Invalid numeric setting: " + field)
    for field in ("gap", "window"):
        v = data[field]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError("Invalid interval")
        if not (0 <= v <= 3600 if field == "gap" else 0 < v <= 86400):
            raise ValueError("Invalid interval range")
    if data["source"] not in ("generate", "file") or data["mode"] not in ("two_stage", "validator"):
        raise ValueError("Invalid mode")
    if data["source"] == "file" and data["repeat"]:
        raise ValueError("Repeat requires generated names")
    return data


def verify_source():
    manifest = read_json(SOURCE / "SOURCE.json")
    if manifest["revision"] != REVISION:
        raise ValueError("Unexpected RoValid revision")
    for name, expected in manifest["files"].items():
        if Path(name).name != name or hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() != expected:
            raise ValueError("RoValid source integrity check failed")
    return manifest


@contextmanager
def readonly_repo(database):
    path = Path(database).resolve()
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        yield RobloxStore(SimpleNamespace(path=path, db=db, lock=threading.RLock()))
    finally:
        db.close()


def permission(database, *, mode="two_stage", connection="direct", lock_held=False):
    from .rovalid_review import permission as reviewed_permission

    return reviewed_permission(database, mode=mode, connection=connection, lock_held=lock_held)


def proxy_entries(proxies):
    entries, seen, duplicates = [], set(), 0
    if not isinstance(proxies, list) or any(not isinstance(p, str) for p in proxies):
        raise ValueError("Invalid proxy input")
    for raw in proxies:
        entry = raw.strip()
        if not entry:
            continue
        if any(c.isspace() or ord(c) < 32 for c in entry):
            raise ValueError("صيغة proxy غير صالحة؛ لم تُعرض بياناته.")
        try:
            parsed = urlsplit(entry if "://" in entry else "http://" + entry)
            port = parsed.port
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or not port
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError()
        except ValueError:
            raise ValueError("صيغة proxy غير صالحة؛ لم تُعرض بياناته.") from None
        # Credentials are case-sensitive: never merge distinct accounts on a common endpoint.
        key = (parsed.scheme.lower(), parsed.hostname.lower(), port, parsed.username, parsed.password)
        if key in seen:
            duplicates += 1
        else:
            entries.append(entry)
            seen.add(key)
    return entries, dict(source_count=len(entries) + duplicates, duplicates=duplicates, accepted=len(entries))


def secrets_payload(proxies, webhook, template):
    entries, _ = proxy_entries(proxies)
    if webhook:
        parsed = urlsplit(webhook)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ("discord.com", "discordapp.com")
            or not parsed.path.startswith("/api/webhooks/")
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("عنوان Discord webhook غير صالح.")
        if not template or len(template) > 1900:
            raise ValueError("يلزم قالب رسالة من 1 إلى 1900 حرف.")
    return dict(proxies=entries, webhook=webhook or None, template=template if webhook else None)


def run_directory(root, identity):
    if (
        not isinstance(identity, str)
        or len(identity) != 32
        or any(c not in "0123456789abcdef" for c in identity)
    ):
        raise ValueError("Invalid run identity")
    directory = Path(root).resolve() / identity
    if directory.is_symlink() or directory.resolve().parent != Path(root).resolve():
        raise ValueError("Unsafe run directory")
    return directory


def histories(root):
    rows = []
    if Path(root).exists():
        for path in Path(root).glob("*/run.json"):
            try:
                if path.parent != run_directory(root, path.parent.name):
                    continue
                data = read_json(path)
                if (
                    data.get("version") != 1
                    or data.get("revision") != REVISION
                    or not isinstance(data.get("created_at"), str)
                    or type(data.get("valid")) is not int
                    or data["valid"] < 0
                    or data.get("state")
                    not in {"Prepared", "Running", "Stopped", "Completed", "ReviewRequired"}
                ):
                    raise ValueError("Malformed run metadata")
                settings(data["settings"])
                data["id"] = path.parent.name
                if data.get("state") == "Running":
                    data = data | {
                        "state": "Interrupted",
                        "reason": "لم يُثبت إنهاء العملية؛ يلزم تدقيق محلي.",
                    }
                rows.append(data)
            except (OSError, ValueError, KeyError, TypeError):
                rows.append(
                    dict(
                        id=path.parent.name,
                        created_at="",
                        state="ReviewRequired",
                        settings={},
                        reason="بيانات سجل غير سليمة؛ يلزم تدقيق الملفات الأصلية دون تشغيل.",
                        valid="—",
                    )
                )
    return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)


def saved_results(directory, cancelled=lambda: False):
    """Read the engine's actual files; an incomplete hit is never an availability verdict."""
    for filename, label in (("hits.txt", "Available"), ("unresolved.txt", "Unknown")):
        path = Path(directory) / "results" / filename
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as stream:
            before = os.fstat(stream.fileno())
            for line in stream:
                if cancelled():
                    raise SearchCancelled("Local operation cancelled")
                name = line.strip()
                if name:
                    complete = filename != "hits.txt" or line.endswith("\n")
                    verdict = label if complete and is_valid_username(name) else "Unknown"
                    yield [name, verdict, "RoValid: " + filename]
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Source results changed during local reading; repeat the view explicitly")


class SearchCancelled(Exception):
    pass


def matching_results(directory, predicate, cancelled=lambda: False):
    for row in saved_results(directory, cancelled):
        if cancelled():
            raise SearchCancelled("Local operation cancelled")
        if predicate.matches(row):
            yield row


def result_rows(
    directory, *, query="", classification="", limit=500, offset=0, filters=None, cancelled=lambda: False
):
    """Only hits and unresolved are canonical per-name evidence in this engine."""
    rows, count = [], 0
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("Invalid result page")
    predicate = parse_filters(filters, query=query, classification=classification)
    for row in matching_results(directory, predicate, cancelled):
        if count >= offset and len(rows) < limit:
            rows.append(row)
        count += 1
    return rows, count


def export_results(
    directory, destination, *, query="", classification="", filters=None, cancelled=lambda: False
):
    # Export from persisted evidence, never from terminal text or an in-flight feed.
    target = Path(destination).resolve()
    if target.is_relative_to(Path(directory).resolve()) or target.suffix.lower() != ".csv":
        raise ValueError("Export must be a CSV outside the source run directory")
    predicate = parse_filters(filters, query=query, classification=classification)
    fields = ["username", "classification", "source"]
    fd, staging = tempfile.mkstemp(prefix=".rovalid-export-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow([*fields, "json_encoded_fields"])
            count = 0
            for row in matching_results(directory, predicate, cancelled):
                writer.writerow(spreadsheet_row(row, fields))
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        if cancelled():
            raise SearchCancelled("Export cancelled before publish")
        Path(staging).replace(target)
        return count
    finally:
        Path(staging).unlink(missing_ok=True)
