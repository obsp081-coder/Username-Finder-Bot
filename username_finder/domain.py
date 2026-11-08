from __future__ import annotations

import re
import string
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

LETTERS = string.ascii_lowercase
DIGITS = string.digits
SPECIALS = "_.-"
RULES_VERSION = "snapchat-syntax-2026-11-05-v1"


class Classification(StrEnum):
    AVAILABLE = "Available"
    TAKEN = "Taken"
    RESERVED = "Reserved"
    INVALID = "Invalid"
    UNKNOWN = "Unknown"


class JobState(StrEnum):
    READY = "Ready"
    RUNNING = "Running"
    PAUSED = "Paused"
    STOPPED = "Stopped"
    INTERRUPTED = "Interrupted"
    COMPLETED = "Completed"
    BLOCKED = "Blocked"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def normalize(value: str) -> str:
    return value.strip().translate(str.maketrans(string.ascii_uppercase, LETTERS))


def syntax_error(username: str) -> str | None:
    if not 3 <= len(username) <= 15:
        return "LENGTH"
    if username[0] not in LETTERS:
        return "START_LETTER"
    if username[-1] not in LETTERS + DIGITS:
        return "END_ALPHANUMERIC"
    if not re.fullmatch(r"[a-z0-9_.-]+", username):
        return "CHARACTERS"
    if sum(username.count(c) for c in SPECIALS) > 1:
        return "ONE_SPECIAL_MAXIMUM"
    return None


@dataclass(frozen=True)
class Filters:
    min_length: int = 3
    max_length: int = 5
    letters: bool = True
    digits: bool = True
    specials: str = ""
    prefix: str = ""
    suffix: str = ""
    required: str = ""
    repeat_target: str = ""
    repeat_count: int = 2
    repeat_exact: bool = True
    adjacency: str = "any"

    def validate(self) -> None:
        if not 3 <= self.min_length <= self.max_length <= 15:
            raise ValueError("الطول يجب أن يكون بين 3 و15.")
        if len(set(self.specials)) != len(self.specials) or any(c not in SPECIALS for c in self.specials):
            raise ValueError("العلامات المسموحة: _ . -")
        if any(c not in LETTERS + DIGITS + SPECIALS for c in self.prefix + self.suffix + self.required):
            raise ValueError("شروط النص تقبل أحرف ASCII الصغيرة والأرقام والعلامات فقط.")
        if (
            self.repeat_target
            and self.repeat_target != "any"
            and (len(self.repeat_target) != 1 or self.repeat_target not in LETTERS + DIGITS)
        ):
            raise ValueError("رمز التكرار حرف أو رقم واحد، أو any.")
        if not 2 <= self.repeat_count <= 15 or self.adjacency not in {"any", "adjacent", "separated"}:
            raise ValueError("شرط التكرار غير صالح.")

    def matches(self, name: str) -> bool:
        if syntax_error(name) or not self.min_length <= len(name) <= self.max_length:
            return False
        body_allowed = (LETTERS if self.letters else "") + (DIGITS if self.digits else "") + self.specials
        if any(c not in body_allowed for c in name[1:]):
            return False
        if not name.startswith(self.prefix) or not name.endswith(self.suffix) or self.required not in name:
            return False
        if not self.repeat_target:
            return True
        targets = set(name) & set(LETTERS + DIGITS) if self.repeat_target == "any" else {self.repeat_target}
        for target in targets:
            count = name.count(target)
            if (count != self.repeat_count) if self.repeat_exact else (count < self.repeat_count):
                continue
            if self.adjacency == "separated" and target * 2 in name:
                continue
            if self.adjacency == "adjacent" and target * self.repeat_count not in name:
                continue
            return True
        return False


@dataclass(frozen=True)
class SearchConfig:
    filters: Filters = field(default_factory=Filters)
    source: str = "generator"
    generation_mode: str = "all"
    categories: tuple[str, ...] = ("english_words", "english_names", "arabic_names_latin", "gaming_terms")
    files: tuple[str, ...] = ()
    limit: int | None = 1000
    workers: int = 5
    auto_workers: bool = True
    queue_size: int = 128
    smart_history: bool = True
    history_ttl_hours: float = 24.0
    max_retries: int = 2
    retry_budget: int = 100
    timeout_seconds: float = 10.0
    circuit_threshold: int = 5
    provider: str = "mock"

    def validate(self) -> None:
        self.filters.validate()
        if self.source not in {"generator", "txt"} or self.generation_mode not in {
            "all",
            "meaningful",
            "mixed",
        }:
            raise ValueError("مصدر البحث أو نمط التوليد غير صالح.")
        if self.source == "txt" and not self.files:
            raise ValueError("اختر ملف TXT واحدًا على الأقل.")
        if not 1 <= self.workers <= 1000 or not 1 <= self.queue_size <= 10000:
            raise ValueError("التزامن 1–1000، والطابور 1–10000.")
        if self.limit is not None and self.limit < 1:
            raise ValueError("حد الأسماء يجب أن يكون موجبًا.")
        if self.history_ttl_hours < 0 or not 0 <= self.max_retries <= 10 or self.retry_budget < 0:
            raise ValueError("سياسة السجل أو إعادة المحاولة غير صالحة.")
        if self.timeout_seconds <= 0 or self.circuit_threshold < 1 or self.provider not in {"mock", "real"}:
            raise ValueError("إعدادات الموفر غير صالحة.")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["limit"] = str(self.limit) if self.limit is not None else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> SearchConfig:
        data = dict(data)
        data["filters"] = Filters(**data["filters"])
        data["files"] = tuple(data.get("files", ()))
        data["categories"] = tuple(data.get("categories", ()))
        if data.get("limit") is not None:
            data["limit"] = int(data["limit"])
        result = cls(**data)
        result.validate()
        return result


@dataclass(frozen=True)
class Evidence:
    classification: Classification
    kind: str
    provider: str
    provider_version: str
    mode: str
    trustworthy: bool = False
    checked_at: str = field(default_factory=utc_now)
    latency_ms: float = 0
    error: str = ""
    trace_id: str = field(default_factory=lambda: uuid4().hex)

    def validate(self) -> None:
        if self.mode not in {"real", "simulated"}:
            raise ValueError("Invalid evidence mode")
        required = {
            Classification.AVAILABLE: {"explicitly_available"},
            Classification.TAKEN: {"explicitly_taken", "account_exists"},
            Classification.RESERVED: {"explicitly_reserved"},
            Classification.INVALID: {"syntax_invalid", "explicitly_invalid"},
        }
        if self.classification in required and self.kind not in required[self.classification]:
            raise ValueError("Classification lacks positive evidence")
        if self.classification == Classification.UNKNOWN and self.trustworthy:
            raise ValueError("Unknown is never trustworthy")
        if not self.provider or not self.provider_version:
            raise ValueError("Provider identity is required")
