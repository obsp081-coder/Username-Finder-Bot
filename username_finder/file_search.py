"""Bounded, read-only UTF-8 literal search and on-demand source-line access."""

from __future__ import annotations

import codecs
import os
import stat
import time
from concurrent.futures import CancelledError
from dataclasses import dataclass
from pathlib import Path
from threading import Event

CHUNK_BYTES = 1024 * 1024
MAX_MATCHES = 100_000
MAX_QUERY_BYTES = 65_536
BATCH_SIZE = 128
PAGE_BYTES = 65_536
WHOLE_VIEW_BYTES = 262_144
MAX_COPY_BYTES = 4 * 1024 * 1024

ERRORS = {
    "encoding": "الملف ليس نص UTF-8 صالحًا، أو يحتوي بيانات ثنائية. حوّل نسخة منه إلى UTF-8 ثم أعد البحث.",
    "changed": "تغيّر ملف المصدر منذ البحث. أعد البحث لعرض نتائج موثوقة.",
    "access": "تعذر قراءة الملف؛ قد يكون محذوفًا أو منقولًا أو لا تملك صلاحية الوصول إليه.",
    "not_file": "اختر ملفًا عاديًا قابلًا للقراءة، وليس مجلدًا أو جهازًا.",
    "query": "أدخل نصًا حرفيًا غير فارغ ضمن سطر واحد، بطول لا يتجاوز 65536 بايت UTF-8.",
    "limit": "أدخل حدًا صحيحًا موجبًا من 1 إلى 100000 نتيجة لحماية الذاكرة.",
    "copy_size": "السطر أكبر من حد النسخ الآمن (4 MiB). محتواه كاملًا متاح عبر صفحات العارض دون اقتطاع صامت.",
    "unexpected": "تعذر إكمال العملية المحلية بسبب خطأ غير متوقع؛ أعد المحاولة.",
}


class SearchFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(ERRORS[code])


def check_cancel(cancel):
    if cancel.is_set():
        raise CancelledError


def change_stamp(info):
    # On Windows/Python 3.14, stat and fstat can expose different legacy ctime
    # semantics. Birth time is consistent; size, mtime and file ID still detect edits.
    return info.st_birthtime_ns if os.name == "nt" and hasattr(info, "st_birthtime_ns") else info.st_ctime_ns


@dataclass(frozen=True, slots=True)
class FileInfo:
    path: str
    device: int
    inode: int
    size: int
    modified: int
    changed: int

    @classmethod
    def capture(cls, path):
        try:
            resolved = Path(path).resolve(strict=True)
            info = resolved.stat()
        except (OSError, ValueError, RuntimeError):
            raise SearchFailure("access") from None
        if not stat.S_ISREG(info.st_mode):
            raise SearchFailure("not_file")
        return cls(
            str(resolved), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, change_stamp(info)
        )

    @property
    def key(self):
        return (self.device, self.inode) if self.inode else os.path.normcase(self.path)

    def verify(self, stream):
        info = os.fstat(stream.fileno())
        actual = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, change_stamp(info))
        if actual != (self.device, self.inode, self.size, self.modified, self.changed):
            raise SearchFailure("changed")

    def open(self):
        try:
            stream = open(self.path, "rb", buffering=0)
        except OSError:
            raise SearchFailure("access") from None
        try:
            self.verify(stream)
        except BaseException:
            stream.close()
            raise
        return stream


@dataclass(frozen=True, slots=True)
class Match:
    file: FileInfo
    line: int
    line_start: int
    offset: int

    @property
    def byte_column(self):
        return self.offset - self.line_start + 1


@dataclass(frozen=True, slots=True)
class Progress:
    current_file: str
    files_done: int
    files_total: int
    matches: int
    processed: int
    total_bytes: int
    bytes_read: int
    elapsed: float


@dataclass(frozen=True, slots=True)
class SearchSummary:
    status: str
    progress: Progress
    error_code: str = ""


def validate_search(query, limit):
    try:
        needle = query.encode("utf-8")
    except (UnicodeError, AttributeError):
        raise SearchFailure("query") from None
    if not needle or b"\n" in needle or b"\r" in needle or b"\0" in needle or len(needle) > MAX_QUERY_BYTES:
        raise SearchFailure("query")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_MATCHES:
        raise SearchFailure("limit")
    return needle


def _decode(decoder, data, final=False):
    if b"\0" in data:
        raise SearchFailure("encoding")
    try:
        decoder.decode(data, final=final)
    except UnicodeError:
        raise SearchFailure("encoding") from None


def search_files(
    paths, query, limit=100, *, cancel=None, on_batch=None, on_progress=None, chunk_bytes=CHUNK_BYTES
):
    """Occurrences overlap. Columns are one-based UTF-8 byte positions, offsets zero-based.

    Only UTF-8/LF/CRLF is supported. A UTF-8 signature is skipped as encoding metadata.
    Memory is bounded by the read block, query tail, requested result limit and batch size.
    """
    needle = validate_search(query, limit)
    if chunk_bytes < 4:
        raise ValueError("chunk_bytes must be at least four")
    cancel = cancel if cancel is not None else Event()
    started = time.perf_counter()
    files, seen = [], set()
    done = found = processed = bytes_read = 0
    current = ""
    total = 0
    last_update = 0.0

    def progress(force=False):
        nonlocal last_update
        now = time.perf_counter()
        value = Progress(current, done, len(files), found, processed, total, bytes_read, now - started)
        if on_progress and (force or now - last_update >= 0.1):
            on_progress(value)
            last_update = now
        return value

    try:
        for path in paths:
            check_cancel(cancel)
            current = str(path)
            info = FileInfo.capture(path)
            if info.key not in seen:
                files.append(info)
                seen.add(info.key)
                total += info.size
        for info in files:
            current = info.path
            progress(True)
            with info.open() as stream:
                prefix = stream.read(4)
                bytes_read += len(prefix)
                if prefix.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
                    raise SearchFailure("encoding")
                signature = 3 if prefix.startswith(codecs.BOM_UTF8) else 0
                stream.seek(signature)
                processed += signature
                offset = signature
                line = 1
                line_start = signature
                tail = b""
                decoder = codecs.getincrementaldecoder("utf-8")("strict")
                while True:
                    check_cancel(cancel)
                    chunk = stream.read(chunk_bytes)
                    bytes_read += len(chunk)
                    if not chunk:
                        _decode(decoder, b"", final=True)
                        info.verify(stream)
                        done += 1
                        break
                    data = tail + chunk
                    base = offset - len(tail)
                    cursor = 0
                    staged = []
                    scan_line, scan_start = line, line_start
                    while True:
                        if len(staged) % 256 == 0:
                            check_cancel(cancel)
                        pos = data.find(needle, cursor)
                        if pos < 0:
                            break
                        scan_line += data.count(b"\n", cursor, pos)
                        newline = data.rfind(b"\n", cursor, pos)
                        if newline >= 0:
                            scan_start = base + newline + 1
                        staged.append(Match(info, scan_line, scan_start, base + pos))
                        cursor = pos + 1
                        if found + len(staged) == limit:
                            break
                    reached = found + len(staged) == limit
                    consumed = staged[-1].offset + len(needle) - offset if reached else len(chunk)
                    _decode(decoder, chunk[:consumed], final=reached)
                    info.verify(stream)
                    processed += consumed
                    for index in range(0, len(staged), BATCH_SIZE):
                        check_cancel(cancel)
                        batch = staged[index : index + BATCH_SIZE]
                        if on_batch and on_batch(batch) is False:
                            raise CancelledError
                        found += len(batch)
                    if reached:
                        eof = offset + consumed == info.size
                        done += int(eof)
                        status = "complete" if eof and done == len(files) else "limit"
                        return SearchSummary(status, progress(True))
                    # Keep only incomplete candidate starts. Account line breaks before the tail.
                    keep = min(len(data), len(needle) - 1)
                    boundary = len(data) - keep
                    line += data.count(b"\n", 0, boundary)
                    newline = data.rfind(b"\n", 0, boundary)
                    if newline >= 0:
                        line_start = base + newline + 1
                    tail = data[boundary:]
                    offset += len(chunk)
                    progress()
        return SearchSummary("complete", progress(True))
    except CancelledError:
        return SearchSummary("cancelled", progress(True))
    except SearchFailure as exc:
        return SearchSummary("error", progress(True), exc.code)
    except OSError:
        return SearchSummary("error", progress(True), "access")


@dataclass(frozen=True, slots=True)
class LineInfo:
    match: Match
    end: int

    @property
    def length(self):
        return self.end - self.match.line_start


@dataclass(frozen=True, slots=True)
class LinePage:
    info: LineInfo
    text: str
    start: int
    end: int
    requested: int


def inspect_line(match, cancel):
    """Locate and validate the complete line off the UI thread, without retaining it."""
    try:
        with match.file.open() as stream:
            stream.seek(match.line_start)
            offset = match.line_start
            last = b""
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            while True:
                check_cancel(cancel)
                block = stream.read(CHUNK_BYTES)
                newline = block.find(b"\n")
                part = block[:newline] if newline >= 0 else block
                _decode(decoder, part, final=newline >= 0 or not block)
                offset += len(part)
                if part:
                    last = part[-1:]
                if newline >= 0 or not block:
                    end = offset - int(newline >= 0 and last == b"\r")
                    match.file.verify(stream)
                    return LineInfo(match, end)
    except OSError:
        raise SearchFailure("access") from None


def read_page(info, start, cancel, *, whole=False):
    """Align UTF-8 boundaries and return the exact original text, excluding its EOL."""
    if whole and info.length > MAX_COPY_BYTES:
        raise SearchFailure("copy_size")
    size = info.length if whole or info.length <= WHOLE_VIEW_BYTES else PAGE_BYTES
    relative = min(max(0, start), max(0, info.length - 1))
    if size >= info.length:
        relative = 0
    requested = relative
    try:
        with info.match.file.open() as stream:
            stream.seek(info.match.line_start + relative)
            head = stream.read(min(4, info.length - relative))
            while relative and head and head[0] & 0xC0 == 0x80:
                relative -= 1
                stream.seek(info.match.line_start + relative)
                head = stream.read(1)
            stream.seek(info.match.line_start + relative)
            parts = []
            remaining = min(info.length, requested + size) - relative
            while remaining:
                check_cancel(cancel)
                block = stream.read(min(PAGE_BYTES, remaining))
                if not block:
                    raise SearchFailure("changed")
                parts.append(block)
                remaining -= len(block)
            data = b"".join(parts)
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            try:
                text = decoder.decode(data, final=relative + len(data) == info.length)
            except UnicodeError:
                raise SearchFailure("encoding") from None
            pending = len(decoder.getstate()[0])
            info.match.file.verify(stream)
            return LinePage(info, text, relative, relative + len(data) - pending, requested)
    except OSError:
        raise SearchFailure("access") from None
