"""Versioned configuration templates, with no job/runtime state or execution side effects."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

from .domain import SearchConfig
from .scheduling import ScheduleConfig
from .sources import fingerprint

PRESET_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def stored_path(path: str) -> str:
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(resolved)


def resolve_path(path: str) -> str:
    candidate = Path(path)
    return str(candidate if candidate.is_absolute() else PROJECT_ROOT / candidate)


def make_payload(config: SearchConfig, schedule: ScheduleConfig, editor: dict) -> dict:
    config.validate()
    schedule.validate()
    # Paths are necessary for TXT templates only; prefer project-relative references.
    files = tuple(stored_path(path) for path in config.files) if config.source == "txt" else ()
    search = replace(config, files=files).to_dict()
    search["files"] = list(files)
    search["categories"] = list(config.categories)
    identities = []
    for name in files:
        path = Path(resolve_path(name))
        try:
            digest = fingerprint(path)
        except OSError:
            digest = None
        identities.append({"path": name, "sha256": digest})
    return {
        "version": PRESET_VERSION,
        "search": search,
        "schedule": asdict(schedule),
        "editor": editor,
        "files": identities,
    }


def validate_payload(payload: dict) -> tuple[SearchConfig, ScheduleConfig, dict]:
    try:
        if payload["version"] != PRESET_VERSION:
            raise ValueError("إصدار هذا القالب غير مدعوم. لم تتغير إعداداتك.")
        search = dict(payload["search"])
        search["files"] = tuple(resolve_path(path) for path in search.get("files", []))
        config = SearchConfig.from_dict(search)
        schedule = ScheduleConfig(**payload["schedule"])
        schedule.validate()
        editor = payload.get("editor", {})
        if not isinstance(editor, dict) or config.provider != "mock":
            raise ValueError("القالب غير متوافق مع وضع المحاكاة الحالي.")
        int_fields = (
            config.workers,
            config.queue_size,
            config.max_retries,
            config.retry_budget,
            config.circuit_threshold,
            config.filters.min_length,
            config.filters.max_length,
            config.filters.repeat_count,
            schedule.duration_minutes,
        )
        bool_fields = (
            config.auto_workers,
            config.smart_history,
            config.filters.letters,
            config.filters.digits,
            config.filters.repeat_exact,
            schedule.start_enabled,
            schedule.stop_enabled,
            schedule.duration_enabled,
        )
        if any(type(value) is not int for value in int_fields) or any(
            type(value) is not bool for value in bool_fields
        ):
            raise ValueError("أنواع قيم القالب غير صالحة.")
        if (
            schedule.duration_minutes > 59999
            or config.timeout_seconds > 300
            or config.retry_budget > 100000
            or config.circuit_threshold > 100
            or config.history_ttl_hours > 87600
            or int(config.timeout_seconds) != config.timeout_seconds
            or round(config.history_ttl_hours, 2) != config.history_ttl_hours
        ):
            raise ValueError("قيم القالب تتجاوز حدود محرر الإعدادات الحالي.")
        if any(not isinstance(value, str) for value in editor.values()) or set(editor) - {
            "repeat_target",
            "limit_text",
        }:
            raise ValueError("قيم محرر القالب غير صالحة.")
        if not set(config.categories) <= set(SearchConfig().categories):
            raise ValueError("القالب يشير إلى مكتبة غير مدعومة.")
        expected = payload.get("files", [])
        if not isinstance(expected, list) or any(
            not isinstance(item.get("path"), str)
            or (
                item.get("sha256") is not None
                and (not isinstance(item["sha256"], str) or len(item["sha256"]) != 64)
            )
            for item in expected
        ):
            raise ValueError("مراجع ملفات القالب غير صالحة.")
        if config.source == "txt" and [resolve_path(item["path"]) for item in expected] != list(config.files):
            raise ValueError("بصمات ملفات القالب ناقصة؛ أعد تحديد الملفات واحفظ قالبًا جديدًا.")
        return config, schedule, editor
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("بيانات القالب ناقصة أو تالفة. لم تتغير إعداداتك.") from exc


def file_issues(payload: dict) -> list[str]:
    issues = []
    for item in payload.get("files", []):
        path = Path(resolve_path(item["path"]))
        try:
            current = fingerprint(path)
        except OSError:
            issues.append(f"الملف غير متاح: {path.name}")
            continue
        if not item.get("sha256") or current != item["sha256"]:
            issues.append(f"تغير محتوى الملف: {path.name}")
    return issues
