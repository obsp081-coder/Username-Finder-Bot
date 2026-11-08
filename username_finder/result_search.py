"""Read-only, paginated searches of current platform results and optional observations."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from . import x

# Descriptions mirror the existing local syntax rules, not registration guarantees.
SYMBOL_RULES = {
    "Roblox": {"symbols": "_", "note": "Roblox: شرطة سفلية واحدة داخل الاسم، لا في طرفيه."},
    "X": {"symbols": "_", "note": "X: الشرطة السفلية مدعومة، بما في ذلك الطرفان."},
}
CLASSES = ("Available", "Taken", "Reserved", "Invalid", "Unknown")
COMPOSITIONS = (
    "any",
    "letters",
    "digits",
    "mixed_plain",
    "alphanumeric",
    "letter",
    "digit",
    "mixed",
    "symbols",
    "plain",
)
REPETITIONS = ("any", "none", "anywhere", "consecutive", "letters", "digits")


@dataclass(frozen=True)
class ResultFilter:
    text: str = ""
    text_mode: str = "contains"
    classification: str = ""
    length_mode: str = "any"
    minimum: int = 0
    maximum: int = 0
    composition: str = "any"
    repetition: str = "any"
    digit_mode: str = "any"
    digits: int = 0
    letter_mode: str = "any"
    letters: int = 0
    symbol_mode: str = "any"
    symbol: str = "_"
    changed: bool = False

    def validate(self):
        choices = (
            (self.text_mode, ("contains", "starts", "ends", "exact")),
            (self.classification, ("", *CLASSES)),
            (self.length_mode, ("any", "exact", "range")),
            (self.composition, COMPOSITIONS),
            (self.repetition, REPETITIONS),
            (self.digit_mode, ("any", "minimum", "exact")),
            (self.letter_mode, ("any", "minimum", "exact")),
            (self.symbol_mode, ("any", "present", "absent", "contains", "excludes")),
        )
        if any(value not in allowed for value, allowed in choices):
            raise ValueError("خيار تصفية غير صالح.")
        if any(type(n) is not int or n < 0 for n in (self.minimum, self.maximum, self.digits, self.letters)):
            raise ValueError("أعداد التصفية يجب أن تكون صحيحة وغير سالبة.")
        if self.length_mode == "range" and self.minimum > self.maximum:
            raise ValueError("الطول الأدنى أكبر من الأعلى.")
        if self.symbol_mode in ("contains", "excludes") and (
            len(self.symbol) != 1 or self.symbol.isalnum() or self.symbol.isspace()
        ):
            raise ValueError("اختر رمزًا واحدًا؛ ظهوره في السجل لا يثبت صلاحيته للمنصة.")
        return self


def register_functions(db):
    # Bounded per-connection cache avoids recomputing traits for combined predicates.
    @lru_cache(maxsize=256)
    def traits(name):
        folded = name.casefold()
        digits = sum("0" <= c <= "9" for c in folded)
        seen, flags = set(), 0
        for index, char in enumerate(folded):
            if char in seen:
                flags |= 1
                if "a" <= char <= "z":
                    flags |= 4
                if "0" <= char <= "9":
                    flags |= 8
            if index and char == folded[index - 1]:
                flags |= 2
            seen.add(char)
        # Count original ASCII characters: casefold may expand non-ASCII invalid input.
        letters = sum(c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" for c in name)
        return letters, digits, len(name) - letters - digits, flags

    db.create_function("username_fold", 1, str.casefold, deterministic=True)
    for index, name in enumerate(("name_letters", "name_digits", "name_symbols", "name_repeats")):
        db.create_function(name, 1, lambda value, i=index: traits(value)[i], deterministic=True)


def predicate(filters):
    """One predicate shared by count, page and streamed export; all values are parameters."""
    f = filters.validate()
    clauses, args = [], []

    def add(sql, *values):
        clauses.append(sql)
        args.extend(values)

    if f.classification:
        add("c.classification=?", f.classification)
    if f.text:
        text = f.text.casefold()
        match f.text_mode:
            case "contains":
                add("instr(username_fold(c.username),?)>0", text)
            case "starts":
                add("substr(username_fold(c.username),1,length(?))=?", text, text)
            case "ends":
                add("substr(username_fold(c.username),-length(?))=?", text, text)
            case "exact":
                add("username_fold(c.username)=?", text)
    if f.length_mode == "exact":
        add("length(c.username)=?", f.minimum)
    elif f.length_mode == "range":
        add("length(c.username) BETWEEN ? AND ?", f.minimum, f.maximum)
    composition = {
        "letters": "name_letters(c.username)=length(c.username) AND length(c.username)>0",
        "digits": "name_digits(c.username)=length(c.username) AND length(c.username)>0",
        "mixed_plain": (
            "name_letters(c.username)>0 AND name_digits(c.username)>0 AND name_symbols(c.username)=0"
        ),
        "alphanumeric": "name_symbols(c.username)=0 AND length(c.username)>0",
        "letter": "name_letters(c.username)>0",
        "digit": "name_digits(c.username)>0",
        "mixed": "name_letters(c.username)>0 AND name_digits(c.username)>0",
        "symbols": "name_symbols(c.username)>0",
        "plain": "name_symbols(c.username)=0",
    }
    if f.composition != "any":
        add("(" + composition[f.composition] + ")")
    for mode, amount, function in (
        (f.digit_mode, f.digits, "name_digits"),
        (f.letter_mode, f.letters, "name_letters"),
    ):
        if mode != "any":
            add(f"{function}(c.username){'=' if mode == 'exact' else '>='}?", amount)
    if f.repetition != "any":
        flag = {"none": 1, "anywhere": 1, "consecutive": 2, "letters": 4, "digits": 8}[f.repetition]
        add(f"(name_repeats(c.username)&?){'=0' if f.repetition == 'none' else '>0'}", flag)
    if f.symbol_mode in ("present", "absent"):
        add("name_symbols(c.username)" + (">0" if f.symbol_mode == "present" else "=0"))
    elif f.symbol_mode in ("contains", "excludes"):
        add("instr(c.username,?)" + (">0" if f.symbol_mode == "contains" else "=0"), f.symbol)
    if f.changed:
        add("c.changed=1")
    return " AND ".join(clauses) or "1", args


def query_parts(scope, filters, history=False):
    platforms = tuple(SYMBOL_RULES) if scope == "All" else (scope,)
    if any(platform not in SYMBOL_RULES for platform in platforms):
        raise ValueError("منصة نتائج غير مدعومة.")
    where, values = predicate(filters)
    parts, args = [], []
    for platform in platforms:
        prefix = "roblox" if platform == "Roblox" else "x"
        base = (
            f"{prefix}_checks c LEFT JOIN {prefix}_names n ON n.username_key=c.username_key"
            if history
            else f"{prefix}_names n JOIN {prefix}_checks c ON c.id=n.latest_id"
        )
        extra = "c.api_code,c.source" if platform == "Roblox" else "NULL AS api_code,'X API' AS source"
        parts.append(
            f"SELECT '{platform}' AS platform,c.id,c.username,c.username_key,c.checked_at,c.classification,"
            f"c.evidence,c.previous_classification,c.changed,n.completed_checks,{extra} "
            f"FROM {base} WHERE {where}"
        )
        args.extend(values)
    return " UNION ALL ".join(parts), args


class SearchCancelled(Exception):
    pass


class ResultSearch:
    def __init__(self, path):
        self.path = Path(path).resolve()

    @contextmanager
    def connection(self, cancel=None):
        cancel = cancel or threading.Event()
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            register_functions(db)
            db.set_progress_handler(lambda: int(cancel.is_set()), 500)
            if cancel.is_set():
                raise SearchCancelled
            db.execute("BEGIN")
            yield db
        except sqlite3.OperationalError as exc:
            if cancel.is_set():
                raise SearchCancelled from exc
            raise
        finally:
            db.close()

    def page(self, scope, filters, *, offset=0, limit=100, cancel=None):
        if offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("صفحة نتائج غير صالحة.")
        query, args = query_parts(scope, filters)
        order = "id DESC" if scope == "X" else "checked_at DESC,platform,id DESC"
        with self.connection(cancel) as db:
            self.last_counts = dict(
                db.execute(
                    "SELECT classification,COUNT(*) FROM (" + query + ") GROUP BY classification", args
                )
            )
            total = sum(self.last_counts.values())
            # Deletions/restores can make an old offset invalid; return a valid final page.
            offset = min(offset, ((total - 1) // limit) * limit) if total else 0
            rows = db.execute(query + f" ORDER BY {order} LIMIT ? OFFSET ?", [*args, limit, offset])
            return [dict(row) for row in rows], total, offset

    def export(self, path, scope, filters, *, history=False, cancel=None):
        path = Path(path)
        if path.resolve() in {self.path, *(Path(str(self.path) + s) for s in ("-wal", "-shm", "-journal"))}:
            raise ValueError("لا يمكن استبدال قاعدة البيانات بالتصدير.")
        query, args = query_parts(scope, filters, history)
        fields = export_fields(scope)
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        count = 0
        try:
            with (
                self.connection(cancel) as db,
                temporary.open("w", encoding="utf-8-sig", newline="") as stream,
            ):
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for row in db.execute(query + " ORDER BY checked_at,platform,id", args):
                    if cancel and cancel.is_set():
                        raise SearchCancelled
                    writer.writerow(export_values(dict(row), fields))
                    count += 1
                stream.flush()
                os.fsync(stream.fileno())
            if cancel and cancel.is_set():
                raise SearchCancelled
            os.replace(temporary, path)
            return count
        finally:
            temporary.unlink(missing_ok=True)


def export_fields(scope):
    roblox_fields = (
        "username",
        "checked_at",
        "classification",
        "api_code",
        "source",
        "completed_checks",
        "previous_classification",
        "changed",
        "http_status",
        "response_data",
        "raw_complete",
        "error_type",
        "preliminary_classification",
        "manual_signup_result",
        "delivery_uncertain",
        "evidence",
    )
    x_fields = (
        "username",
        "classification",
        "checked_at",
        "previous_classification",
        "changed",
        "completed_checks",
        "api_reason",
        "preliminary",
        "independently_verified",
        "warning",
        "evidence",
    )
    if scope == "Roblox":
        return roblox_fields
    if scope == "X":
        return x_fields
    return ("platform", *dict.fromkeys((*roblox_fields, *x_fields)))


def export_values(row, fields):
    evidence = json.loads(row["evidence"])
    row.update(
        http_status=evidence.get("http_status"),
        response_data=evidence.get("response_data", ""),
        raw_complete=evidence.get("raw_complete", False),
        error_type=evidence.get("error", ""),
        preliminary_classification=row["classification"],
        manual_signup_result="NotChecked",
        delivery_uncertain=evidence.get("delivery_uncertain", False),
        api_reason=evidence.get("api_reason"),
        preliminary=True,
        independently_verified=False,
        warning=x.WARNING if row["platform"] == "X" else "نتيجة Roblox أولية؛ لا تضمن التسجيل.",
    )
    return {field: row.get(field, "") for field in fields}
