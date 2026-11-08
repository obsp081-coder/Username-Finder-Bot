from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .domain import RULES_VERSION, Classification, Evidence, JobState, SearchConfig, syntax_error, utc_now
from .roblox_storage import initialize as initialize_roblox
from .sources import InputRecord

SCHEMA_VERSION = 2
COUNTERS = (
    "lines",
    "blank",
    "malformed",
    "invalid",
    "filtered",
    "duplicate",
    "unique",
    "skipped",
    "dispatched",
    "completed",
    "retries",
)


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    """One serialized writer, short FULL/WAL transactions, independent paged readers."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1, SCHEMA_VERSION}:
            self.db.close()
            raise ValueError("إصدار قاعدة البيانات غير مدعوم؛ لم تُعدّل البيانات.")
        if version == 1:
            self.backup(self.path.parent / "Backups" / f"before-schema-2-{uuid4().hex}.db")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                config TEXT NOT NULL, snapshot TEXT NOT NULL, cursor TEXT NOT NULL DEFAULT '{}',
                counters TEXT NOT NULL, state TEXT NOT NULL, source_done INTEGER NOT NULL DEFAULT 0,
                active_seconds REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                schedule TEXT NOT NULL DEFAULT '{}', rules_version TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), username TEXT NOT NULL,
                state TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0,
                result_id INTEGER REFERENCES attempts(id), UNIQUE(job_id, username)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
                username TEXT NOT NULL, classification TEXT NOT NULL, kind TEXT NOT NULL,
                provider TEXT NOT NULL, provider_version TEXT NOT NULL, mode TEXT NOT NULL,
                trustworthy INTEGER NOT NULL, checked_at TEXT NOT NULL, latency_ms REAL NOT NULL,
                error TEXT NOT NULL, trace_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS username_state (
                username TEXT NOT NULL, mode TEXT NOT NULL,
                latest_id INTEGER NOT NULL REFERENCES attempts(id),
                trusted_id INTEGER REFERENCES attempts(id), PRIMARY KEY(username, mode)
            );
            CREATE TABLE IF NOT EXISTS quarantined_providers (
                provider TEXT NOT NULL, version TEXT NOT NULL, PRIMARY KEY(provider, version)
            );
            CREATE INDEX IF NOT EXISTS task_queue ON tasks(job_id, state, id);
            CREATE INDEX IF NOT EXISTS attempt_task ON attempts(task_id, id);
        """)
        self._migrate_presets()
        initialize_roblox(self)

    def _migrate_presets(self):
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS presets (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            db.execute("PRAGMA user_version=2")

    def presets(self) -> list[dict]:
        with self.lock:
            return [
                dict(row)
                for row in self.db.execute("SELECT id,name,updated_at FROM presets ORDER BY name_key,id")
            ]

    def preset(self, preset_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM presets WHERE id=?", (preset_id,)).fetchone()
        if row is None:
            raise ValueError("القالب غير موجود.")
        try:
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            from .presets import validate_payload

            validate_payload(result["payload"])
            return result
        except (ValueError, TypeError) as exc:
            raise ValueError("تعذر قراءة القالب؛ بياناته تالفة أو إصدارها غير مدعوم.") from exc

    @staticmethod
    def _preset_name(name: str) -> tuple[str, str]:
        name = name.strip()
        if not name or len(name) > 100 or any(ord(c) < 32 for c in name):
            raise ValueError("اكتب اسمًا للقالب من 1 إلى 100 حرف دون أسطر جديدة.")
        return name, name.casefold()

    def save_preset(self, name: str, payload: dict, preset_id: str | None = None) -> str:
        from .presets import validate_payload

        validate_payload(payload)
        name, key = self._preset_name(name)
        identity = preset_id or uuid4().hex
        now = utc_now()
        try:
            with self.transaction() as db:
                if preset_id:
                    if not db.execute("SELECT 1 FROM presets WHERE id=?", (preset_id,)).fetchone():
                        raise ValueError("القالب غير موجود.")
                    db.execute(
                        "UPDATE presets SET name=?,name_key=?,payload=?,updated_at=? WHERE id=?",
                        (name, key, dumps(payload), now, identity),
                    )
                else:
                    db.execute(
                        "INSERT INTO presets VALUES(?,?,?,?,?,?)",
                        (identity, name, key, dumps(payload), now, now),
                    )
        except sqlite3.IntegrityError as exc:
            raise ValueError("يوجد قالب بهذا الاسم؛ اختر اسمًا آخر.") from exc
        return identity

    def rename_preset(self, preset_id: str, name: str):
        name, key = self._preset_name(name)
        try:
            with self.transaction() as db:
                if not db.execute(
                    "UPDATE presets SET name=?,name_key=?,updated_at=? WHERE id=?",
                    (name, key, utc_now(), preset_id),
                ).rowcount:
                    raise ValueError("القالب غير موجود.")
        except sqlite3.IntegrityError as exc:
            raise ValueError("يوجد قالب بهذا الاسم؛ اختر اسمًا آخر.") from exc

    def delete_preset(self, preset_id: str):
        with self.transaction() as db:
            db.execute("DELETE FROM presets WHERE id=?", (preset_id,))

    def close(self):
        with self.lock:
            self.db.close()

    @contextmanager
    def transaction(self, *, timeout_ms=None):
        with self.lock:
            timeout = self.db.execute("PRAGMA busy_timeout").fetchone()[0]
        budget = timeout if timeout_ms is None else max(0, int(timeout_ms))
        deadline = time.monotonic() + budget / 1000
        # An external writer must not hold our Python mutex for the entire SQLite timeout.
        # Retry only BEGIN acquisition; transaction bodies and commits are never replayed.
        while True:
            self.lock.acquire()
            try:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                self.db.execute(f"PRAGMA busy_timeout={min(50, remaining)}")
                try:
                    self.db.execute("BEGIN IMMEDIATE")
                finally:
                    self.db.execute(f"PRAGMA busy_timeout={timeout}")
            except BaseException as exc:
                self.lock.release()
                busy = isinstance(exc, sqlite3.OperationalError) and (
                    getattr(exc, "sqlite_errorcode", 0) & 255
                ) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                if not busy or time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            else:
                break
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        finally:
            self.lock.release()

    def create_job(self, config: SearchConfig, snapshot: dict, schedule: dict | None = None) -> str:
        config.validate()
        job_id = uuid4().hex
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO jobs(id,created_at,updated_at,config,snapshot,counters,
                          state,schedule,rules_version)
                          VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    now,
                    now,
                    dumps(config.to_dict()),
                    dumps(snapshot),
                    dumps({k: "0" for k in COUNTERS}),
                    JobState.READY,
                    dumps(schedule or {}),
                    RULES_VERSION,
                ),
            )
        return job_id

    def job(self, job_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("عملية البحث غير موجودة.")
        result = dict(row)
        for key in ("config", "snapshot", "cursor", "counters", "schedule"):
            result[key] = json.loads(result[key])
        result["counters"] = {k: int(v) for k, v in result["counters"].items()}
        return result

    def jobs(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT id,created_at,state,active_seconds,error,config FROM jobs "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ]

    def set_state(self, job_id: str, state: JobState, error: str = "", active_seconds: float | None = None):
        with self.transaction() as db:
            if active_seconds is None:
                db.execute(
                    "UPDATE jobs SET state=?, error=?, updated_at=? WHERE id=?",
                    (state, error, utc_now(), job_id),
                )
            else:
                db.execute(
                    "UPDATE jobs SET state=?,error=?,active_seconds=?,updated_at=? WHERE id=?",
                    (state, error, active_seconds, utc_now(), job_id),
                )

    def recover(self):
        with self.transaction() as db:
            db.execute("UPDATE tasks SET state='Interrupted' WHERE state IN ('Checking','RetryWaiting')")
            db.execute("UPDATE jobs SET state=? WHERE state=?", (JobState.INTERRUPTED, JobState.RUNNING))

    def update_active(self, job_id: str, seconds: float):
        with self.transaction() as db:
            db.execute("UPDATE jobs SET active_seconds=? WHERE id=?", (seconds, job_id))

    def update_schedule(self, job_id: str, schedule: dict):
        with self.transaction() as db:
            db.execute("UPDATE jobs SET schedule=? WHERE id=?", (dumps(schedule), job_id))

    def begin_job(self, job_id: str):
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM jobs WHERE state=?", (JobState.RUNNING,)).fetchone():
                return False
            db.execute(
                "UPDATE tasks SET state='Interrupted' WHERE job_id=? "
                "AND state IN ('Checking','RetryWaiting')",
                (job_id,),
            )
            db.execute("UPDATE jobs SET state=?,error='' WHERE id=?", (JobState.RUNNING, job_id))
            return True

    @staticmethod
    def _save_counters(db, job_id, counters, cursor=None):
        if cursor is None:
            db.execute(
                "UPDATE jobs SET counters=?,updated_at=? WHERE id=?",
                (dumps({k: str(v) for k, v in counters.items()}), utc_now(), job_id),
            )
        else:
            db.execute(
                "UPDATE jobs SET counters=?,cursor=?,updated_at=? WHERE id=?",
                (dumps({k: str(v) for k, v in counters.items()}), dumps(cursor), utc_now(), job_id),
            )

    def _trusted(self, db, username: str, mode: str):
        return db.execute(
            """SELECT a.* FROM username_state s JOIN attempts a ON a.id=s.trusted_id
               LEFT JOIN quarantined_providers q ON q.provider=a.provider AND q.version=a.provider_version
               WHERE s.username=? AND s.mode=? AND q.provider IS NULL""",
            (username, mode),
        ).fetchone()

    def _skip(self, db, username: str, mode: str, config: SearchConfig, provider: str, version: str) -> bool:
        if not config.smart_history:
            return False
        evidence = self._trusted(db, username, mode)
        if not evidence:
            return False
        if evidence["classification"] == Classification.INVALID and evidence["provider"] == "local-syntax":
            return evidence["provider_version"] == RULES_VERSION
        if (evidence["provider"], evidence["provider_version"]) != (provider, version):
            return False
        if evidence["classification"] == Classification.INVALID:
            return True  # Provider version identifies the frozen validation semantics.
        if evidence["classification"] not in {Classification.TAKEN, Classification.RESERVED}:
            return False
        age = (datetime.now(UTC) - datetime.fromisoformat(evidence["checked_at"])).total_seconds()
        return 0 <= age < config.history_ttl_hours * 3600

    def ingest(
        self, job_id: str, record: InputRecord, config: SearchConfig, mode: str, provider: str, version: str
    ) -> str:
        """Task creation/exclusion and source advancement commit together."""
        with self.transaction() as db:
            job = db.execute("SELECT counters FROM jobs WHERE id=?", (job_id,)).fetchone()
            counters = {k: int(v) for k, v in json.loads(job[0]).items()}
            counters["lines"] += 1
            name = record.username
            if record.kind in {"blank", "malformed"}:
                outcome = record.kind
            elif syntax_error(name):
                outcome = "invalid"
            elif not config.filters.matches(name):
                outcome = "filtered"
            else:
                outcome = "candidate"
            if outcome in {"candidate", "invalid"}:
                exists = db.execute(
                    "SELECT id FROM tasks WHERE job_id=? AND username=?", (job_id, name)
                ).fetchone()
                if exists:
                    outcome = "duplicate"
                else:
                    pending = db.execute(
                        "SELECT COUNT(*) FROM tasks WHERE job_id=? AND state NOT IN ('Completed','Skipped')",
                        (job_id,),
                    ).fetchone()[0]
                    if pending >= config.queue_size:
                        raise BufferError("Durable pending-task bound reached")
                    skipped = self._skip(db, name, mode, config, provider, version)
                    task_id = db.execute(
                        "INSERT INTO tasks(job_id,username,state,metadata) VALUES(?,?,?,?)",
                        (job_id, name, "Skipped" if skipped else "Pending", dumps(record.metadata or {})),
                    ).lastrowid
                    if outcome == "candidate":
                        counters["unique"] += 1
                    if skipped:
                        if outcome == "invalid":
                            counters["invalid"] += 1
                        outcome = "skipped"
                    elif outcome == "invalid":
                        from .providers import local_invalid

                        self._record_attempt(db, task_id, name, local_invalid(mode), completed=True)
                        counters["completed"] += 1
            if outcome != "candidate":
                counters[outcome] += 1
            self._save_counters(db, job_id, counters, record.cursor)
            return outcome

    def source_done(self, job_id: str):
        with self.transaction() as db:
            db.execute("UPDATE jobs SET source_done=1 WHERE id=?", (job_id,))

    def pending(self, job_id: str, limit: int = 128) -> list[dict]:
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    """SELECT * FROM tasks WHERE job_id=?
                AND state IN ('Pending','Interrupted') ORDER BY id LIMIT ?""",
                    (job_id, limit),
                )
            ]

    def pending_count(self, job_id: str) -> int:
        with self.lock:
            return self.db.execute(
                """SELECT COUNT(*) FROM tasks WHERE job_id=?
                AND state NOT IN ('Completed','Skipped')""",
                (job_id,),
            ).fetchone()[0]

    def claim(self, task_id: int):
        with self.transaction() as db:
            row = db.execute("SELECT job_id,state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row["state"] not in {"Pending", "Interrupted", "RetryWaiting"}:
                raise ValueError("Task cannot be claimed twice")
            db.execute("UPDATE tasks SET state='Checking',attempts=attempts+1 WHERE id=?", (task_id,))
            counters = {
                k: int(v)
                for k, v in json.loads(
                    db.execute("SELECT counters FROM jobs WHERE id=?", (row["job_id"],)).fetchone()[0]
                ).items()
            }
            counters["dispatched"] += 1
            self._save_counters(db, row["job_id"], counters)

    def interrupt(self, task_id: int):
        with self.transaction() as db:
            db.execute("UPDATE tasks SET state='Interrupted' WHERE id=? AND state!='Completed'", (task_id,))

    @staticmethod
    def _record_attempt(db, task_id: int, username: str, evidence: Evidence, completed: bool) -> int:
        evidence.validate()
        values = asdict(evidence)
        fields = (
            "classification",
            "kind",
            "provider",
            "provider_version",
            "mode",
            "trustworthy",
            "checked_at",
            "latency_ms",
            "error",
            "trace_id",
        )
        result_id = db.execute(
            """INSERT INTO attempts(task_id,username,classification,kind,provider,
            provider_version,mode,trustworthy,checked_at,latency_ms,error,trace_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, username, *(values[k] for k in fields)),
        ).lastrowid
        trusted = result_id if evidence.trustworthy else None
        db.execute(
            """INSERT INTO username_state(username,mode,latest_id,trusted_id) VALUES(?,?,?,?)
            ON CONFLICT(username,mode) DO UPDATE SET latest_id=excluded.latest_id,
            trusted_id=COALESCE(excluded.trusted_id,username_state.trusted_id)""",
            (username, evidence.mode, result_id, trusted),
        )
        db.execute(
            "UPDATE tasks SET state=?,result_id=? WHERE id=?",
            ("Completed" if completed else "RetryWaiting", result_id, task_id),
        )
        return result_id

    def finish(self, task_id: int, evidence: Evidence, *, completed: bool = True):
        with self.transaction() as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task["state"] == "Completed":
                raise ValueError("Task already completed")
            self._record_attempt(db, task_id, task["username"], evidence, completed)
            counters = {
                k: int(v)
                for k, v in json.loads(
                    db.execute("SELECT counters FROM jobs WHERE id=?", (task["job_id"],)).fetchone()[0]
                ).items()
            }
            counters["completed" if completed else "retries"] += 1
            self._save_counters(db, task["job_id"], counters)

    def quarantine(self, provider: str, version: str):
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO quarantined_providers VALUES(?,?)", (provider, version))

    def provider_quarantined(self, provider: str, version: str) -> bool:
        with self.lock:
            return (
                self.db.execute(
                    "SELECT 1 FROM quarantined_providers WHERE provider=? AND version=?", (provider, version)
                ).fetchone()
                is not None
            )

    def results(
        self,
        job_id: str | None = None,
        *,
        mode: str,
        classification: str = "",
        search: str = "",
        limit: int = 100,
        offset: int = 0,
        trusted: bool = False,
        sort: str = "oldest",
    ) -> list[dict]:
        where = ["a.mode=?"]
        args: list = [mode]
        if job_id:
            join = "JOIN tasks t ON t.result_id=a.id"
            where.extend(["t.job_id=?", "t.state='Completed'"])
            args.append(job_id)
        else:
            field = "trusted_id" if trusted else "latest_id"
            join = f"JOIN username_state s ON s.{field}=a.id"
        if classification:
            where.append("CASE WHEN q.provider IS NULL THEN a.classification ELSE 'Unknown' END=?")
            args.append(classification)
        if search:
            where.append("instr(a.username,?)>0")
            args.append(search)
        if trusted:
            where.extend(["a.trustworthy=1", "q.provider IS NULL"])
        orders = {
            "oldest": "a.id",
            "newest": "a.id DESC",
            "name": "a.username,a.id",
            "name_desc": "a.username DESC,a.id DESC",
        }
        if sort not in orders:
            raise ValueError("ترتيب غير مدعوم.")
        sql = f"""SELECT a.*,CASE WHEN q.provider IS NULL THEN 0 ELSE 1 END AS quarantined FROM attempts a
            {join} LEFT JOIN quarantined_providers q ON q.provider=a.provider AND q.version=a.provider_version
            WHERE {" AND ".join(where)} ORDER BY {orders[sort]} LIMIT ? OFFSET ?"""
        with self.lock:
            results = [dict(r) for r in self.db.execute(sql, (*args, limit, offset))]
            for row in results:
                row["effective_classification"] = "Unknown" if row["quarantined"] else row["classification"]
                row["trustworthy"] = bool(row["trustworthy"] and not row["quarantined"])
            return results

    def result_page(
        self, job_id: str, *, mode: str, classification="", search="", offset=0, limit=100, sort="oldest"
    ) -> dict:
        where = ["t.job_id=?", "t.state='Completed'", "a.mode=?"]
        args = [job_id, mode]
        if classification:
            where.append("CASE WHEN q.provider IS NULL THEN a.classification ELSE 'Unknown' END=?")
            args.append(classification)
        if search:
            where.append("instr(a.username,?)>0")
            args.append(search)
        with self.lock:
            total = self.db.execute(
                f"""SELECT COUNT(*) FROM tasks t JOIN attempts a ON a.id=t.result_id
                LEFT JOIN quarantined_providers q ON q.provider=a.provider AND q.version=a.provider_version
                WHERE {" AND ".join(where)}""",
                args,
            ).fetchone()[0]
            offset = min(max(0, offset), max(0, ((total - 1) // limit) * limit))
            rows = self.results(
                job_id,
                mode=mode,
                classification=classification,
                search=search,
                offset=offset,
                limit=limit,
                sort=sort,
            )
        return {"rows": rows, "total": total, "offset": offset}

    def result_counts(self, job_id: str, mode: str) -> dict:
        with self.lock:
            rows = self.db.execute(
                """SELECT CASE WHEN q.provider IS NULL THEN a.classification
                ELSE 'Unknown' END AS effective,COUNT(*) FROM tasks t JOIN attempts a ON a.id=t.result_id
                LEFT JOIN quarantined_providers q ON q.provider=a.provider AND q.version=a.provider_version
                WHERE t.job_id=? AND t.state='Completed' AND a.mode=? GROUP BY effective""",
                (job_id, mode),
            )
            counts = dict(rows)
        return {status.value: counts.get(status.value, 0) for status in Classification}

    def history_count(self, mode: str, search: str = "") -> int:
        with self.lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM username_state WHERE mode=? AND instr(username,?)>0", (mode, search)
            ).fetchone()[0]

    def job_count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def history_page(self, *, mode: str, search: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    """SELECT s.username,a.classification AS latest,
                a.checked_at AS latest_at,b.classification AS trusted,b.checked_at AS trusted_at,
                a.mode,b.provider,b.provider_version,
                CASE WHEN q.provider IS NULL THEN 0 ELSE 1 END AS quarantined
                FROM username_state s JOIN attempts a ON a.id=s.latest_id
                LEFT JOIN attempts b ON b.id=s.trusted_id
                LEFT JOIN quarantined_providers q ON q.provider=b.provider AND q.version=b.provider_version
                WHERE s.mode=? AND instr(s.username,?)>0 ORDER BY s.username LIMIT ? OFFSET ?""",
                    (mode, search, limit, offset),
                )
            ]

    def export(
        self, directory: Path, *, mode: str, job_id: str | None = None, trusted: bool = False
    ) -> list[Path]:
        """One read snapshot, streamed rows, then atomic replacement of each complete file."""
        directory.mkdir(parents=True, exist_ok=True)
        scope = job_id or ("trusted-history" if trusted else "latest-history")
        prefix = f"{mode}_{scope}_"
        targets = {
            status.value: directory / f"{prefix}{status.value.lower()}.txt" for status in Classification
        }
        targets["csv"] = directory / f"{prefix}results.csv"
        temporary = {key: path.with_name(path.name + f".{uuid4().hex}.tmp") for key, path in targets.items()}
        fields = [
            "username",
            "classification",
            "effective_classification",
            "mode",
            "checked_at",
            "provider",
            "provider_version",
            "kind",
            "trustworthy",
            "quarantined",
            "latency_ms",
            "trace_id",
            "error",
        ]
        streams = {}
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN")
            where = ["a.mode=?"]
            args = [mode]
            if job_id:
                join = "JOIN tasks t ON t.result_id=a.id"
                where += ["t.job_id=?", "t.state='Completed'"]
                args.append(job_id)
            else:
                field = "trusted_id" if trusted else "latest_id"
                join = f"JOIN username_state s ON s.{field}=a.id"
            if trusted:
                where += ["a.trustworthy=1", "q.provider IS NULL"]
            rows = db.execute(
                f"""SELECT a.*,CASE WHEN q.provider IS NULL THEN 0 ELSE 1 END AS quarantined
                FROM attempts a {join} LEFT JOIN quarantined_providers q
                ON q.provider=a.provider AND q.version=a.provider_version
                WHERE {" AND ".join(where)} ORDER BY a.id""",
                args,
            )
            for key, path in temporary.items():
                streams[key] = path.open("w", encoding="utf-8-sig" if key == "csv" else "utf-8", newline="")
            writer = csv.DictWriter(streams["csv"], fieldnames=fields)
            writer.writeheader()
            for row in rows:
                effective = "Unknown" if row["quarantined"] else row["classification"]
                record = {key: row[key] for key in fields if key != "effective_classification"}
                record["effective_classification"] = effective
                record["trustworthy"] = int(row["trustworthy"] and not row["quarantined"])
                # Prevent spreadsheet formulas in untrusted imported usernames.
                for key, value in record.items():
                    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                        record[key] = "'" + value
                writer.writerow(record)
                streams[effective].write(row["username"] + "\n")
            for stream in streams.values():
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
            for key, path in targets.items():
                os.replace(temporary[key], path)
            return list(targets.values())
        finally:
            for stream in streams.values():
                stream.close()
            db.close()

    def backup(self, target: Path):
        if target.resolve() == self.path.resolve():
            raise ValueError("اختر ملف نسخ احتياطي مختلفًا عن قاعدة البيانات الحالية.")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + f".{uuid4().hex}.tmp")
        with (
            closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as src,
            closing(sqlite3.connect(temporary)) as dest,
        ):
            src.backup(dest, pages=128, sleep=0.01)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity failed")
            if dest.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Backup foreign keys failed")
        os.replace(temporary, target)

    @staticmethod
    def validate_backup(path: Path):
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in {1, SCHEMA_VERSION}:
                raise ValueError("Unsupported backup schema")
            if (
                db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or db.execute("PRAGMA foreign_key_check").fetchall()
            ):
                raise ValueError("Corrupt backup")

    def restore(self, source: Path, safety_backup: Path):
        if len({p.resolve() for p in (source, safety_backup, self.path)}) != 3:
            raise ValueError("ملف الاستعادة ونسخة الأمان والقاعدة الحالية يجب أن تكون ملفات مختلفة.")
        self.validate_backup(source)
        self.backup(safety_backup)
        with self.lock, closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            src.backup(self.db)
        self._migrate_presets()
        initialize_roblox(self)
        self.recover()

    def metrics(self, job_id: str) -> dict:
        with self.lock:
            rows = self.db.execute(
                "SELECT state,COUNT(*) FROM tasks WHERE job_id=? GROUP BY state", (job_id,)
            )
            states = dict(rows)
            trusted_count = self.db.execute(
                """SELECT COUNT(*) FROM tasks t JOIN attempts a ON t.result_id=a.id
                LEFT JOIN quarantined_providers q ON q.provider=a.provider AND q.version=a.provider_version
                WHERE t.job_id=? AND a.trustworthy=1 AND q.provider IS NULL
                AND a.classification!='Unknown' AND a.provider!='local-syntax'""",
                (job_id,),
            ).fetchone()[0]
        return {"states": states, "decisive": trusted_count}

    def health(self) -> dict:
        wal = self.path.with_name(self.path.name + "-wal")
        try:
            wal_bytes = wal.stat().st_size
        except FileNotFoundError:
            wal_bytes = 0
        return {
            "database_bytes": self.path.stat().st_size,
            "wal_bytes": wal_bytes,
            "free_bytes": shutil.disk_usage(self.path.parent).free,
        }

    def latest_unfinished(self) -> str | None:
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM jobs WHERE state!=? ORDER BY updated_at DESC LIMIT 1", (JobState.COMPLETED,)
            ).fetchone()
            return row[0] if row else None
