"""Roblox-only local preparation and preliminary API evidence."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import math
import re
import secrets
import sqlite3
import string
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import roblox_protocol as protocol
from .generation_ranges import allocations, interleave

INTERVAL_SECONDS = 5.0
MAX_RECORD_BYTES = 1024 * 1024
RULES_SOURCE = "https://en.help.roblox.com/hc/en-us/articles/4412614080532-Xbox-Error-901"
LABELS = {
    "Available": "متاح مبدئيًا (Available)",
    "Taken": "مستخدم (Taken)",
    "Reserved": "محجوز (Reserved)",
    "Invalid": "غير صالح (Invalid)",
    "Unknown": "غير محسوم (Unknown)",
}


def now():
    return datetime.now(UTC).isoformat()


def syntax_error(name):
    # Local format only; moderation and registration eligibility remain server-side.
    if not 3 <= len(name) <= 20:
        return "الطول المطلوب من 3 إلى 20 محرفًا."
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        return "المسموح حروف لاتينية وأرقام وشرطة سفلية فقط."
    if name.startswith("_") or name.endswith("_") or name.count("_") > 1:
        return "يسمح بشرطة سفلية واحدة داخل الاسم فقط."
    return None


def csv_usernames(text):
    return list(csv_rows(io.StringIO(text, newline="")))


def csv_rows(stream):
    try:
        reader = csv.reader(stream, strict=True)
        first = next((row for row in reader if any(cell.strip() for cell in row)), None)
        if first is None:
            return
        keys = [cell.strip().casefold() for cell in first]
        columns = [
            i for i, key in enumerate(keys) if key in {"username", "user_name", "identifier", "اسم المستخدم"}
        ]
        if len(columns) > 1:
            raise ValueError("ملف CSV يحتوي أعمدة أسماء مكررة.")
        if columns:
            column = columns[0]
        elif len(first) == 1:
            column = 0
            yield first[0]
        else:
            raise ValueError("ملف CSV متعدد الأعمدة يحتاج عمود username واضحًا.")
        for line, row in enumerate(reader, 2):
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) != len(first):
                raise ValueError(f"عدد أعمدة CSV غير متسق عند السطر {line}.")
            yield row[column]
    except csv.Error as exc:
        raise ValueError("تعذر قراءة CSV؛ تحقق من الاقتباسات والأعمدة.") from exc


@dataclass(frozen=True)
class Prepared:
    names: tuple[str, ...]
    duplicates: int
    invalid: tuple[tuple[str, str], ...]
    blank: int
    total: int = 0
    skipped: int = 0
    invalid_count: int = 0
    complete: bool = True
    scanned: int = 0


def bounded_lines(stream):
    """Bound individual physical records, not total input size."""
    while line := stream.readline(MAX_RECORD_BYTES + 1):
        if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError("السطر يتجاوز حد الأمان 1 MiB؛ لا يوجد حد لحجم الملف الكامل.")
        if "\x00" in line:
            raise ValueError("الملف يحتوي محارف NUL.")
        yield line


def input_spec(manual="", files=()):
    paths = []
    for filename in dict.fromkeys(str(Path(p).resolve()) for p in files):
        path = Path(filename)
        try:
            stat = path.stat()
        except OSError as exc:
            raise ValueError("تعذر الوصول إلى ملف الإدخال؛ تحقق من وجوده وصلاحية قراءته.") from exc
        paths.append({"path": filename, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    return {"manual": manual, "files": paths}


def input_values(spec):
    if spec.get("generation"):
        config = spec["generation"]
        if "length_specs" in config:
            yield from interleave(generated_names(**part) for part in config["length_specs"])
        else:
            yield from generated_names(**config)
        return
    yield from bounded_lines(io.StringIO(spec.get("manual", "")))
    for record in spec.get("files", []):
        path = Path(record["path"])
        try:
            stat = path.stat()
        except OSError as exc:
            raise ValueError("تعذر الوصول إلى ملف الإدخال؛ حُفظت الدفعات السابقة ولم يبدأ الفحص.") from exc
        if (stat.st_size, stat.st_mtime_ns) != (record["size"], record["mtime_ns"]):
            raise ValueError("تغير ملف الإدخال؛ أعد اختياره لعملية جديدة.")
        if path.suffix.lower() not in {".txt", ".csv"}:
            raise ValueError("اختر ملف TXT أو CSV.")
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                lines = bounded_lines(stream)
                yield from csv_rows(lines) if path.suffix.lower() == ".csv" else lines
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != (record["size"], record["mtime_ns"]):
                raise ValueError("تغير ملف الإدخال أثناء القراءة؛ لا يبدأ الفحص.")
        except (OSError, UnicodeError) as exc:
            raise ValueError("تعذر قراءة الملف؛ يلزم ملف UTF-8 صالح ومتاح.") from exc


def prepare_inputs(manual="", files=(), *, spec=None, cancel=None, known=lambda _: False, preview=False):
    """Exact streaming counts by default; GUI previews stop at a bounded local sample."""
    spec = spec or input_spec(manual, files)
    names, invalid = [], []
    duplicates = blank = total = skipped = invalid_count = 0
    scanned, scanned_bytes, complete = 0, 0, True
    with closing(sqlite3.connect("")) as seen, closing(input_values(spec)) as values:
        seen.execute("CREATE TABLE seen (name TEXT PRIMARY KEY)")
        for value in values:
            if cancel and cancel.is_set():
                raise ValueError("أُلغيت المعاينة؛ لم يبدأ الفحص.")
            scanned += 1
            if preview:
                scanned_bytes += len(value.encode("utf-8"))
            name = value.strip()
            if not name:
                blank += 1
            elif not seen.execute("INSERT OR IGNORE INTO seen VALUES(?)", (name.casefold(),)).rowcount:
                duplicates += 1
            else:
                error = syntax_error(name)
                if error:
                    invalid_count += 1
                    if len(invalid) < 5:
                        invalid.append((name[:80], error))
                else:
                    total += 1
                    skipped += bool(known(name.casefold()))
                    if len(names) < 20:
                        names.append(name)
            if preview and (len(names) >= 20 or scanned >= 2000 or scanned_bytes >= 64 * 1024):
                complete = False
                break
    if preview:
        # A bounded preview does not exhaust input_values' final source check.
        for record in spec.get("files", []):
            try:
                stat = Path(record["path"]).stat()
            except OSError as exc:
                raise ValueError("تعذر الوصول إلى ملف الإدخال؛ لم تُستورد محتوياته.") from exc
            if (stat.st_size, stat.st_mtime_ns) != (record["size"], record["mtime_ns"]):
                raise ValueError("تغير ملف الإدخال أثناء المعاينة؛ أعد اختياره.")
    return Prepared(
        tuple(names), duplicates, tuple(invalid), blank, total, skipped, invalid_count, complete, scanned
    )


def generation_spec(length, quantity):
    if type(length) is not int or type(quantity) is not int or not 3 <= length <= 20:
        raise ValueError("اختر طولًا من 3 إلى 20.")
    total = 26 * 36 ** (length - 1)
    if not 1 <= quantity <= total:
        raise ValueError(f"الكمية تتجاوز المساحة الفريدة المتاحة: {total:,}.")
    # A permutation of ranks gives unique candidates without collision/retry loops.
    multiplier = secrets.randbelow(total - 1) + 1
    while math.gcd(multiplier, total) != 1:
        multiplier = (multiplier % (total - 1)) + 1
    offset = secrets.randbelow(total)
    return {"length": length, "quantity": quantity, "multiplier": multiplier, "offset": offset}


def generation_range_spec(minimum, maximum, quantity):
    counts = allocations(
        minimum, maximum, quantity, lower=3, upper=20, capacity=lambda length: 26 * 36 ** (length - 1)
    )
    if minimum == maximum:
        return generation_spec(minimum, quantity)
    return {
        "minimum": minimum,
        "maximum": maximum,
        "quantity": quantity,
        "length_specs": [generation_spec(length, count) for length, count in counts.items()],
    }


def generated_names(length, quantity, multiplier, offset):
    total = 26 * 36 ** (length - 1)
    alphabet = string.ascii_lowercase + string.digits
    for index in range(quantity):
        rank = (multiplier * index + offset) % total
        tail = []
        for _ in range(length - 1):
            rank, digit = divmod(rank, 36)
            tail.append(alphabet[digit])
        yield string.ascii_lowercase[rank] + "".join(reversed(tail))


def generate(length, quantity):
    """Lazy candidates; the operation persists the random permutation parameters."""
    return generated_names(**generation_spec(length, quantity))


def result_for(username, response, *, checked_at=None):
    classification, reason, code, message = protocol.classify(response)
    if "retry-after" in response.headers and classification != "Unknown":
        classification, reason = "Unknown", "retry_after_header"
    return {
        "username": username,
        "classification": classification,
        "api_code": code,
        "message": message,
        "reason": reason,
        "checked_at": checked_at or now(),
        "http_status": response.status,
        "latency_seconds": response.elapsed,
        "error": response.error,
        "headers": response.headers,
        "challenge": response.challenge,
        "raw_complete": response.complete,
        "raw_sha256": hashlib.sha256(response.body).hexdigest(),
        "raw_base64": base64.b64encode(response.body).decode("ascii"),
        "response_data": response.body.decode("utf-8", errors="replace"),
        "request_url": protocol.request_url(username),
        "parameters": {"Username": username, "Birthday": protocol.BIRTHDAY, "Context": protocol.CONTEXT},
        "preliminary": True,
        "independently_verified_classification": "Unknown",
    }


def retry_not_before(value, *, at=None):
    at = at or datetime.now(UTC)
    try:
        seconds = max(INTERVAL_SECONDS, int(value))
        return (at + timedelta(seconds=seconds)).isoformat()
    except (TypeError, ValueError, OverflowError):
        try:
            date = parsedate_to_datetime(value).astimezone(UTC)
            return max(date, at + timedelta(seconds=INTERVAL_SECONDS)).isoformat()
        except (TypeError, ValueError, OverflowError):
            return (at + timedelta(minutes=5)).isoformat()
