"""Streaming exact top-level account IDs in UTF-8 newline-delimited JSON.

The first nonblank record and candidate records are parsed. Noncandidate JSON
syntax is not audited; all consumed bytes are checked for valid UTF-8. Records
are bounded to 8 MiB. This deliberately is not a general JSON document reader.
"""

from __future__ import annotations

import codecs
import json
import re
import time
from concurrent.futures import CancelledError
from threading import Event

from .file_search import (
    BATCH_SIZE,
    CHUNK_BYTES,
    ERRORS,
    FileInfo,
    Match,
    Progress,
    SearchFailure,
    SearchSummary,
    _decode,
    check_cancel,
    validate_search,
)

MAX_RECORD_BYTES = 8 * 1024 * 1024
ERRORS.update(
    json_record="سجل JSON مرشح غير صالح؛ توقّف البحث. استخدم كائن JSON مستقلًا في كل سطر.",
    json_structure=(
        "المطابقة الدقيقة تتطلب كائن JSON في كل سطر وحقل id علويًا غير مكرر. "
        "النص الحر غير مدعوم هنا؛ استخدم البحث النصي العادي."
    ),
    json_size="يتجاوز سجل JSON حد المعالجة الآمن (8 MiB). استخدم البحث النصي العادي لهذا الملف.",
    json_id="معرّف الحساب المطلوب يجب أن يكون سلسلة أرقام عشرية موجبة دون أصفار بادئة.",
    json_escape=(
        "معرّف JSON المطابق مكتوب بمحارف هروب؛ "
        "استخدم أرقامًا عشرية مباشرة في نسخة من البيانات للمطابقة الموضعية الدقيقة."
    ),
)


def _invalid_constant(_):
    raise ValueError


DECODER = json.JSONDecoder(parse_constant=_invalid_constant)
SPACE = re.compile(r"[ \t\r\n]*")


def record_id_offset(raw, identifier):
    """Validate a candidate and locate only its unique top-level id value."""
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise SearchFailure("encoding") from None
    if "\0" in text:
        raise SearchFailure("encoding")
    pos = SPACE.match(text).end()
    if pos == len(text):
        return None
    if text[pos] != "{":
        raise SearchFailure("json_structure")
    pos += 1
    seen = set()
    match = None
    try:
        pos = SPACE.match(text, pos).end()
        if text[pos : pos + 1] != "}":
            while True:
                key, pos = DECODER.raw_decode(text, pos)
                if not isinstance(key, str) or key in seen:
                    raise SearchFailure("json_structure")
                seen.add(key)
                pos = SPACE.match(text, pos).end()
                if text[pos : pos + 1] != ":":
                    raise ValueError
                start = SPACE.match(text, pos + 1).end()
                value, pos = DECODER.raw_decode(text, start)
                if key == "id" and (
                    (type(value) is int and str(value) == identifier)
                    or (isinstance(value, str) and value == identifier)
                ):
                    token = text[start:pos]
                    if token not in (identifier, '"' + identifier + '"'):
                        raise SearchFailure("json_escape")
                    match = len(text[:start].encode("utf-8")) + int(isinstance(value, str))
                pos = SPACE.match(text, pos).end()
                if text[pos : pos + 1] == "}":
                    break
                if text[pos : pos + 1] != ",":
                    raise ValueError
                pos = SPACE.match(text, pos + 1).end()
        if text[pos : pos + 1] != "}" or SPACE.match(text, pos + 1).end() != len(text):
            raise ValueError
    except (ValueError, RecursionError, OverflowError):
        raise SearchFailure("json_record") from None
    return match


def search_account_ids(
    paths, identifier, limit=100, *, cancel=None, on_batch=None, on_progress=None, chunk_bytes=CHUNK_BYTES
):
    """Filter blocks in C before parsing candidate lines; never inspect nested IDs."""
    needle = validate_search(identifier, limit)
    if not re.fullmatch(rb"[1-9][0-9]*", needle):
        raise SearchFailure("json_id")
    if not 4 <= chunk_bytes <= MAX_RECORD_BYTES:
        raise ValueError("chunk_bytes must be between four and MAX_RECORD_BYTES")
    cancel = cancel if cancel is not None else Event()
    started = time.perf_counter()
    files, seen, batch = [], set(), []
    current = ""
    done = found = processed = total = bytes_read = 0
    last_update = 0.0

    def progress(force=False):
        nonlocal last_update
        now = time.perf_counter()
        value = Progress(current, done, len(files), found, processed, total, bytes_read, now - started)
        if on_progress and (force or now - last_update >= 0.1):
            on_progress(value)
            last_update = now
        return value

    def deliver():
        if batch:
            if on_batch and on_batch(list(batch)) is False:
                raise CancelledError
            batch.clear()
            check_cancel(cancel)

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
                base, line, tail = signature, 1, b""
                first = True
                decoder = codecs.getincrementaldecoder("utf-8")("strict")
                while True:
                    check_cancel(cancel)
                    chunk = stream.read(chunk_bytes)
                    bytes_read += len(chunk)
                    data = tail + chunk
                    end = data.rfind(b"\n") + 1 if chunk else len(data)
                    if end == 0 and chunk:
                        if len(data) > MAX_RECORD_BYTES:
                            raise SearchFailure("json_size")
                        tail = data
                        continue
                    if tail and data.find(b"\n") > MAX_RECORD_BYTES:
                        raise SearchFailure("json_size")
                    cursor, scan_line = 0, line
                    # C-level filtering avoids regex work over every byte. Cache the
                    # next escape candidate so dense literal hits do not rescan tails.
                    escaped = data.find(b"\\u003", 0, end)
                    while cursor < end:
                        check_cancel(cancel)
                        candidate = data.find(needle, cursor, end)
                        if 0 <= escaped < cursor:
                            escaped = data.find(b"\\u003", cursor, end)
                        if escaped >= 0:
                            candidate = min(candidate, escaped) if candidate >= 0 else escaped
                        if not first and candidate < 0:
                            break
                        start = cursor if first else data.rfind(b"\n", cursor, candidate) + 1
                        # rfind returns -1 when the candidate is in the current line.
                        start = max(cursor, start)
                        stop = data.find(b"\n", start, end)
                        stop = end if stop < 0 else stop + 1
                        scan_line += data.count(b"\n", cursor, start)
                        raw = data[start:stop]
                        if len(raw) > MAX_RECORD_BYTES:
                            raise SearchFailure("json_size")
                        offset = record_id_offset(raw, identifier)
                        if raw.strip():
                            first = False
                        _decode(decoder, data[cursor:stop])
                        processed += stop - cursor
                        if offset is not None:
                            info.verify(stream)
                            batch.append(Match(info, scan_line, base + start, base + start + offset))
                            found += 1
                            if len(batch) == BATCH_SIZE:
                                deliver()
                            if found == limit:
                                deliver()
                                eof = base + stop == info.size
                                done += int(eof)
                                status = "complete" if eof and done == len(files) else "limit"
                                return SearchSummary(status, progress(True))
                        scan_line += data.count(b"\n", start, stop)
                        cursor = stop
                    _decode(decoder, data[cursor:end], final=not chunk)
                    info.verify(stream)
                    processed += end - cursor
                    line += data.count(b"\n", 0, end)
                    base += end
                    tail = data[end:]
                    if len(tail) > MAX_RECORD_BYTES:
                        raise SearchFailure("json_size")
                    deliver()
                    progress()
                    if not chunk:
                        done += 1
                        break
        return SearchSummary("complete", progress(True))
    except CancelledError:
        return SearchSummary("cancelled", progress(True))
    except (SearchFailure, OSError) as exc:
        try:
            deliver()
        except CancelledError:
            return SearchSummary("cancelled", progress(True))
        return SearchSummary(
            "error", progress(True), exc.code if isinstance(exc, SearchFailure) else "access"
        )
