"""Isolated, staged, unauthenticated research. No application imports or live import-time work."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import secrets
import sqlite3
import statistics
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ENDPOINT = "https://api.x.com/i/users/username_available.json"
STAGES = {1: 5, 2: 15, 3: 80}
CLASSES = ("Available", "Taken", "Reserved", "Invalid", "Unknown")
MAX_BODY = 1024 * 1024
HEADERS = {
    "content-type",
    "date",
    "retry-after",
    "x-rate-limit-limit",
    "x-rate-limit-remaining",
    "x-rate-limit-reset",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}
SECRET = re.compile(r"password|token|cookie|authorization|session|secret|csrf|email", re.I)


def utcnow():
    return datetime.now(UTC)


def atomic_text(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SECRET.search(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"(?is)<script\b[^>]*>.*?(?:</script>|$)", "<script>[REDACTED]</script>", value)
    value = re.sub(r"""(?i)\bvalue\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", 'value="[REDACTED]"', value)
    value = re.sub(
        r"""(?i)((?:password|token|cookie|authorization|session|secret|csrf|email)[\w-]*["']?\s*[:=]\s*)["']?[^\s,;<>"']+""",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [REDACTED]", value)
    return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", value)


def manifest_rows():
    rows = []

    def add(name, category, hypothesis):
        rows.append({"username": name, "category": category, "hypothesis": hypothesis})

    add("OpenAI", "existing_account_candidate", "May be taken; not independently verified here")
    add("github", "existing_account_candidate", "May be taken; not independently verified here")

    def random_name(length):
        while True:
            value = secrets.choice(string.ascii_lowercase) + "".join(
                secrets.choice(string.ascii_lowercase + string.digits) for _ in range(length - 1)
            )
            if value.casefold() not in {r["username"].casefold() for r in rows}:
                return value

    add(random_name(5), "valid_short", "Format candidate only; availability unknown")
    add(random_name(15), "valid_long", "15-character change-handle boundary; availability unknown")
    add("Support", "existing_account_candidate", "May be taken; not independently verified here")
    for name in ("Microsoft", "Google"):
        add(name, "existing_account_candidate", "May be taken; not independently verified here")
    for name in ("admin", "administrator", "twitter", "xadmin", "officialadmin"):
        add(name, "restricted_looking", "Policy/reservation hypothesis; ownership and reservation unproven")
    for name in ("bad-name", "abcd", "a" * 16, "bad.name", "bad name", "@abcde"):
        add(name, "invalid_format", "Outside the scoped 5-15 change-handle syntax")
    while len(rows) < 20:
        add(random_name(5), "valid_short", "Format candidate only; availability unknown")
    for index in range(80):
        length = (5, 15, 8, 12)[index % 4]
        add(
            random_name(length),
            "valid_short" if length == 5 else "valid_long" if length == 15 else "random_valid",
            "Format candidate only; availability unknown",
        )
    for index, row in enumerate(rows, 1):
        row.update(position=index, stage=1 if index <= 5 else 2 if index <= 20 else 3)
    return rows


@dataclass
class Response:
    status: int | None = None
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    error: str = ""
    complete: bool = True


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def fetch(username, timeout):
    request = urllib.request.Request(
        ENDPOINT + "?" + urllib.parse.urlencode({"username": username}),
        headers={"Accept": "application/json", "User-Agent": "XUsernameResearch/0.1"},
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        try:
            stream = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            stream = error
        with stream:
            # Only selected diagnostic headers, never cookies or authentication secrets.
            headers = {key.lower(): value for key, value in stream.headers.items() if key.lower() in HEADERS}
            for key in stream.headers:
                if re.search(r"challenge|captcha|cf-mitigated", key, re.I):
                    headers["challenge-indicator"] = "present"
            body = stream.read(MAX_BODY + 1)
            return Response(stream.status, body[:MAX_BODY], headers, complete=len(body) <= MAX_BODY)
    except (TimeoutError, urllib.error.URLError, OSError) as error:
        return Response(error=type(error).__name__, complete=False)


def validate_syntax(username):
    """Change-handle syntax only; no availability or keyword-reservation inference."""
    issues = []
    if len(username) < 5:
        issues.append("too_short")
    if len(username) > 15:
        issues.append("too_long")
    if re.search(r"[^A-Za-z0-9_]", username):
        issues.append("unsupported_characters")
    return {"valid": not issues, "issues": issues, "boundary_needs_verification": len(username) == 15}


def length_rejection(parsed, username):
    """Require coherent, bounded length wording and an independent local violation."""
    msg = " ".join(parsed.get("msg", "").split())
    desc = " ".join(parsed.get("desc", "").split())
    patterns = (
        (
            "short",
            "too_short",
            r"(?:Your )?username (?:is )?too short[.!]?",
            r"(?:Your )?username must be (?:longer than 4 characters|at least 5 characters(?: long)?)[.!]?",
        ),
        (
            "long",
            "too_long",
            r"(?:Your )?username (?:is )?too long[.!]?",
            r"(?:Your )?username (?:must be (?:shorter than 15 characters|at most 15 characters(?: long)?)"
            r"|cannot be longer than 15 characters)[.!]?",
        ),
    )
    issues = validate_syntax(username)["issues"]
    for kind, issue, msg_pattern, desc_pattern in patterns:
        if (
            issue in issues
            and re.fullmatch(msg_pattern, msg, re.I)
            and re.fullmatch(desc_pattern, desc, re.I)
        ):
            return kind
    return None


def positive_availability(message):
    without_negatives = re.sub(r"\bnot\s+available\b|\bunavailable\b", "", message, flags=re.I)
    return bool(re.search(r"\bavailable\b", without_negatives, re.I))


def parse(response, username):
    parsed = None
    try:

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate_json_key")
                result[key] = value
            return result

        parsed = json.loads(
            response.body.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")),
        )
    except (ValueError, UnicodeError, RecursionError):
        pass
    reason = ""
    remaining_values = [
        response.headers[key].strip()
        for key in ("x-rate-limit-remaining", "x-ratelimit-remaining")
        if key in response.headers
    ]
    if response.error:
        reason = "network_or_execution_error:" + response.error
    elif not response.complete:
        reason = "incomplete_or_oversized_body"
    elif response.status != 200:
        reason = f"unexpected_http_status:{response.status}"
    elif "retry-after" in response.headers:
        reason = "retry_after_present"
    elif any(re.fullmatch(r"-?\d+", value) and int(value) <= 0 for value in remaining_values):
        reason = "exhausted_rate_limit"
    elif any(not value.isdecimal() for value in remaining_values):
        reason = "unexpected_rate_limit_header"
    elif response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        reason = "unexpected_or_missing_content_type"
    elif "challenge-indicator" in response.headers or re.search(
        r"captcha|challenge|access denied|authenticate|authentication|log[ -]?in required|rate limit",
        response.body.decode("utf-8", errors="replace"),
        re.I,
    ):
        reason = "challenge_or_access_restriction"
    elif not isinstance(parsed, dict) or set(parsed) - {"valid", "reason", "msg", "desc"}:
        reason = "invalid_json_or_unexpected_schema"
    elif (
        type(parsed.get("valid")) is not bool
        or not isinstance(parsed.get("reason"), str)
        or any(not isinstance(parsed[key], str) for key in ("msg", "desc") if key in parsed)
    ):
        reason = "unexpected_field_types"
    else:
        pair = parsed["valid"], parsed["reason"]
        message = " ".join(parsed.get(key, "") for key in ("msg", "desc"))
        # Historically documented pairs only. These remain preliminary API indications.
        if pair == (True, "available"):
            if re.search(r"taken|not available|unavailable|invalid|reserved", message, re.I):
                return "Unknown", "contradictory_message", parsed
            if validate_syntax(username)["valid"]:
                return "Available", "historical_pair_available_unverified_current_semantics", parsed
            reason = "available_conflicts_with_documented_local_syntax"
        elif pair == (False, "taken"):
            if positive_availability(message) or re.search(r"\breserved\b|\binvalid\b", message, re.I):
                return "Unknown", "contradictory_message", parsed
            return "Taken", "historical_pair_taken_unverified_current_semantics", parsed
        elif pair in ((False, "is_banned_word"), (False, "contains_banned_word")):
            if positive_availability(message) or re.search(r"\btaken\b|\binvalid\b", message, re.I):
                return "Unknown", "contradictory_message", parsed
            return "Reserved", f"preliminary_api_{pair[1]}_rejection_not_account_ownership", parsed
        elif pair == (False, "improper_format"):
            if re.search(r"taken|reserved", message, re.I) or positive_availability(message):
                return "Unknown", "contradictory_message", parsed
            if validate_syntax(username)["valid"]:
                return "Unknown", "format_rejection_conflicts_with_documented_local_syntax", parsed
            return (
                "Invalid",
                "preliminary_api_improper_format_syntax_rejection_not_ownership_or_availability",
                parsed,
            )
        elif pair == (False, "invalid_username"):
            kind = length_rejection(parsed, username)
            if kind:
                return "Invalid", f"preliminary_api_invalid_username_explicit_{kind}_length_rejection", parsed
            reason = "unsupported_or_contradictory_invalid_username_evidence"
        else:
            # Other rejection reasons have no established mapping.
            reason = "unrecognized_or_contradictory_pair"
    return "Unknown", reason, parsed


@contextmanager
def exclusive(folder):
    with (folder / ".run.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("Another experiment process is active") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Experiment:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.db = sqlite3.connect(self.folder / "evidence.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")

    def close(self):
        self.db.close()

    def setting(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))

    def validate(self):
        raw = (self.folder / "manifest.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.setting("manifest_sha256"):
            raise ValueError("Manifest changed; refusing requests")
        rows = json.loads(raw)["candidates"]
        expected = [
            tuple(r[k] for k in ("position", "stage", "username", "category", "hypothesis")) for r in rows
        ]
        actual = [tuple(row) for row in self.db.execute("SELECT * FROM candidates ORDER BY position")]
        if expected != actual or len(rows) != 100 or len({r["username"].casefold() for r in rows}) != 100:
            raise ValueError("Candidate manifest/database mismatch")
        if Counter(r["stage"] for r in rows) != STAGES:
            raise ValueError("Stage sizes changed")

    def recover(self):
        with self.db:
            for row in self.db.execute("SELECT * FROM outcomes WHERE state='InFlight'").fetchall():
                record = json.loads(row["record"])
                record.update(
                    classification="Unknown",
                    reason="interrupted_delivery_uncertain",
                    delivery_uncertain=True,
                    completed_response=False,
                )
                self.db.execute(
                    "UPDATE outcomes SET state='Done',record=? WHERE position=?",
                    (json.dumps(record), row["position"]),
                )
                self.set("blocked", "interrupted_delivery_uncertain")
                self.set("last_stop_reason", "interrupted_delivery_uncertain")
            self.db.execute(
                "UPDATE sessions SET reason='process_interrupted_runtime_incomplete' WHERE elapsed IS NULL"
            )

    def reviews(self):
        if not self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='classification_reviews'"
        ).fetchone():
            return []
        return [
            dict(row) for row in self.db.execute("SELECT * FROM classification_reviews ORDER BY position")
        ]

    def records(self):
        reviews = {r["position"]: r for r in self.reviews()}
        records = []
        for row in self.db.execute("SELECT position,record FROM outcomes ORDER BY position"):
            record = json.loads(row["record"])
            review = reviews.get(row["position"])
            if review:
                if hashlib.sha256(row["record"].encode()).hexdigest() != review["original_sha256"]:
                    raise ValueError("Reviewed evidence changed; refusing reinterpretation")
                record.update(
                    original_classification=record["classification"],
                    original_reason=record["reason"],
                    classification=review["classification"],
                    reason=review["reason"],
                    reviewed_at=review["reviewed_at"],
                )
            records.append(record)
        return records

    def review_stage1(self, *, apply=False, clock=utcnow):
        return self.review_stage(1, apply=apply, clock=clock)

    def review_stage2(self, *, apply=False, clock=utcnow):
        return self.review_stage(2, apply=apply, clock=clock)

    def review_stage2_improper_format(self, *, apply=False, clock=utcnow):
        return self.review_stage(2, apply=apply, clock=clock, improper_format=True)

    def review_stage2_invalid_username(self, *, apply=False, clock=utcnow):
        return self.review_stage(2, apply=apply, clock=clock, invalid_username=True)

    def review_stage(
        self, stage, *, apply=False, clock=utcnow, improper_format=False, invalid_username=False
    ):
        """Review only a specific observed rejection stop, never provider restrictions."""
        if stage not in (1, 2):
            raise ValueError("Reviewed resume is supported only for the observed Stage 1/2 stops")
        if improper_format and stage != 2:
            raise ValueError("improper_format reviewed resume is limited to Stage 2")
        if invalid_username and (stage != 2 or improper_format):
            raise ValueError("invalid_username requires its own Stage 2 review")
        position, api_reason = (2, "is_banned_word") if stage == 1 else (8, "contains_banned_word")
        stop_reason = "unrecognized_or_contradictory_pair"
        if improper_format:
            position, api_reason = 13, "improper_format"
        if invalid_username:
            # The same explicit length-review command covers only observed evidence stops.
            target = "abcd"
            long_row = self.db.execute(
                "SELECT c.position,o.record FROM candidates c JOIN outcomes o USING(position) "
                "WHERE c.stage=2 AND c.username=?",
                ("a" * 16,),
            ).fetchone()
            if long_row:
                saved = json.loads(long_row[1])
                if saved.get("classification") == "Unknown" and saved.get("reason") == (
                    "unsupported_or_contradictory_invalid_username_evidence"
                ):
                    target = "a" * 16
                    stop_reason = "unsupported_or_contradictory_invalid_username_evidence"
            candidate = self.db.execute(
                "SELECT position FROM candidates WHERE stage=2 AND username=?",
                (target,),
            ).fetchone()
            if not candidate:
                raise ValueError("Observed length-rejected candidate missing from persisted manifest")
            position, api_reason = candidate[0], "invalid_username"
        remaining = self.db.execute(
            "SELECT COUNT(*) FROM candidates WHERE stage=? "
            "AND position NOT IN (SELECT position FROM outcomes)",
            (stage,),
        ).fetchone()[0]
        rows = self.db.execute("SELECT * FROM outcomes ORDER BY position").fetchall()
        existing = self.reviews()
        effective = self.records()  # Validate existing review digests, including Stage 1.
        if any(review["position"] == position for review in existing):
            # Repeated acknowledgement is harmless, but never unlocks a later failure.
            if self.setting("blocked"):
                raise ValueError("A later stop requires separate investigation; review cannot unlock it")
            return {"already_reviewed": True, "reviews": existing, f"remaining_stage{stage}": remaining}
        if (
            self.setting("blocked") != stop_reason
            or len(rows) != position
            or [(r["position"], r["stage"], r["state"]) for r in rows]
            != [(i, 1 if i <= 5 else 2, "Done") for i in range(1, position + 1)]
        ):
            raise ValueError(f"Review requires exactly the original {position} completed responses")
        records = [json.loads(row["record"]) for row in rows]
        names = [
            r[0]
            for r in self.db.execute(
                "SELECT username FROM candidates WHERE position<=? ORDER BY position", (position,)
            )
        ]
        required_names = {1: "OpenAI", 2: "github", 6: "Microsoft", 7: "Google", 8: "admin", 13: "bad-name"}
        if invalid_username:
            required_names[position] = target
        if any(names[i - 1] != name for i, name in required_names.items() if i <= position):
            raise ValueError("Candidate identities differ from the observed stop")
        for index, (record, name) in enumerate(zip(records, names, strict=True)):
            expected = (
                ("Invalid" if improper_format or invalid_username else "Reserved")
                if index == position - 1
                else "Taken"
                if stage == 1
                else effective[index]["classification"]
            )
            raw = record.get("raw_response_redacted", "").encode("utf-8")
            if (
                record.get("username") != name
                or not record.get("completed_response")
                or record.get("delivery_uncertain")
                or record.get("redaction_applied")
                or hashlib.sha256(raw).hexdigest() != record.get("raw_sha256")
            ):
                raise ValueError("Incomplete, altered or uncertain historical evidence")
            classification, reason, parsed = parse(
                Response(
                    record.get("http_status"),
                    raw,
                    record.get("headers", {}),
                    record.get("error", ""),
                    record.get("raw_complete", False),
                ),
                name,
            )
            if (
                classification == "Unknown"
                or classification != expected
                or parsed != record.get("parsed_fields")
            ):
                raise ValueError("Historical response is not eligible for the reviewed correction")
        original = records[-1]
        if (
            original["classification"] != "Unknown"
            or original["reason"] != stop_reason
            or original["parsed_fields"].get("reason") != api_reason
        ):
            raise ValueError("Historical classifications differ from the observed stop")
        review = dict(
            position=position,
            original_sha256=hashlib.sha256(rows[-1]["record"].encode()).hexdigest(),
            original_classification=original["classification"],
            original_reason=original["reason"],
            classification=classification,
            reason=reason,
            reviewed_at=clock().isoformat(),
        )
        if apply:
            with self.db:
                self.db.execute("""CREATE TABLE IF NOT EXISTS classification_reviews(
                    position INTEGER PRIMARY KEY, original_sha256 TEXT,
                    original_classification TEXT, original_reason TEXT,
                    classification TEXT, reason TEXT, reviewed_at TEXT)""")
                self.db.execute(
                    "INSERT INTO classification_reviews VALUES(?,?,?,?,?,?,?)", tuple(review.values())
                )
                self.set("blocked", None)
                self.set("last_stop_reason", f"reviewed_{api_reason}_resume_authorized_stage{stage}")
        return {"review": review, f"remaining_stage{stage}": remaining, "preliminary": True, "applied": apply}

    def run(
        self,
        stage,
        *,
        delay=5.0,
        timeout=15.0,
        fetcher=fetch,
        cancel=None,
        clock=utcnow,
        monotonic=time.monotonic,
        wait=None,
        resume_reviewed_is_banned_word=False,
        resume_reviewed_contains_banned_word=False,
        resume_reviewed_improper_format=False,
        resume_reviewed_invalid_username=False,
    ):
        if stage not in STAGES or not math.isfinite(delay) or delay < 5 or not 1 <= timeout <= 60:
            raise ValueError("Use stage 1/2/3, delay >=5 seconds, timeout 1-60 seconds")
        if resume_reviewed_is_banned_word and stage != 1:
            raise ValueError("Reviewed resume is limited to Stage 1")
        if resume_reviewed_contains_banned_word and stage != 2:
            raise ValueError("contains_banned_word reviewed resume is limited to Stage 2")
        if resume_reviewed_improper_format and (stage != 2 or resume_reviewed_contains_banned_word):
            raise ValueError("improper_format requires Stage 2 and its own explicit review flag")
        if resume_reviewed_invalid_username and (
            stage != 2 or resume_reviewed_improper_format or resume_reviewed_contains_banned_word
        ):
            raise ValueError("invalid_username requires Stage 2 and its own explicit review flag")
        cancel = cancel or threading.Event()
        wait = wait or cancel.wait
        with exclusive(self.folder):
            self.validate()
            self.recover()
            if resume_reviewed_is_banned_word:
                self.review_stage1(apply=True, clock=clock)
            if resume_reviewed_contains_banned_word:
                self.review_stage2(apply=True, clock=clock)
            if resume_reviewed_improper_format:
                self.review_stage2_improper_format(apply=True, clock=clock)
            if resume_reviewed_invalid_username:
                self.review_stage2_invalid_username(apply=True, clock=clock)
            self.export()
            if self.setting("blocked"):
                raise ValueError(
                    "Experiment stopped; inspect evidence before any new research: " + self.setting("blocked")
                )
            for previous in range(1, stage):
                if (
                    self.db.execute(
                        "SELECT COUNT(*) FROM outcomes WHERE stage=? AND state='Done'", (previous,)
                    ).fetchone()[0]
                    != STAGES[previous]
                ):
                    raise ValueError("Complete each previous stage explicitly first")
            pending = self.db.execute(
                "SELECT * FROM candidates WHERE stage=? AND position NOT IN "
                "(SELECT position FROM outcomes) ORDER BY position",
                (stage,),
            ).fetchall()
            if not pending:
                return self.report()
            started = monotonic()
            with self.db:
                session = self.db.execute(
                    "INSERT INTO sessions(stage,started_at,delay,timeout) VALUES(?,?,?,?)",
                    (stage, clock().isoformat(), delay, timeout),
                ).lastrowid
            stop = "stage_completed"
            current = None
            try:
                for candidate in pending:
                    if cancel.is_set():
                        stop = "cancelled_between_requests"
                        break
                    deadline = self.setting("not_before")
                    remaining = (
                        max(0, (datetime.fromisoformat(deadline) - clock()).total_seconds())
                        if deadline
                        else 0
                    )
                    if remaining and wait(remaining):
                        stop = "cancelled_during_delay"
                        break
                    if cancel.is_set():
                        stop = "cancelled_between_requests"
                        break
                    current = candidate["position"]
                    record = dict(candidate)
                    record.update(
                        request_at=clock().isoformat(),
                        classification="Unknown",
                        reason="in_flight",
                        preliminary=True,
                        independently_verified="NotChecked",
                        requested_delay=delay,
                        http_status=None,
                        latency_seconds=None,
                        completed_response=False,
                        delivery_uncertain=True,
                        endpoint=ENDPOINT,
                        method="GET",
                        parameters={"username": candidate["username"]},
                    )
                    with self.db:
                        self.db.execute(
                            "INSERT INTO outcomes VALUES(?,?,?,?)",
                            (current, stage, "InFlight", json.dumps(record)),
                        )
                    request_start = monotonic()
                    response = fetcher(candidate["username"], timeout)
                    finished = clock()
                    latency = monotonic() - request_start
                    classification, reason, parsed = parse(response, candidate["username"])
                    raw = response.body.decode("utf-8", errors="replace")
                    sanitized = (
                        json.dumps(redact(parsed), ensure_ascii=False)
                        if parsed is not None and redact(parsed) != parsed
                        else redact(raw)
                    )
                    record.update(
                        http_status=response.status,
                        headers=redact(response.headers),
                        raw_response_redacted=sanitized,
                        raw_sha256=hashlib.sha256(response.body).hexdigest(),
                        raw_bytes=len(response.body),
                        raw_complete=response.complete,
                        parsed_fields=redact(parsed),
                        classification=classification,
                        reason=reason,
                        latency_seconds=latency,
                        error=response.error,
                        response_at=finished.isoformat(),
                        completed_response=response.status is not None and response.complete,
                        delivery_uncertain=bool(response.error),
                        redaction_applied=sanitized != raw,
                    )
                    with self.db:
                        self.db.execute(
                            "UPDATE outcomes SET state='Done',record=? WHERE position=?",
                            (json.dumps(record), current),
                        )
                        self.set("not_before", (finished + timedelta(seconds=delay)).isoformat())
                        if classification == "Unknown":
                            self.set("blocked", reason)
                    current = None
                    self.export()
                    if classification == "Unknown":
                        stop = reason
                        break
            except KeyboardInterrupt:
                stop = (
                    "cancelled_during_request_delivery_uncertain" if current else "cancelled_between_requests"
                )
                self.recover()
            except Exception as error:
                stop = "unexpected_execution_error:" + type(error).__name__
                self.recover()
                with self.db:
                    self.set("blocked", stop)
            finally:
                with self.db:
                    self.db.execute(
                        "UPDATE sessions SET ended_at=?,elapsed=?,reason=? WHERE id=?",
                        (clock().isoformat(), monotonic() - started, stop, session),
                    )
                    self.set("last_stop_reason", stop)
                self.export()
            return self.report()

    def report(self):
        records = self.records()
        sessions = [dict(row) for row in self.db.execute("SELECT * FROM sessions ORDER BY id")]
        report = {
            "live_accuracy_independently_verified": False,
            "blocked": self.setting("blocked"),
            "last_stop_reason": self.setting("last_stop_reason", "not_started"),
            "classification_reviews": self.reviews(),
            "stages": {},
        }
        for stage, planned in STAGES.items():
            rows = [r for r in records if r["stage"] == stage]
            times = sorted(r["latency_seconds"] for r in rows if r.get("latency_seconds") is not None)
            stage_sessions = [s for s in sessions if s["stage"] == stage]
            runtime = sum(s["elapsed"] or 0 for s in stage_sessions)
            counts = Counter(r["classification"] for r in rows)
            report["stages"][stage] = {
                "planned": planned,
                "attempted_unique": len(rows),
                "completed_responses": sum(bool(r.get("completed_response")) for r in rows),
                "classification_counts": {key: counts[key] for key in CLASSES},
                "http_status_counts": dict(Counter(str(r.get("http_status")) for r in rows)),
                "mean_latency_seconds": statistics.mean(times) if times else None,
                "p95_latency_seconds": times[math.ceil(len(times) * 0.95) - 1] if times else None,
                "runtime_seconds_including_pacing": runtime,
                "runtime_incomplete": any(s["elapsed"] is None for s in stage_sessions),
                "attempts_per_minute": (
                    len(rows) * 60 / runtime
                    if runtime and all(s["elapsed"] is not None for s in stage_sessions)
                    else None
                ),
                "unknown_reasons": dict(
                    Counter(r["reason"] for r in rows if r["classification"] == "Unknown")
                ),
                "stopping_reason": stage_sessions[-1]["reason"] if stage_sessions else "not_started",
                "rate_limit_headers": [
                    r.get("headers", {})
                    for r in rows
                    if any("rate" in key or key == "retry-after" for key in r.get("headers", {}))
                ],
                "sessions": stage_sessions,
            }
        return report

    def export(self):
        fields = [
            "username",
            "stage",
            "request_at",
            "http_status",
            "headers",
            "raw_response_redacted",
            "raw_sha256",
            "parsed_fields",
            "classification",
            "reason",
            "latency_seconds",
            "error",
            "completed_response",
            "delivery_uncertain",
            "preliminary",
            "independently_verified",
            "requested_delay",
            "original_classification",
            "original_reason",
            "reviewed_at",
        ]
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in self.records():
            for key in ("headers", "parsed_fields"):
                record[key] = json.dumps(record.get(key), ensure_ascii=False)
            writer.writerow(record)
        atomic_text(self.folder / "results.csv", stream.getvalue())
        report = self.report()
        atomic_text(self.folder / "report.json", json.dumps(report, indent=2, ensure_ascii=False))
        for stage in STAGES:
            atomic_text(
                self.folder / f"stage-{stage}-report.json", json.dumps(report["stages"][stage], indent=2)
            )


def prepare(folder):
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        raise ValueError("Preparation requires an empty output directory; existing evidence is never reset")
    rows = manifest_rows()
    raw = json.dumps(
        {
            "endpoint_hypothesis": ENDPOINT,
            "method_hypothesis": "GET",
            "syntax_scope": "change-handle: 5-15 ASCII letters, digits, underscore",
            "candidates": rows,
        },
        indent=2,
    ).encode("utf-8")
    atomic_text(folder / "manifest.json", raw.decode())
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(folder / "manifest.csv", stream.getvalue())
    manual = io.StringIO(newline="")
    writer = csv.writer(manual)
    writer.writerow(["username", "stage", "official_interface_result", "observed_at_utc", "notes"])
    writer.writerows((r["username"], r["stage"], "NotChecked", "", "") for r in rows)
    atomic_text(folder / "manual-confirmation.csv", manual.getvalue())
    experiment = Experiment(folder)
    try:
        experiment.db.executescript("""
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE candidates(position INTEGER PRIMARY KEY,stage INTEGER,
                                    username TEXT COLLATE NOCASE UNIQUE,
                                    category TEXT,hypothesis TEXT);
            CREATE TABLE outcomes(position INTEGER PRIMARY KEY,stage INTEGER,state TEXT,record TEXT);
            CREATE TABLE sessions(id INTEGER PRIMARY KEY,stage INTEGER,started_at TEXT,ended_at TEXT,
                                  delay REAL,timeout REAL,elapsed REAL,reason TEXT);
        """)
        with experiment.db:
            experiment.db.executemany(
                "INSERT INTO candidates VALUES(?,?,?,?,?)",
                [
                    tuple(r[k] for k in ("position", "stage", "username", "category", "hypothesis"))
                    for r in rows
                ],
            )
            experiment.set("manifest_sha256", hashlib.sha256(raw).hexdigest())
        experiment.validate()
        experiment.export()
    finally:
        experiment.close()


def main():
    parser = argparse.ArgumentParser(description="Isolated X research; preliminary indications only")
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "run",
            "report",
            "review-stage1",
            "review-stage2",
            "review-stage2-improper-format",
            "review-stage2-invalid-username",
        ),
    )
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "output")
    parser.add_argument("--stage", type=int, choices=STAGES)
    parser.add_argument(
        "--delay", type=float, default=5.0, help="Post-response delay; minimum/default 5 seconds"
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--resume-reviewed-is-banned-word",
        action="store_true",
        help="Acknowledge preliminary Reserved correction and resume only pending Stage 1 names",
    )
    parser.add_argument(
        "--resume-reviewed-contains-banned-word",
        action="store_true",
        help="Acknowledge preliminary Reserved correction and resume only pending Stage 2 names",
    )
    parser.add_argument(
        "--resume-reviewed-improper-format",
        action="store_true",
        help="Acknowledge Invalid syntax correction and resume only the seven pending Stage 2 names",
    )
    parser.add_argument(
        "--resume-reviewed-invalid-username",
        action="store_true",
        help="Acknowledge the observed short-length rejection and resume pending Stage 2 names only",
    )
    args = parser.parse_args()
    if args.resume_reviewed_is_banned_word and (args.action != "run" or args.stage != 1):
        parser.error("--resume-reviewed-is-banned-word requires run --stage 1")
    if args.resume_reviewed_contains_banned_word and (args.action != "run" or args.stage != 2):
        parser.error("--resume-reviewed-contains-banned-word requires run --stage 2")
    if args.resume_reviewed_improper_format and (
        args.action != "run" or args.stage != 2 or args.resume_reviewed_contains_banned_word
    ):
        parser.error("--resume-reviewed-improper-format requires run --stage 2 and no other review flag")
    if args.resume_reviewed_invalid_username and (
        args.action != "run"
        or args.stage != 2
        or args.resume_reviewed_improper_format
        or args.resume_reviewed_contains_banned_word
    ):
        parser.error("--resume-reviewed-invalid-username requires run --stage 2 and no other review flag")
    try:
        if args.action == "prepare":
            prepare(args.output)
            print("Prepared exactly 100 candidates; zero live requests.")
            return 0
        if not (args.output / "evidence.sqlite3").is_file():
            raise ValueError("Prepare the candidate manifest first")
        experiment = Experiment(args.output)
        try:
            if args.action == "run":
                if args.stage is None:
                    parser.error("run requires an explicit --stage")
                report = experiment.run(
                    args.stage,
                    delay=args.delay,
                    timeout=args.timeout,
                    resume_reviewed_is_banned_word=args.resume_reviewed_is_banned_word,
                    resume_reviewed_contains_banned_word=args.resume_reviewed_contains_banned_word,
                    resume_reviewed_improper_format=args.resume_reviewed_improper_format,
                    resume_reviewed_invalid_username=args.resume_reviewed_invalid_username,
                )
            elif args.action == "review-stage2-invalid-username":
                with exclusive(experiment.folder):
                    experiment.validate()
                    print(json.dumps(experiment.review_stage2_invalid_username(), indent=2))
                return 0
            elif args.action == "review-stage2-improper-format":
                with exclusive(experiment.folder):
                    experiment.validate()
                    print(json.dumps(experiment.review_stage2_improper_format(), indent=2))
                return 0
            elif args.action in ("review-stage1", "review-stage2"):
                with exclusive(experiment.folder):
                    experiment.validate()
                    print(json.dumps(experiment.review_stage(int(args.action[-1])), indent=2))
                return 0
            else:
                with exclusive(experiment.folder):
                    experiment.validate()
                    experiment.recover()
                    experiment.export()
                    report = experiment.report()
            print(json.dumps(report, indent=2))
            return 2 if report["blocked"] else 0
        finally:
            experiment.close()
    except KeyboardInterrupt:
        print("Cancelled; saved evidence is preserved. Inspect report before further commands.")
        return 130
    except (ValueError, OSError, sqlite3.Error) as error:
        print("Stopped: " + str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
