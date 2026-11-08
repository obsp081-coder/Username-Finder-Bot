"""Additive Roblox tables in the application's existing FULL/WAL SQLite store."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from . import roblox_protocol as protocol
from .roblox import INTERVAL_SECONDS, bounded_lines, input_values, now, result_for, syntax_error

CHECK_INTERVALS = (5.0, 2.0, 1.0, 0.5, 0.0)


def check_interval(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value not in CHECK_INTERVALS:
        raise ValueError("Unsupported Roblox check interval")
    return float(value)


def initialize(store):
    statements = (
        """CREATE TABLE IF NOT EXISTS roblox_runs (
        id TEXT PRIMARY KEY,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        kind TEXT NOT NULL,state TEXT NOT NULL,skip_previous INTEGER NOT NULL,
        skipped INTEGER NOT NULL DEFAULT 0,duplicates INTEGER NOT NULL DEFAULT 0,
        invalid INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '')""",
        """CREATE TABLE IF NOT EXISTS roblox_queue (
        run_id TEXT NOT NULL REFERENCES roblox_runs(id),position INTEGER NOT NULL,
        username TEXT NOT NULL,username_key TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'Pending',
        PRIMARY KEY(run_id,position),UNIQUE(run_id,username_key))""",
        """CREATE TABLE IF NOT EXISTS roblox_checks (
        id INTEGER PRIMARY KEY,username_key TEXT NOT NULL,username TEXT NOT NULL,
        checked_at TEXT NOT NULL,classification TEXT NOT NULL,api_code INTEGER,
        evidence TEXT NOT NULL,source TEXT NOT NULL,source_key TEXT NOT NULL UNIQUE,
        fingerprint TEXT NOT NULL,completed INTEGER NOT NULL,
        previous_classification TEXT,changed INTEGER NOT NULL DEFAULT 0,
        run_id TEXT REFERENCES roblox_runs(id))""",
        """CREATE TABLE IF NOT EXISTS roblox_names (
        username_key TEXT PRIMARY KEY,username TEXT NOT NULL,
        latest_id INTEGER NOT NULL REFERENCES roblox_checks(id),completed_checks INTEGER NOT NULL)""",
        "CREATE TABLE IF NOT EXISTS roblox_imports "
        "(digest TEXT PRIMARY KEY,source TEXT,imported_at TEXT,count INTEGER)",
        "CREATE TABLE IF NOT EXISTS roblox_control (id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL)",
        "INSERT OR IGNORE INTO roblox_control VALUES(1,'{}')",
        "CREATE INDEX IF NOT EXISTS roblox_check_name ON roblox_checks(username_key,checked_at,id)",
        "CREATE INDEX IF NOT EXISTS roblox_check_fingerprint ON roblox_checks(fingerprint)",
        "CREATE INDEX IF NOT EXISTS roblox_pending ON roblox_queue(run_id,state,position)",
    )
    with store.transaction() as db:
        for statement in statements:
            db.execute(statement)
        if "interval_seconds" not in {row[1] for row in db.execute("PRAGMA table_info(roblox_runs)")}:
            db.execute("ALTER TABLE roblox_runs ADD COLUMN interval_seconds REAL NOT NULL DEFAULT 5")
        columns = {row[1] for row in db.execute("PRAGMA table_info(roblox_runs)")}
        for column, definition in (
            ("preparation", "TEXT NOT NULL DEFAULT '{}'"),
            ("input_cursor", "INTEGER NOT NULL DEFAULT 0"),
            ("blank", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in columns:
                db.execute(f"ALTER TABLE roblox_runs ADD COLUMN {column} {definition}")
        db.execute(
            "CREATE TABLE IF NOT EXISTS roblox_input_seen ("
            "run_id TEXT REFERENCES roblox_runs(id),name TEXT,PRIMARY KEY(run_id,name))"
        )


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC).isoformat()
    except (ValueError, TypeError) as exc:
        raise ValueError("تاريخ النتيجة المسجلة مفقود أو غير صالح؛ لم يُستورد الملف.") from exc


def fingerprint(result):
    data = [
        result["username"].casefold(),
        result["checked_at"],
        result["raw_sha256"],
        result.get("http_status"),
        result.get("api_code"),
    ]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


class RobloxStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _control(db):
        return json.loads(db.execute("SELECT value FROM roblox_control WHERE id=1").fetchone()[0])

    @staticmethod
    def _save_control(db, value):
        db.execute("UPDATE roblox_control SET value=? WHERE id=1", (json.dumps(value),))

    @staticmethod
    def _insert(db, result, source, source_key, run_id=None, completed=True):
        name = result["username"]
        key = name.casefold()
        result = dict(
            result,
            checked_at=timestamp(result["checked_at"]),
            preliminary=True,
            independently_verified_classification="Unknown",
        )
        cursor = db.execute(
            """INSERT INTO roblox_checks(username_key,username,checked_at,classification,api_code,
            evidence,source,source_key,fingerprint,completed,run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                name,
                result["checked_at"],
                result["classification"],
                result.get("api_code"),
                json.dumps(result, ensure_ascii=False),
                source,
                source_key,
                fingerprint(result),
                int(completed),
                run_id,
            ),
        )
        ordered = db.execute(
            "SELECT id,classification FROM roblox_checks WHERE username_key=? ORDER BY checked_at,id", (key,)
        ).fetchall()
        previous = None
        for row in ordered:
            db.execute(
                "UPDATE roblox_checks SET previous_classification=?,changed=? WHERE id=?",
                (previous, int(previous is not None and previous != row["classification"]), row["id"]),
            )
            previous = row["classification"]
        latest_id = ordered[-1]["id"]
        latest_name = db.execute("SELECT username FROM roblox_checks WHERE id=?", (latest_id,)).fetchone()[0]
        count = db.execute(
            "SELECT SUM(completed) FROM roblox_checks WHERE username_key=?", (key,)
        ).fetchone()[0]
        db.execute(
            """INSERT INTO roblox_names VALUES(?,?,?,?) ON CONFLICT(username_key) DO UPDATE SET
                   username=excluded.username,latest_id=excluded.latest_id,completed_checks=excluded.completed_checks""",
            (key, latest_name, latest_id, count),
        )
        return cursor.lastrowid

    def known_keys(self):
        with self.store.lock:
            return {r[0] for r in self.store.db.execute("SELECT username_key FROM roblox_names")}

    def preferred_interval(self):
        value = self.control().get("preferred_interval_seconds", INTERVAL_SECONDS)
        try:
            return check_interval(value)
        except ValueError:
            return INTERVAL_SECONDS

    def set_preferred_interval(self, value):
        value = check_interval(value)
        with self.store.transaction() as db:
            control = self._control(db)
            control["preferred_interval_seconds"] = value
            self._save_control(db, control)

    def create_run(
        self,
        names=(),
        *,
        kind="normal",
        skip=True,
        duplicates=0,
        invalid=0,
        spec=None,
        cancel=None,
        notify=lambda _: None,
        run_id=None,
    ):
        identity = run_id or uuid4().hex
        if kind == "recheck_all" and spec is None:
            spec = {"history": True}
        if run_id:
            saved = self.progress(run_id)
            spec = json.loads(saved["preparation"])
            if not spec:
                raise ValueError("No recoverable input configuration")
            skip = bool(saved["skip_previous"])
        else:
            if kind.startswith("recheck"):
                skip = False
            with self.store.transaction() as db:
                at = now()
                db.execute(
                    "INSERT INTO roblox_runs(id,created_at,updated_at,kind,state,skip_previous,"
                    "duplicates,invalid,interval_seconds,preparation) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        at,
                        at,
                        kind,
                        "Preparing",
                        int(skip),
                        duplicates,
                        invalid,
                        self.preferred_interval(),
                        json.dumps(spec or {}),
                    ),
                )
        if spec and not spec.get("history"):
            names = input_values(spec)
        elif kind == "recheck_all" or (spec and spec.get("history")):
            # Keyset pages avoid holding the entire historical set or a long read transaction.
            def historical():
                key = ""
                while True:
                    with self.store.lock:
                        rows = self.store.db.execute(
                            "SELECT username_key,username FROM roblox_names WHERE username_key>? "
                            "ORDER BY username_key LIMIT 1000",
                            (key,),
                        ).fetchall()
                    if not rows:
                        return
                    for row in rows:
                        key = row["username_key"]
                        yield row["username"]

            names = historical()
        saved = self.progress(identity)
        cursor = saved["input_cursor"]
        consumed = 0
        values = None
        try:
            values = iter(names)
            # Re-read the immutable source on resume; completed batches are never reinserted.
            for _ in range(cursor):
                if cancel and cancel.is_set():
                    self.end(identity, "PreparationPaused")
                    return identity
                next(values)
            while True:
                if cancel and cancel.is_set():
                    self.end(identity, "PreparationPaused")
                    return identity
                batch, size = [], 0
                for value in values:
                    if cancel and cancel.is_set():
                        self.end(identity, "PreparationPaused")
                        return identity
                    batch.append(value)
                    size += len(value) if isinstance(value, str) else 0
                    if len(batch) >= 1000 or size >= 64 * 1024:
                        break
                if not batch:
                    break
                if shutil.disk_usage(self.store.path.parent).free < 1024 * 1024:
                    raise ValueError("المساحة الحرة منخفضة؛ حُفظت الدفعات السابقة ولم يبدأ الفحص.")
                records = []
                for offset, value in enumerate(batch, 1):
                    if not isinstance(value, str) or len(value) > 1024 * 1024 or "\x00" in value:
                        raise ValueError("Unsafe input record")
                    name = value.strip()
                    error = bool(spec and not spec.get("history") and syntax_error(name)) if name else False
                    if name and not error and len(name) > 256:
                        raise ValueError("Unsafe username record")
                    records.append((offset, name, name.casefold(), error))
                keys = tuple(dict.fromkeys(key for _, name, key, _ in records if name))
                with self.store.transaction() as db:
                    seen, known = set(), set()
                    for offset in range(0, len(keys), 400):
                        part = keys[offset : offset + 400]
                        placeholders = ",".join("?" for _ in part)
                        seen.update(
                            row[0]
                            for row in db.execute(
                                "SELECT name FROM roblox_input_seen WHERE run_id=? "
                                f"AND name IN ({placeholders})",
                                (identity, *part),
                            )
                        )
                        if skip:
                            known.update(
                                row[0]
                                for row in db.execute(
                                    "SELECT username_key FROM roblox_names "
                                    f"WHERE username_key IN ({placeholders})",
                                    part,
                                )
                            )
                    blank_count = duplicate_count = invalid_count = skipped_count = 0
                    new_seen, queued = [], []
                    for offset, name, key, error in records:
                        if not name:
                            blank_count += 1
                            continue
                        if key in seen:
                            duplicate_count += 1
                            continue
                        seen.add(key)
                        new_seen.append((identity, key))
                        if error:
                            invalid_count += 1
                            continue
                        skipped = key in known
                        queued.append(
                            (
                                identity,
                                cursor + consumed + offset,
                                name,
                                key,
                                "Skipped" if skipped else "Pending",
                            )
                        )
                        if skipped:
                            skipped_count += 1
                    db.executemany("INSERT INTO roblox_input_seen VALUES(?,?)", new_seen)
                    db.executemany("INSERT INTO roblox_queue VALUES(?,?,?,?,?)", queued)
                    db.execute(
                        "UPDATE roblox_runs SET input_cursor=?,state='Preparing',error='',updated_at=?,"
                        "blank=blank+?,duplicates=duplicates+?,invalid=invalid+?,skipped=skipped+? "
                        "WHERE id=?",
                        (
                            cursor + consumed + len(batch),
                            now(),
                            blank_count,
                            duplicate_count,
                            invalid_count,
                            skipped_count,
                            identity,
                        ),
                    )
                consumed += len(batch)
                notify(identity)
            if not self.progress(identity)["total"]:
                raise ValueError("No valid usernames; review empty or invalid input")
            self.end(identity, "Ready")
        except Exception as exc:
            self.end(identity, "PreparationError", str(exc)[:160])
            raise
        finally:
            if values is not None and hasattr(values, "close"):
                values.close()
        return identity

    def is_known(self, key):
        with self.store.lock:
            return (
                self.store.db.execute("SELECT 1 FROM roblox_names WHERE username_key=?", (key,)).fetchone()
                is not None
            )

    def preparation_progress(self, identity):
        """Constant-size checkpoint metadata; no growing queue scan on the GUI thread."""
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT id,created_at,updated_at,kind,state,skip_previous,skipped,duplicates,invalid,error,"
                "interval_seconds,input_cursor,blank FROM roblox_runs WHERE id=?",
                (identity,),
            ).fetchone()
            if row is None:
                raise ValueError("عملية التحضير غير موجودة.")
            return dict(row, counts={}, total=None)

    def known_count(self):
        with self.store.lock:
            return self.store.db.execute("SELECT COUNT(*) FROM roblox_names").fetchone()[0]

    def recover(self):
        # Probe each existing run through (run_id,state,position), not the entire queue.
        # Each bounded batch commits evidence and delivery state together.
        while True:
            with self.store.transaction() as db:
                tasks = db.execute(
                    "SELECT * FROM roblox_queue WHERE run_id IN (SELECT id FROM roblox_runs) "
                    "AND state='InFlight' LIMIT 100"
                ).fetchall()
                for task in tasks:
                    result = result_for(
                        task["username"],
                        protocol.Response(error="interrupted_delivery_uncertain", complete=False),
                    )
                    result["delivery_uncertain"] = True
                    self._insert(
                        db,
                        result,
                        "interrupted",
                        f"{task['run_id']}:{task['position']}",
                        task["run_id"],
                        completed=False,
                    )
                    db.execute(
                        "UPDATE roblox_queue SET state='Done' WHERE run_id=? AND position=?",
                        (task["run_id"], task["position"]),
                    )
            if len(tasks) < 100:
                break
        with self.store.transaction() as db:
            db.execute("UPDATE roblox_runs SET state='Paused',updated_at=? WHERE state='Running'", (now(),))
            db.execute("UPDATE roblox_runs SET state='PreparationPaused' WHERE state='Preparing'")
            control = self._control(db)
            control.pop("active_run", None)
            self._save_control(db, control)

    def start(self, run_id):
        with self.store.transaction() as db:
            control = self._control(db)
            if control.get("blocked"):
                raise ValueError("أوقف الموفر وقائيًا؛ يلزم فحص سبب القيود قبل طلبات إضافية.")
            if control.get("active_run"):
                raise ValueError("توجد عملية Roblox نشطة بالفعل.")
            row = db.execute("SELECT state FROM roblox_runs WHERE id=?", (run_id,)).fetchone()
            if not row or row[0] in {"Completed", "Preparing", "PreparationPaused", "PreparationError"}:
                return False
            control["active_run"] = run_id
            self._save_control(db, control)
            db.execute(
                "UPDATE roblox_runs SET state='Running',error='',updated_at=? WHERE id=?", (now(), run_id)
            )
        return True

    def control(self):
        with self.store.lock:
            return self._control(self.store.db)

    def claim_next(self, run_id):
        with self.store.transaction() as db:
            run = db.execute("SELECT * FROM roblox_runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["state"] != "Running":
                raise ValueError("العملية ليست نشطة.")
            while True:
                row = db.execute(
                    "SELECT * FROM roblox_queue WHERE run_id=? AND state='Pending' ORDER BY position LIMIT 1",
                    (run_id,),
                ).fetchone()
                if row is None:
                    return None
                if (
                    run["skip_previous"]
                    and db.execute(
                        "SELECT 1 FROM roblox_names WHERE username_key=?", (row["username_key"],)
                    ).fetchone()
                ):
                    db.execute(
                        "UPDATE roblox_queue SET state='Skipped' WHERE run_id=? AND position=?",
                        (run_id, row["position"]),
                    )
                    db.execute("UPDATE roblox_runs SET skipped=skipped+1 WHERE id=?", (run_id,))
                    continue
                db.execute(
                    "UPDATE roblox_queue SET state='InFlight' WHERE run_id=? AND position=?",
                    (run_id, row["position"]),
                )
                return dict(row)

    def has_pending(self, run_id):
        with self.store.lock:
            return (
                self.store.db.execute(
                    "SELECT 1 FROM roblox_queue WHERE run_id=? AND state='Pending' LIMIT 1", (run_id,)
                ).fetchone()
                is not None
            )

    def release_unstarted(self, task):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE roblox_queue SET state='Pending' WHERE run_id=? AND position=? AND state='InFlight'",
                (task["run_id"], task["position"]),
            )

    def finish_check(self, task, result, *, control_update=None, response_finished_at=None):
        response_finished_at = response_finished_at or datetime.now(UTC)
        with self.store.transaction() as db:
            state = db.execute(
                "SELECT state FROM roblox_queue WHERE run_id=? AND position=?",
                (task["run_id"], task["position"]),
            ).fetchone()
            if not state or state[0] != "InFlight":
                raise ValueError("النتيجة محفوظة مسبقًا أو المهمة غير نشطة.")
            self._insert(db, result, "roblox-api", f"{task['run_id']}:{task['position']}", task["run_id"])
            db.execute(
                "UPDATE roblox_queue SET state='Done' WHERE run_id=? AND position=?",
                (task["run_id"], task["position"]),
            )
            control = self._control(db)
            previous_deadline = control.get("not_before", "")
            control.update(control_update or {})
            interval = db.execute(
                "SELECT interval_seconds FROM roblox_runs WHERE id=?", (task["run_id"],)
            ).fetchone()[0]
            control["not_before"] = max(
                previous_deadline,
                control.get("not_before", ""),
                (response_finished_at + timedelta(seconds=interval)).isoformat(),
            )
            self._save_control(db, control)
            db.execute("UPDATE roblox_runs SET updated_at=? WHERE id=?", (now(), task["run_id"]))

    def end(self, run_id, state, error=""):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE roblox_runs SET state=?,error=?,updated_at=? WHERE id=?",
                (state, error, now(), run_id),
            )
            control = self._control(db)
            if control.get("active_run") == run_id:
                control.pop("active_run", None)
            self._save_control(db, control)

    def runs(self):
        with self.store.lock:
            return [
                dict(r)
                for r in self.store.db.execute(
                    "SELECT * FROM roblox_runs WHERE state!='Completed' OR id IN "
                    "(SELECT id FROM roblox_runs WHERE state='Completed' ORDER BY created_at DESC LIMIT 100) "
                    "ORDER BY state='Completed',created_at DESC"
                )
            ]

    def progress(self, run_id):
        with self.store.lock:
            run = self.store.db.execute("SELECT * FROM roblox_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                return None
            result = dict(run)
            # Input counters and queue inserts are committed atomically by preparation.
            # Count only the much smaller non-pending ranges in the existing index.
            total = run["input_cursor"] - run["duplicates"] - run["invalid"] - run["blank"]
            checkpointed = bool(json.loads(run["preparation"])) and run["input_cursor"] > 0
            result["counts"] = dict(
                self.store.db.execute(
                    "SELECT state,COUNT(*) FROM roblox_queue WHERE run_id=? "
                    + ("AND state IN ('Done','Skipped','InFlight') " if checkpointed else "")
                    + "GROUP BY state",
                    (run_id,),
                )
            )
            if checkpointed:
                pending = total - sum(result["counts"].values())
                if pending < 0:
                    raise ValueError("عدادات طابور Roblox غير متسقة؛ يلزم مراجعة البيانات.")
                if pending:
                    result["counts"]["Pending"] = pending
            result["total"] = sum(result["counts"].values())
            return result

    @staticmethod
    def _where(classification="", search="", changed=False):
        clauses, args = [], []
        if classification:
            if classification not in protocol.CLASSES:
                raise ValueError("تصنيف غير صالح.")
            clauses.append("c.classification=?")
            args.append(classification)
        if search:
            clauses.append("instr(c.username_key,?)>0")
            args.append(search.casefold()[:256])
        if changed:
            clauses.append("c.changed=1")
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), args

    def results(self, classification="", search="", changed=False, *, offset=0, limit=100):
        where, args = self._where(classification, search, changed)
        base = " FROM roblox_names n JOIN roblox_checks c ON c.id=n.latest_id" + where
        with self.store.lock:
            total = self.store.db.execute("SELECT COUNT(*)" + base, args).fetchone()[0]
            rows = self.store.db.execute(
                "SELECT c.*,n.completed_checks"
                + base
                + " ORDER BY c.checked_at DESC,c.id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            return [dict(r) for r in rows], total

    def history(self, username, limit=200):
        with self.store.lock:
            return [
                dict(r)
                for r in self.store.db.execute(
                    "SELECT * FROM roblox_checks WHERE username_key=? "
                    "ORDER BY checked_at DESC,id DESC LIMIT ?",
                    (username.casefold(), limit),
                )
            ]

    def counts(self):
        with self.store.lock:
            return dict(
                self.store.db.execute(
                    "SELECT c.classification,COUNT(*) FROM roblox_names n "
                    "JOIN roblox_checks c ON c.id=n.latest_id GROUP BY c.classification"
                )
            )

    def import_csv(self, path, *, cancel=None):
        path = Path(path)
        before = path.stat()
        hasher = hashlib.sha256()
        with path.open(encoding="utf-8-sig", newline="") as stream:
            while block := stream.read(64 * 1024):
                if cancel and cancel.is_set():
                    raise ValueError("أُلغي استيراد السجل؛ لم تُرسل طلبات جديدة.")
                hasher.update(block.encode("utf-8"))
        digest = hasher.hexdigest()
        # Validate all rows into temporary SQLite before exposing any historical result.
        with closing(sqlite3.connect("")) as stage, path.open(encoding="utf-8-sig", newline="") as stream:
            stage.execute("CREATE TABLE staged (result TEXT)")
            reader = csv.DictReader(bounded_lines(stream), strict=True)
            fields = reader.fieldnames or []
            if len(fields) != len(set(fields)) or not {"username", "response_data"} <= set(fields):
                raise ValueError("CSV requires distinct username and response_data columns")
            count = 0
            for row in reader:
                if cancel and cancel.is_set():
                    raise ValueError("History import cancelled; committed records are retained")
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("صفوف CSV زائدة أو غير متسقة.")
                if (
                    row.get("attempted", "True").lower() not in {"true", "1"}
                    or row.get("reason") == "not_attempted"
                ):
                    continue
                name = row["username"].strip()
                if not name or len(name) > 256 or any(ord(c) < 32 for c in name) or name[0] in "=+-@":
                    raise ValueError("اسم غير آمن أو فارغ في ملف النتائج.")
                checked = timestamp(row.get("request_at_utc") or row.get("checked_at"))
                try:
                    status = int(row.get("http_status", ""))
                except (ValueError, TypeError):
                    status = None
                response = protocol.Response(
                    status,
                    row["response_data"].encode("utf-8"),
                    {"content-type": "application/json"},
                    error=row.get("error_type", ""),
                    complete=row.get("raw_complete", "True").lower() == "true",
                )
                result = result_for(name, response, checked_at=checked)
                result.update(
                    imported_record=row,
                    import_json_parse_assumption=True,
                    import_source={"path": str(Path(path).resolve()), "csv_utf8_content_sha256": digest},
                )
                recorded = row.get("preliminary_classification", row.get("classification", "Unknown"))
                if recorded != result["classification"]:
                    result.update(classification="Unknown", reason="import_conflicting_or_ambiguous_record")
                if (row.get("api_code") not in (None, "") and str(result["api_code"]) != row["api_code"]) or (
                    row.get("raw_sha256") and row["raw_sha256"] != result["raw_sha256"]
                ):
                    result.update(classification="Unknown", reason="import_evidence_mismatch")
                if row.get("delivery_uncertain", "False").lower() == "true" or row.get("completed") == "0":
                    result.update(classification="Unknown", delivery_uncertain=True)
                stage.execute("INSERT INTO staged VALUES(?)", (json.dumps(result),))
                count += 1
            if not count:
                raise ValueError("No recorded results in CSV")
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("History CSV changed during preparation")
            with self.store.lock:
                if self.store.db.execute("SELECT 1 FROM roblox_imports WHERE digest=?", (digest,)).fetchone():
                    return {"imported": 0, "duplicates": count, "already_imported": True}
            inserted = 0
            cursor = stage.execute("SELECT result FROM staged")
            while rows := cursor.fetchmany(500):
                if cancel and cancel.is_set():
                    raise ValueError("History import cancelled; restart import to skip committed records")
                with self.store.transaction() as db:
                    for row in rows:
                        result = json.loads(row[0])
                        identity = fingerprint(result)
                        if db.execute(
                            "SELECT 1 FROM roblox_checks WHERE fingerprint=?", (identity,)
                        ).fetchone():
                            continue
                        self._insert(
                            db,
                            result,
                            f"experiment-csv:{path.name}",
                            "import:" + identity,
                            completed=not result.get("delivery_uncertain", False),
                        )
                        inserted += 1
            with self.store.transaction() as db:
                db.execute(
                    "INSERT OR IGNORE INTO roblox_imports VALUES(?,?,?,?)", (digest, path.name, now(), count)
                )
            return {"imported": inserted, "duplicates": count - inserted, "already_imported": False}

    def export_csv(self, path, classification="", search="", changed=False, *, history=False):
        path = Path(path)
        database = self.store.path.resolve()
        if path.resolve() in {
            database,
            *(Path(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal")),
        }:
            raise ValueError("لا يمكن استبدال قاعدة البيانات بملف تصدير.")
        where, args = self._where(classification, search, changed)
        base = (
            " FROM roblox_checks c"
            if history
            else " FROM roblox_names n JOIN roblox_checks c ON c.id=n.latest_id"
        )
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        fields = [
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
        ]
        with (
            closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db,
            temporary.open("w", encoding="utf-8-sig", newline="") as stream,
        ):
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            if history:
                base += " LEFT JOIN roblox_names n ON n.username_key=c.username_key"
            rows = db.execute(
                "SELECT c.*,n.completed_checks" + base + where + " ORDER BY c.checked_at,c.id", args
            )
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            count = 0
            for row in rows:
                row = dict(row)
                count += 1
                evidence = json.loads(row["evidence"])
                result = {field: row.get(field, "") for field in fields}
                result.update(
                    http_status=evidence.get("http_status"),
                    response_data=evidence.get("response_data", ""),
                    raw_complete=evidence.get("raw_complete", False),
                    error_type=evidence.get("error", ""),
                    completed_checks=row["completed_checks"],
                    preliminary_classification=row["classification"],
                    manual_signup_result="NotChecked",
                    delivery_uncertain=evidence.get("delivery_uncertain", False),
                )
                writer.writerow(result)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return count
