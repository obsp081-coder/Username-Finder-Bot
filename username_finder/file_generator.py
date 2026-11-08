"""Exact finite grammars and crash-recoverable offline TXT/CSV generation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from functools import cache
from pathlib import Path

from .file_generator_rules import DIGITS, LOWER, UPPER, Grammar

STATE_BUDGET = 200_000
RECORD_LIMIT = 256


def category(char, requirement):
    return requirement == "any" or char in {
        "letter": LOWER + UPPER,
        "alnum": LOWER + UPPER + DIGITS,
        "digit": DIGITS,
    }.get(requirement, "")


class LengthSpace:
    def __init__(self, rule, length, cancel=None):
        self.rule, self.length = rule, length
        self.cancel = cancel
        specials = {c: (limit, places) for c, limit, places in rule.specials}
        self.tracked = tuple(c for c, (limit, _) in specials.items() if limit < length)
        self.limits = tuple(specials[c][0] for c in self.tracked)
        self.alphabet = "".join(dict.fromkeys(rule.alphabet + "".join(specials)))
        self.options = []
        self.states = 0
        for pos in range(length):
            chars = []
            for c in self.alphabet:
                if pos == 0 and not category(c, rule.first):
                    continue
                if pos == length - 1 and not category(c, rule.last):
                    continue
                if c in specials:
                    _, places = specials[c]
                    # A one-character string occupies both boundary positions.
                    required = ("s" if pos == 0 else "") + ("e" if pos == length - 1 else "")
                    required = required or "b"
                    if not set(required) <= set(places):
                        continue
                fixed = []
                if pos < len(rule.prefix):
                    fixed.append(rule.prefix[pos])
                tail = pos - length + len(rule.suffix)
                if tail >= 0:
                    fixed.append(rule.suffix[tail])
                if all(c == value for value in fixed):
                    chars.append(c)
            if max(len(rule.prefix), len(rule.suffix)) > length:
                chars = []
            self.options.append(tuple(chars))
        self.count = cache(self._count)

    def advance(self, char, counts, previous):
        if char in self.rule.no_repeat and previous == char:
            return None
        if char in self.tracked:
            index = self.tracked.index(char)
            counts = tuple(n + (i == index) for i, n in enumerate(counts))
            if counts[index] > self.limits[index]:
                return None
        return counts, char if char in self.rule.no_repeat else ""

    def _count(self, pos, counts, previous):
        if self.cancel and self.cancel.is_set():
            raise ValueError("أُلغي حساب الاحتمالات؛ لم يبدأ التوليد.")
        self.states += 1
        if self.states > STATE_BUDGET:
            raise ValueError("تعقيد القواعد يستهلك ذاكرة كبيرة؛ قلل قيود تكرار الرموز أو مجال الطول.")
        if pos == self.length:
            return 1
        total = 0
        for char in self.options[pos]:
            state = self.advance(char, counts, previous)
            if state is not None:
                total += self.count(pos + 1, *state)
        return total

    @property
    def total(self):
        return self.count(0, (0,) * len(self.tracked), "")

    def iter_from(self, rank=0):
        def walk(pos, counts, previous, prefix, skip):
            if pos == self.length:
                yield prefix
                return
            for char in self.options[pos]:
                state = self.advance(char, counts, previous)
                if state is None:
                    continue
                size = self.count(pos + 1, *state)
                if skip >= size:
                    skip -= size
                    continue
                yield from walk(pos + 1, *state, prefix + char, skip)
                skip = 0

        if not 0 <= rank <= self.total:
            raise ValueError("Invalid enumeration offset")
        if rank < self.total:
            yield from walk(0, (0,) * len(self.tracked), "", "", rank)


class GrammarSpace:
    def __init__(self, rule, cancel=None):
        if not 1 <= rule.minimum <= rule.maximum <= RECORD_LIMIT:
            raise ValueError(f"حد أمان المحرف في السجل {RECORD_LIMIT}؛ ليس حدًا لعدد النتائج.")
        chars = rule.alphabet + "".join(row[0] for row in rule.specials)
        if not chars or set(chars) - set(LOWER + UPPER + DIGITS + "_.-"):
            raise ValueError("اختر مجموعة ASCII المدعومة: حروف، أرقام، _ . - فقط.")
        special_chars = [row[0] for row in rule.specials]
        if len(special_chars) != len(set(special_chars)) or set(rule.alphabet) & set(special_chars):
            raise ValueError("Special characters must have one independent rule")
        for char, limit, positions in rule.specials:
            if len(char) != 1 or char not in "_.-" or type(limit) is not int or limit < 0:
                raise ValueError("Invalid special-character rule")
            if set(positions) - set("sbe"):
                raise ValueError("Invalid special-character positions")
        if set(rule.prefix + rule.suffix) - set(chars):
            raise ValueError("البادئة أو اللاحقة تتضمن محرفًا غير محدد.")
        if rule.first not in {"any", "letter", "alnum", "digit"} or rule.last not in {
            "any",
            "letter",
            "alnum",
            "digit",
        }:
            raise ValueError("Invalid boundary requirement")
        self.rule = rule
        self.spaces = [LengthSpace(rule, n, cancel) for n in range(rule.minimum, rule.maximum + 1)]
        self.total = 0
        states = 0
        for space in self.spaces:
            self.total += space.total
            states += space.states
            if states > STATE_BUDGET:
                raise ValueError("قواعد العد تتجاوز ميزانية الذاكرة؛ قلل تعقيد القواعد أو مجال الطول.")
        self.characters = sum(space.total * space.length for space in self.spaces)

    def iter_from(self, rank=0):
        if not 0 <= rank <= self.total:
            raise ValueError("Invalid enumeration offset")
        for space in self.spaces:
            if rank >= space.total:
                rank -= space.total
                continue
            yield from space.iter_from(rank)
            rank = 0

    def prefix_characters(self, count):
        result = 0
        for space in self.spaces:
            taken = min(count, space.total)
            result += taken * space.length
            count -= taken
        return result


class IdentifierSpace:
    def __init__(self, config, cancel=None):
        self.config = config
        self.primary = GrammarSpace(Grammar.from_data(config["primary"]), cancel)
        self.secondary = (
            GrammarSpace(Grammar.from_data(config["secondary"]), cancel) if config.get("secondary") else None
        )
        self.total = self.primary.total * (self.secondary.total if self.secondary else 1)
        self.characters = self.primary.characters
        if self.secondary:
            self.characters = (
                self.primary.characters * self.secondary.total
                + self.secondary.characters * self.primary.total
                + self.total
            )
        self.csv = config.get("format", "txt") == "csv"
        if config.get("format", "txt") not in {"txt", "csv"}:
            raise ValueError("Unsupported output format")
        self.header = b"identifier\n" if self.csv else b""
        self.bytes = self.characters + self.total + len(self.header)

    def iter_from(self, index=0):
        if not self.secondary:
            yield from self.primary.iter_from(index)
        elif self.secondary.total:
            primary, secondary = divmod(index, self.secondary.total)
            for name in self.primary.iter_from(primary):
                for tag in self.secondary.iter_from(secondary):
                    yield name + "#" + tag
                secondary = 0

    def offset(self, index):
        if not 0 <= index <= self.total:
            raise ValueError("Invalid checkpoint index")
        if not self.secondary:
            return len(self.header) + self.primary.prefix_characters(index) + index
        full, tail = divmod(index, self.secondary.total)
        size = (
            len(self.header)
            + self.primary.prefix_characters(full) * self.secondary.total
            + full * self.secondary.characters
            + 2 * full * self.secondary.total
        )
        if tail:
            length = len(next(self.primary.iter_from(full)))
            size += (length + 2) * tail + self.secondary.prefix_characters(tail)
        return size


def paths(destination):
    path = Path(destination).resolve()
    return path, path.with_name(path.name + ".part"), path.with_name(path.name + ".generation.json")


def save_journal(path, record):
    temporary = path.with_name(path.name + ".tmp")
    # The temporary is owned by this generator; the last committed journal remains valid on failure.
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(record, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_journal(destination):
    target, _, journal = paths(destination)
    if journal.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid generation journal size")
    record = json.loads(journal.read_text(encoding="utf-8"))
    if (
        not isinstance(record, dict)
        or record.get("kind") != "username-file-generator-v1"
        or record.get("destination") != str(target)
    ):
        raise ValueError("ملف الاستئناف لا يطابق الوجهة.")
    if (
        not isinstance(record.get("config"), dict)
        or type(record.get("index")) is not int
        or type(record.get("offset")) is not int
        or not isinstance(record.get("sha256"), str)
    ):
        raise ValueError("بيانات الاستئناف غير مكتملة؛ لم يُعدّل الملف.")
    return record


def resource_check(destination, required):
    directory = Path(destination).resolve().parent
    if required + 1024 * 1024 > shutil.disk_usage(directory).free:
        raise ValueError(f"المساحة الحرة غير كافية؛ حجم الناتج المحسوب {required:,} بايت، مع احتياطي 1 MiB.")
    if os.name == "nt":
        import ctypes

        root, filesystem = ctypes.create_unicode_buffer(1024), ctypes.create_unicode_buffer(256)
        api = ctypes.windll.kernel32
        if (
            api.GetVolumePathNameW(str(directory), root, len(root))
            and api.GetVolumeInformationW(root, None, 0, None, None, None, filesystem, len(filesystem))
            and filesystem.value.upper() == "FAT32"
            and required >= 2**32
        ):
            raise ValueError("حجم الناتج يتجاوز حد الملف الواحد في FAT32؛ اختر نظام ملفات مناسبًا.")


def generate_file(config, destination, *, cancel=None, notify=lambda _: None, resume=False):
    cancel = cancel or threading.Event()
    space = IdentifierSpace(config, cancel)
    if not space.total:
        raise ValueError("لا توجد احتمالات تحقق هذه القواعد.")
    target, partial, journal = paths(destination)
    digest = hashlib.sha256()
    if resume:
        record = read_journal(target)
        if (
            json.dumps(record["config"], sort_keys=True) != json.dumps(config, sort_keys=True)
            or not 0 <= record["index"] <= space.total
        ):
            raise ValueError("تغيرت القواعد؛ لا يمكن استئناف الملف بهذه الإعدادات.")
        if record["offset"] != space.offset(record["index"]) or record.get("total") != space.total:
            raise ValueError("مؤشر الاستئناف لا يطابق حجم البيانات؛ لم يُعدّل الملف.")
        source = target if target.exists() else partial
        with source.open("rb") as stream:
            remaining = record["offset"]
            if not 0 <= remaining <= space.bytes or remaining > source.stat().st_size:
                raise ValueError("Invalid checkpoint offset")
            while remaining:
                if cancel.is_set():
                    raise ValueError("أُلغي التحقق من ملف الاستئناف؛ لم يُعدّل الملف.")
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("Incomplete checkpoint")
                digest.update(block)
                remaining -= len(block)
        if digest.hexdigest() != record["sha256"]:
            raise ValueError("تغير محتوى الملف المحفوظ؛ أُوقف الاستئناف لحماية البيانات.")
        if target.exists():
            if record["index"] != space.total or target.stat().st_size != record["offset"]:
                raise ValueError("الوجهة موجودة؛ لن تُستبدل.")
            record["state"] = "Completed"
            save_journal(journal, record)
            return record
    else:
        if any(p.exists() for p in (target, partial, journal, journal.with_name(journal.name + ".tmp"))):
            raise ValueError("الوجهة أو ملف الاستئناف موجود؛ اختر استئنافًا صريحًا أو وجهة جديدة.")
        resource_check(target, space.bytes)
        record = {
            "kind": "username-file-generator-v1",
            "destination": str(target),
            "config": config,
            "index": 0,
            "offset": len(space.header),
            "sha256": "",
            "state": "Paused",
            "total": space.total,
        }
        with partial.open("xb") as stream:
            stream.write(space.header)
            stream.flush()
            os.fsync(stream.fileno())
        digest.update(space.header)
        record["sha256"] = digest.hexdigest()
        save_journal(journal, record)
    resource_check(target, space.bytes - record["offset"])
    with partial.open("r+b") as stream:
        stream.seek(record["offset"])
        stream.truncate()
        batch = bytearray()
        index = record["index"]

        def checkpoint():
            stream.write(batch)
            stream.flush()
            os.fsync(stream.fileno())
            digest.update(batch)
            record.update(index=index, offset=stream.tell(), sha256=digest.hexdigest(), state="Paused")
            save_journal(journal, record)
            batch.clear()
            notify(dict(record))

        for name in space.iter_from(index):
            if cancel.is_set():
                break
            batch.extend((name + "\n").encode("ascii"))
            index += 1
            if len(batch) >= 64 * 1024:
                checkpoint()
        if batch:
            checkpoint()
    if record["index"] == space.total:
        # Final destination must not overwrite a concurrently created file.
        if os.name == "nt":
            os.rename(partial, target)
        else:
            os.link(partial, target)
        record["state"] = "Completed"
        save_journal(journal, record)
        # On POSIX the owned .part hard link retains crash recovery without replacing a file.
    notify(dict(record))
    return record
