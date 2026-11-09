"""Optional offline file selection. No provider, production database or job integration."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import threading
import unicodedata
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .result_search import ResultFilter, predicate, register_functions
from .roblox import input_spec, input_values, syntax_error


class Cancelled(Exception):
    pass


def spreadsheet_cell(value):
    """Return display text and an explicit JSON-encoding flag; never modify canonical evidence."""
    if (
        isinstance(value, str)
        and value
        and (value[0] in "=+-@'" or value[0].isspace() or unicodedata.category(value[0]).startswith("C"))
    ):
        # A literal JSON string begins with a double quote, not a spreadsheet formula prefix.
        # ASCII escaping also makes hidden controls visible. CSV quoting alone is insufficient.
        return json.dumps(value, ensure_ascii=True), True
    return value, False


def spreadsheet_row(row, fields):
    """Append the names of JSON-encoded fields, so decoding cannot confuse ordinary JSON-like text."""
    display, encoded = [], []
    for name, value in zip(fields, row, strict=True):
        cell, transformed = spreadsheet_cell(value)
        display.append(cell)
        if transformed:
            encoded.append(name)
    return [*display, json.dumps(encoded)]


@dataclass(frozen=True)
class Selection:
    budget: int | None = None
    prioritize: bool = False
    shorter_first: bool = False
    fewer_digits_first: bool = False
    filters: ResultFilter = field(default_factory=ResultFilter)
    preferred: ResultFilter = field(default_factory=ResultFilter)

    def validate(self):
        if self.budget is not None and (type(self.budget) is not int or self.budget < 1):
            raise ValueError("Candidate budget must be a positive integer")
        for flag in (self.prioritize, self.shorter_first, self.fewer_digits_first):
            if type(flag) is not bool:
                raise ValueError("Priority switches must be boolean")
        for rule in (self.filters, self.preferred):
            if not isinstance(rule, ResultFilter) or not isinstance(rule.text, str) or len(rule.text) > 256:
                raise ValueError("Use existing local filters with text up to 256 characters")
            rule.validate()
            if rule.classification or rule.changed:
                raise ValueError("Provider classifications and historical changes are not local criteria")
        return self

    @classmethod
    def from_data(cls, data):
        data = dict(data)
        for key in ("filters", "preferred"):
            data[key] = ResultFilter(**data.get(key, {}))
        return cls(**data).validate()

    @property
    def order(self):
        fields = ["priority"] if self.prioritize else []
        if self.prioritize and self.shorter_first:
            fields.append("size")
        if self.prioritize and self.fewer_digits_first:
            fields.append("digits")
        return ",".join([*fields, "ordinal"])


def fingerprint(path, cancel):
    if cancel.is_set():
        raise Cancelled("Cancelled before checking source")
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if cancel.is_set():
                raise Cancelled("Cancelled while checking source; existing checkpoints retained")
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def exclusive(directory):
    # OS releases this local-only lock after crashes. Never use the provider activity lock.
    with (directory / "selection.lock").open("a+b") as stream:
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


class LocalSelection:
    def __init__(self, directory, *, cancel=None, notify=lambda _: None):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "selection.sqlite3"
        self.cancel = cancel or threading.Event()
        self.notify = notify

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=0)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA cache_size=-4096")
            db.execute("PRAGMA temp_store=FILE")
            db.execute("PRAGMA mmap_size=0")
            register_functions(db)
            db.set_progress_handler(lambda: int(self.cancel.is_set()), 2000)
            try:
                with db:
                    yield db
            except sqlite3.OperationalError as exc:
                if self.cancel.is_set():
                    raise Cancelled("Cancelled; last committed checkpoint retained") from exc
                raise
            finally:
                db.set_progress_handler(None, 0)

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled("Cancelled; last committed checkpoint retained")

    def create(self, source, selection=None):
        selection = selection or Selection()
        selection.validate()
        source = Path(source).resolve()
        if source.suffix.lower() not in {".txt", ".csv"}:
            raise ValueError("Choose a UTF-8 TXT or CSV file")
        spec = input_spec(files=(source,))
        digest = fingerprint(source, self.cancel)
        # Only an entirely new directory is accepted; no original or prior output is overwritten.
        self.directory.mkdir(parents=False, exist_ok=False)
        with exclusive(self.directory), self.connect() as db:
            db.executescript(
                "CREATE TABLE meta(value TEXT NOT NULL);"
                "CREATE TABLE records(ordinal INTEGER PRIMARY KEY, key TEXT UNIQUE, raw TEXT, "
                "username TEXT, first_record INTEGER, category TEXT, reason TEXT, "
                "priority INTEGER DEFAULT 0, size INTEGER DEFAULT 0, digits INTEGER DEFAULT 0);"
            )
            db.execute(
                "INSERT INTO meta VALUES(?)",
                (
                    json.dumps(
                        {
                            "version": 1,
                            "source": spec,
                            "sha256": digest,
                            "selection": asdict(selection),
                            "cursor": 0,
                            "phase": "Importing",
                        }
                    ),
                ),
            )
        return self

    @staticmethod
    def meta(db):
        row = db.execute("SELECT value FROM meta").fetchone()
        if not row:
            raise ValueError("Incomplete local selection; original source is unchanged")
        meta = json.loads(row[0])
        if meta["version"] != 1:
            raise ValueError("Unsupported local selection version")
        return meta

    @staticmethod
    def save(db, meta):
        db.execute("UPDATE meta SET value=?", (json.dumps(meta),))

    def prepare(self):
        if not self.path.is_file():
            raise ValueError("Local selection does not exist")
        with exclusive(self.directory), self.connect() as db:
            meta = self.meta(db)
            config = Selection.from_data(meta["selection"])
            source = meta["source"]["files"][0]["path"]
            if fingerprint(source, self.cancel) != meta["sha256"]:
                raise ValueError("Source changed; no checkpoint or record was modified")
            if meta["phase"] == "Importing":
                cursor = meta["cursor"]
                with closing(input_values(meta["source"])) as values:
                    for ordinal, raw in enumerate(values, 1):
                        self.check_cancel()
                        if ordinal <= cursor:
                            continue
                        name = raw.strip()
                        reason = syntax_error(name) if name else "Empty input record"
                        category = "LocalInvalid" if name else "Blank"
                        key = ("invalid:" if reason else "valid:") + name.casefold() if name else None
                        inserted = db.execute(
                            "INSERT OR IGNORE INTO records(ordinal,key,raw,username,first_record,"
                            "category,reason) VALUES(?,?,?,?,?,?,?)",
                            (
                                ordinal,
                                key,
                                raw,
                                name,
                                ordinal,
                                category if reason else "LowerPriorityValid",
                                reason or "",
                            ),
                        ).rowcount
                        if not inserted:
                            first = db.execute("SELECT ordinal FROM records WHERE key=?", (key,)).fetchone()[
                                0
                            ]
                            db.execute(
                                "INSERT INTO records(ordinal,raw,username,first_record,category,reason) "
                                "VALUES(?,?,?,?,?,?)",
                                (ordinal, raw, name, first, "Duplicate", "Case-insensitive exact duplicate"),
                            )
                        meta["cursor"] = ordinal
                        if ordinal % 1000 == 0:
                            self.save(db, meta)
                            db.commit()
                            self.notify({"phase": "Importing", "records": ordinal})
                meta["phase"] = "Selecting"
                self.save(db, meta)
                db.commit()
                self.notify({"phase": "Selecting", "records": meta["cursor"]})
            if meta["phase"] == "Selecting":
                self.check_cancel()
                allowed, args = predicate(config.filters)
                db.execute(
                    "UPDATE records AS c SET category='UserExcluded',reason='User local filter' "
                    "WHERE category='LowerPriorityValid' AND NOT (" + allowed + ")",
                    args,
                )
                if config.prioritize:
                    preferred, args = predicate(config.preferred)
                    db.execute(
                        "UPDATE records AS c SET priority=CASE WHEN (" + preferred + ") THEN 0 ELSE 1 END,"
                        "size=length(username),digits=name_digits(username) "
                        "WHERE category='LowerPriorityValid'",
                        args,
                    )
                # SQLite sorts on disk with a bounded cache. No million-element Python list or heap.
                db.execute(
                    "UPDATE records SET category='SelectedValid' WHERE ordinal IN "
                    "(SELECT ordinal FROM records WHERE category='LowerPriorityValid' ORDER BY "
                    + config.order
                    + " LIMIT ?)",
                    (config.budget if config.budget is not None else -1,),
                )
                meta["phase"] = "Ready"
                self.save(db, meta)
                db.commit()
                self.notify({"phase": "Ready", "records": meta["cursor"]})
        return self.summary()

    def summary(self):
        with self.connect() as db:
            meta = self.meta(db)
            counts = {
                key: 0
                for key in (
                    "SelectedValid",
                    "LowerPriorityValid",
                    "UserExcluded",
                    "LocalInvalid",
                    "Duplicate",
                    "Blank",
                )
            }
            counts.update(dict(db.execute("SELECT category,count(*) FROM records GROUP BY category")))
            eligible = counts["SelectedValid"] + counts["LowerPriorityValid"]
            return {
                **meta,
                "counts": counts,
                "eligible": eligible,
                "budget_examples": {str(n): min(n, eligible) for n in (10000, 50000, 100000)},
                "provider_results": False,
                "availability_verified": False,
            }

    def export(self, destination):
        """Export to a NEW directory. Interrupted exports remain explicitly incomplete and immutable."""
        destination = Path(destination).resolve()
        with exclusive(self.directory), self.connect() as db:
            self.check_cancel()
            meta = self.meta(db)
            if meta["phase"] != "Ready":
                raise ValueError("Finish local preparation before exporting")
            config = Selection.from_data(meta["selection"])
            destination.mkdir(parents=False, exist_ok=False)
            evidence = {}
            queries = {
                "audit.csv": "SELECT ordinal,raw,username,first_record,category,reason "
                "FROM records ORDER BY ordinal",
                "selected.txt": "SELECT username FROM records WHERE category='SelectedValid' ORDER BY "
                + config.order,
                "retained.txt": "SELECT username FROM records WHERE category IN "
                "('LowerPriorityValid','UserExcluded') ORDER BY ordinal",
            }
            for filename, query in queries.items():
                count = 0
                fields = (
                    "source_record",
                    "original_value",
                    "username",
                    "first_record",
                    "local_category",
                    "reason",
                )
                with (destination / filename).open("x", encoding="utf-8", newline="") as stream:
                    writer = csv.writer(stream) if filename.endswith("csv") else None
                    if writer:
                        writer.writerow((*fields, "csv_json_fields"))
                    for row in db.execute(query):
                        self.check_cancel()
                        if writer:
                            writer.writerow(spreadsheet_row(row, fields))
                        else:
                            stream.write(row[0] + "\n")
                        count += 1
                        if count % 1000 == 0:
                            self.notify({"phase": "Exporting", "file": filename, "records": count})
                    stream.flush()
                    os.fsync(stream.fileno())
                evidence[filename] = {
                    "rows": count,
                    "sha256": fingerprint(destination / filename, self.cancel),
                }
            # Only this final durable marker authorizes using exported candidates. No provider is invoked.
            report = {
                **self.summary(),
                "complete": True,
                "files": evidence,
                "audit_csv": {
                    "version": 2,
                    "encoding": "JSON string literals for sensitive cells; others unchanged",
                    "decode": "json.loads fields named in json.loads(csv_json_fields); absent means v1 raw",
                    "canonical_evidence": "Unmodified source (sha256) and selection.sqlite3 records",
                },
                "note": "Local preferences only, not availability. Source records are logical input values.",
            }
            self.check_cancel()
            temporary = destination / "summary.json.part"
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, destination / "summary.json")
            return report
