from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .domain import SearchConfig, normalize, syntax_error
from .generation import GeneratorSpace

DATA_DIR = Path(__file__).parent / "data"


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preview_txt(
    config: SearchConfig, expected_hashes: dict | None = None, *, max_lines: int = 200, max_bytes: int = 65536
) -> dict:
    """Bounded local preview; it neither creates a source checkpoint nor verifies a username."""
    config.validate()
    counts = dict.fromkeys(("lines", "eligible", "duplicate", "blank", "invalid", "filtered", "malformed"), 0)
    seen = set()
    examples = []
    codes = []
    notes = []
    consumed = 0
    complete = True
    expected_hashes = expected_hashes or {}
    for index, name in enumerate(config.files):
        if index >= 20 or counts["lines"] >= max_lines or consumed >= max_bytes:
            complete = False
            break
        path = Path(name)
        try:
            before = path.stat()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                first_line = True
                while stream.tell() < before.st_size:
                    if counts["lines"] >= max_lines or consumed >= max_bytes:
                        complete = False
                        break
                    remaining = max_bytes - consumed
                    raw = stream.readline(remaining + 1)
                    consumed += len(raw)
                    if len(raw) > remaining:
                        complete = False
                        break
                    if not raw:
                        break
                    digest.update(raw)
                    counts["lines"] += 1
                    try:
                        text = raw.decode("utf-8-sig" if first_line else "utf-8")
                    except UnicodeDecodeError:
                        counts["malformed"] += 1
                        codes.append({"file": index + 1, "code": "UNSUPPORTED_ENCODING"})
                        first_line = False
                        continue
                    first_line = False
                    username = normalize(text)
                    if not username:
                        counts["blank"] += 1
                    elif syntax_error(username):
                        counts["invalid"] += 1
                    elif not config.filters.matches(username):
                        counts["filtered"] += 1
                    elif username in seen:
                        counts["duplicate"] += 1
                    else:
                        seen.add(username)
                        counts["eligible"] += 1
                        if len(examples) < 5:
                            examples.append(username)
                file_complete = stream.tell() == before.st_size and consumed <= max_bytes
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                codes.append({"file": index + 1, "code": "FILE_CHANGED"})
                notes.append("تغير أحد الملفات أثناء المعاينة؛ أعد اختيار الملف ثم حاول مجددًا.")
            elif str(path.resolve()) in expected_hashes and file_complete:
                expected = expected_hashes[str(path.resolve())]
                if not expected or digest.hexdigest() != expected:
                    codes.append({"file": index + 1, "code": "PRESET_FILE_CHANGED"})
                    notes.append("تغير محتوى ملف مرتبط بالقالب؛ أعد اختياره قبل إنشاء البحث.")
            if not file_complete:
                complete = False
        except OSError as exc:
            complete = False
            codes.append({"file": index + 1, "code": type(exc).__name__})
            notes.append("أحد ملفات TXT غير موجود أو لا يمكن قراءته؛ أعد اختياره وتحقق من صلاحية الوصول.")
    if counts["malformed"]:
        notes.append("توجد أسطر بترميز غير مدعوم؛ احفظ الملف بترميز UTF-8 ثم أعد المعاينة.")
    errors = bool(codes)
    if complete and not errors:
        if counts["eligible"]:
            message = f"معاينة TXT: {counts['eligible']:,} أسماء فريدة مطابقة من {counts['lines']:,} أسطر."
        elif counts["lines"] == counts["blank"]:
            message = "الملفات المقروءة فارغة أو تحتوي أسطرًا فارغة فقط؛ لا توجد أسماء للمعاينة."
        else:
            message = (
                "لا توجد أسماء مطابقة للشروط في الملفات المقروءة؛ راجع الطول والبداية والنهاية وشروط التكرار."
            )
    else:
        message = f"معاينة جزئية: {counts['eligible']:,} أسماء مطابقة في الجزء المقروء."
        if not counts["eligible"]:
            message += " لا يمكن الجزم بأن بقية الملفات خالية من الأسماء المطابقة."
        message += " العدد الفريد الكلي غير معلوم قبل قراءة الملفات كاملة."
    if examples:
        message += "\nأمثلة: " + "، ".join(examples)
    if notes:
        message += "\n" + "\n".join(dict.fromkeys(notes))
    message += "\nهذه معاينة للصيغة والفلاتر قبل تخطي السجل؛ لا تتضمن فحص Snapchat."
    if not complete and expected_hashes:
        message += " تُفحص بصمات الملفات كاملة عند إنشاء البحث."
    return {
        "message": message,
        "level": "warning" if errors or not complete else "success",
        "diagnostics": {"counts": counts, "complete": complete, "bytes_read": consumed, "codes": codes},
        "examples": examples,
    }


class MeaningLibrary:
    def __init__(self, directory: Path = DATA_DIR):
        self.directory = directory
        self.manifest_path = directory / "manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def entries(self, categories: tuple[str, ...]):
        available = {p["category"] for p in self.manifest["packages"]}
        if not categories or not set(categories) <= available:
            raise ValueError("اختر فئة معانٍ متوفرة واحدة على الأقل.")
        merged: dict[str, dict] = {}
        for package in self.manifest["packages"]:
            if package["category"] not in categories:
                continue
            path = self.directory / package["file"]
            if fingerprint(path) != package["sha256"]:
                raise ValueError(f"مكتبة تالفة: {package['category']}")
            if not all(package.get(key) for key in ("version", "source_url", "license", "extracted_at")):
                raise ValueError("بيانات مصدر المكتبة ناقصة.")
            items = json.loads(path.read_text(encoding="utf-8"))
            if len(items) != package["count"] or not items:
                raise ValueError("مكتبة المعاني فارغة أو عدد عناصرها غير متطابق.")
            for item in items:
                name = item["name"]
                if syntax_error(name) or name != normalize(name) or not item.get("reference"):
                    raise ValueError("عنصر مكتبة غير صالح أو غير موثق.")
                meta = merged.setdefault(name, {"categories": [], "sources": [], "original": name})
                meta["categories"].append(package["category"])
                meta["sources"].append(package["id"])
        return list(sorted(merged.items()))

    def snapshot(self, categories: tuple[str, ...]) -> dict:
        # Validate selected content before recording its identity.
        self.entries(categories)
        return {
            "manifest_sha256": fingerprint(self.manifest_path),
            "packages": [
                {"id": p["id"], "sha256": p["sha256"]}
                for p in self.manifest["packages"]
                if p["category"] in categories
            ],
        }


def make_snapshot(config: SearchConfig, library: MeaningLibrary | None = None) -> dict:
    snapshot: dict = {"rules": "source-v1"}
    if config.source == "txt":
        snapshot["files"] = []
        for name in config.files:
            path = Path(name).resolve(strict=True)
            before = path.stat()
            digest = fingerprint(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("تغير ملف TXT أثناء تجهيز البحث.")
            snapshot["files"].append({"path": str(path), "sha256": digest, "size": after.st_size})
    elif config.generation_mode != "all":
        snapshot["library"] = (library or MeaningLibrary()).snapshot(config.categories)
    return snapshot


@dataclass(frozen=True)
class InputRecord:
    username: str
    cursor: dict
    kind: str = "candidate"
    metadata: dict | None = None


class CandidateSource:
    """One bounded record at a time. Cursor denotes the *next* unread record."""

    def __init__(
        self,
        config: SearchConfig,
        snapshot: dict,
        cursor: dict | None = None,
        library: MeaningLibrary | None = None,
    ):
        self.config = config
        self.snapshot = snapshot
        if make_snapshot(config, library) != snapshot:
            raise ValueError("تغيرت ملفات TXT أو مكتبة المعاني؛ الاستكمال ممنوع. ابدأ بحثًا جديدًا.")
        self.file_signatures = {}
        for file in snapshot.get("files", []):
            stat = Path(file["path"]).stat()
            self.file_signatures[file["path"]] = (stat.st_size, stat.st_mtime_ns)
        self.cursor = cursor or {
            "stage": "txt" if config.source == "txt" else "meaningful",
            "rank": "0",
            "file": 0,
            "offset": 0,
            "line": 0,
        }
        self.stream: BinaryIO | None = None
        self.space = None if config.source == "txt" else GeneratorSpace(config.filters)
        self.meaningful = []
        if config.source == "generator" and config.generation_mode != "all":
            self.meaningful = (library or MeaningLibrary()).entries(config.categories)

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def next_record(self) -> InputRecord | None:
        c = dict(self.cursor)
        if self.config.source == "txt":
            while c["file"] < len(self.snapshot["files"]):
                path = self.snapshot["files"][c["file"]]["path"]
                stat = Path(path).stat()
                if self.file_signatures[path] != (stat.st_size, stat.st_mtime_ns):
                    raise ValueError("تغير ملف TXT أثناء التشغيل؛ أُوقف البحث لحماية الاستكمال.")
                if self.stream is None:
                    self.stream = open(self.snapshot["files"][c["file"]]["path"], "rb")
                    self.stream.seek(c["offset"])
                raw = self.stream.readline(65537)
                if not raw:
                    self.close()
                    c.update(file=c["file"] + 1, offset=0, line=0)
                    continue
                too_long = len(raw) > 65536
                while too_long and raw and not raw.endswith(b"\n"):
                    raw = self.stream.readline(65537)
                offset = self.stream.tell()
                first = c["offset"] == 0
                c.update(offset=offset, line=c["line"] + 1)
                try:
                    text = raw.decode("utf-8-sig" if first else "utf-8") if not too_long else ""
                except UnicodeDecodeError:
                    return self._record("", c, "malformed")
                name = normalize(text)
                return self._record(name, c, "malformed" if too_long else ("candidate" if name else "blank"))
            self.cursor = c
            return None
        if c["stage"] == "meaningful":
            rank = int(c["rank"])
            if self.config.generation_mode != "all" and rank < len(self.meaningful):
                name, meta = self.meaningful[rank]
                c["rank"] = str(rank + 1)
                return self._record(name, c, metadata=meta)
            if self.config.generation_mode == "meaningful":
                return None
            c.update(stage="all", rank="0")
        rank = int(c["rank"])
        if rank >= self.space.total:
            return None
        name = self.space.unrank(rank)
        c["rank"] = str(rank + 1)
        return self._record(name, c)

    def _record(self, name: str, cursor: dict, kind: str = "candidate", metadata=None) -> InputRecord:
        self.cursor = cursor
        return InputRecord(name, cursor, kind, metadata)
