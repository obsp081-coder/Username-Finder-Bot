"""Additive X-only tables; existing platform tables and schema version are untouched."""

from __future__ import annotations

import csv
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

from . import x, x_connection


class XStore:
    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            for sql in (
                "CREATE TABLE IF NOT EXISTS x_runs(id TEXT PRIMARY KEY,created_at TEXT,state TEXT,"
                "spec TEXT,skip_previous INTEGER,interval_seconds REAL,cursor INTEGER DEFAULT 0,"
                "skipped INTEGER DEFAULT 0,duplicates INTEGER DEFAULT 0,invalid INTEGER DEFAULT 0,"
                "blank INTEGER DEFAULT 0,error TEXT DEFAULT '')",
                "CREATE TABLE IF NOT EXISTS x_queue(run_id TEXT REFERENCES x_runs(id),position INTEGER,"
                "username TEXT,username_key TEXT,state TEXT DEFAULT 'Pending',"
                "PRIMARY KEY(run_id,position),UNIQUE(run_id,username_key))",
                "CREATE TABLE IF NOT EXISTS x_seen(run_id TEXT REFERENCES x_runs(id),name TEXT,"
                "PRIMARY KEY(run_id,name))",
                "CREATE TABLE IF NOT EXISTS x_checks(id INTEGER PRIMARY KEY,username TEXT,username_key TEXT,"
                "checked_at TEXT,classification TEXT,evidence TEXT,"
                "previous_classification TEXT,changed INTEGER,"
                "run_id TEXT REFERENCES x_runs(id),position INTEGER,UNIQUE(run_id,position))",
                "CREATE TABLE IF NOT EXISTS x_names(username_key TEXT PRIMARY KEY,latest_id INTEGER "
                "REFERENCES x_checks(id),completed_checks INTEGER)",
                "CREATE TABLE IF NOT EXISTS x_control(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT)",
                "CREATE TABLE IF NOT EXISTS x_recovery_events(id TEXT PRIMARY KEY,restriction TEXT NOT NULL,"
                "started_at TEXT NOT NULL,connection TEXT NOT NULL,state TEXT NOT NULL,"
                "evidence TEXT NOT NULL DEFAULT '{}',released_at TEXT)",
                "INSERT OR IGNORE INTO x_control VALUES(1,'{}')",
                "CREATE INDEX IF NOT EXISTS x_pending ON x_queue(run_id,state,position)",
                "CREATE INDEX IF NOT EXISTS x_check_name ON x_checks(username_key,id)",
            ):
                db.execute(sql)

    def control(self):
        with self.store.lock:
            return json.loads(self.store.db.execute("SELECT value FROM x_control WHERE id=1").fetchone()[0])

    def restriction_info(self):
        """Read persisted evidence, including legacy stops, without changing any latch."""
        with self.store.lock:
            control = self.control()
            if not control.get("blocked"):
                return None
            check_id = control.get("restriction_check_id")
            row = self.store.db.execute(
                "SELECT id,run_id,evidence FROM x_checks WHERE id=?"
                if check_id
                else "SELECT id,run_id,evidence FROM x_checks WHERE classification='Unknown' "
                "ORDER BY id DESC LIMIT 1",
                (check_id,) if check_id else (),
            ).fetchone()
            evidence = json.loads(row["evidence"]) if row else {}
            if evidence.get("reason") != control["blocked"]:
                evidence, row = {}, None
            return {
                "reason": control["blocked"],
                "scope": "platform",
                "check_id": row["id"] if row else None,
                "run_id": row["run_id"] if row else None,
                "recorded_at": evidence.get("checked_at"),
                "http_status": evidence.get("http_status"),
                "raw_sha256": evidence.get("raw_sha256"),
                "connection": (
                    x_connection.validate(evidence.get("connection"))
                    if row and "connection" in evidence
                    else self.connection_for_run(row["run_id"])
                    if row
                    else None
                ),
                "headers": evidence.get("headers", {}),
                "local_not_before": control.get("not_before"),
                **x.restriction_wait(evidence.get("headers", {}), evidence.get("checked_at")),
            }

    @staticmethod
    def _control(db, **changes):
        value = json.loads(db.execute("SELECT value FROM x_control WHERE id=1").fetchone()[0])
        value.update(changes)
        db.execute("UPDATE x_control SET value=? WHERE id=1", (json.dumps(value),))

    def preferred_interval(self):
        return x.interval(self.control().get("preferred_interval_seconds", 5.0))

    def set_interval(self, value):
        with self.store.transaction() as db:
            self._control(db, preferred_interval_seconds=x.interval(value))

    def preferred_connection(self):
        return x_connection.validate(self.control().get("preferred_connection"))

    def set_connection(self, value):
        value = x_connection.validate(value)
        with self.store.transaction() as db:
            self._control(db, preferred_connection=value)

    def connection_for_run(self, identity):
        with self.store.lock:
            row = self.store.db.execute("SELECT spec FROM x_runs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("عملية X غير موجودة.")
            return x_connection.validate(json.loads(row[0]).get("connection"))

    def prepare(
        self, spec=None, *, skip=True, kind="normal", run_id=None, cancel=None, notify=lambda _: None
    ):
        identity = run_id or uuid4().hex
        if run_id is None:
            if kind == "recheck_all":
                rows, _ = self.results(limit=None)
                spec = {"manual": "\n".join(r["username"] for r in rows), "files": []}
                skip = False
            spec = dict(spec, connection=self.preferred_connection())
            with self.store.transaction() as db:
                db.execute(
                    "INSERT INTO x_runs(id,created_at,state,spec,skip_previous,interval_seconds) "
                    "VALUES(?,?,'Preparing',?,?,?)",
                    (identity, x.now(), json.dumps(spec), int(skip), self.preferred_interval()),
                )
        run = self.progress(identity)
        if run["state"] not in ("Preparing", "PreparationPaused", "PreparationError"):
            raise ValueError("العملية جاهزة أو مكتملة؛ لا تُعد تحضيرها.")
        spec = json.loads(run["spec"])
        values = None
        try:
            self.end(identity, "Preparing")
            notify(identity)
            values = iter(x.candidates(spec))
            cursor = run["cursor"]
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
                    size += len(value)
                    if len(batch) >= 1000 or size >= 64 * 1024:
                        break
                if not batch:
                    break
                if shutil.disk_usage(self.store.path.parent).free < 1024 * 1024:
                    raise ValueError("المساحة الحرة منخفضة؛ حُفظت الدفعات السابقة ولم يبدأ الفحص.")
                records = []
                for offset, value in enumerate(batch, 1):
                    name = value.strip()
                    records.append(
                        (
                            cursor + offset,
                            name,
                            name.casefold(),
                            bool(name and not x.protocol.validate_syntax(name)["valid"]),
                        )
                    )
                keys = tuple(dict.fromkeys(key for _, name, key, _ in records if name))
                with self.store.transaction() as db:
                    seen, known = set(), set()
                    for start in range(0, len(keys), 400):
                        part = keys[start : start + 400]
                        placeholders = ",".join("?" for _ in part)
                        seen.update(
                            row[0]
                            for row in db.execute(
                                f"SELECT name FROM x_seen WHERE run_id=? AND name IN ({placeholders})",
                                (identity, *part),
                            )
                        )
                        if run["skip_previous"]:
                            known.update(
                                row[0]
                                for row in db.execute(
                                    "SELECT username_key FROM x_names "
                                    f"WHERE username_key IN ({placeholders})",
                                    part,
                                )
                            )
                    blank = duplicates = invalid = skipped = 0
                    new_seen, queued = [], []
                    for position, name, key, error in records:
                        if not name:
                            blank += 1
                        elif key in seen:
                            duplicates += 1
                        else:
                            seen.add(key)
                            new_seen.append((identity, key))
                            if error:
                                invalid += 1
                            elif key in known:
                                skipped += 1
                            else:
                                queued.append((identity, position, name, key))
                    db.executemany("INSERT INTO x_seen VALUES(?,?)", new_seen)
                    db.executemany("INSERT INTO x_queue VALUES(?,?,?,?,'Pending')", queued)
                    cursor += len(batch)
                    db.execute(
                        "UPDATE x_runs SET cursor=?,blank=blank+?,duplicates=duplicates+?,"
                        "invalid=invalid+?,skipped=skipped+? WHERE id=?",
                        (cursor, blank, duplicates, invalid, skipped, identity),
                    )
                notify(identity)
            if cancel and cancel.is_set():
                self.end(identity, "PreparationPaused")
                return identity
            self.end(identity, "Ready")
        except Exception as error:
            self.end(identity, "PreparationError", str(error))
            raise
        finally:
            if values is not None and hasattr(values, "close"):
                values.close()
        return identity

    def preparation_progress(self, identity):
        """Read checkpoint counters without scanning the growing queue or loading source text."""
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT id,created_at,state,skip_previous,interval_seconds,cursor,skipped,"
                "duplicates,invalid,blank,error FROM x_runs WHERE id=?",
                (identity,),
            ).fetchone()
            if row is None:
                raise ValueError("عملية X غير موجودة.")
            return dict(row, total=None, completed=0)

    def progress(self, identity):
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM x_runs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("عملية X غير موجودة.")
            data = dict(row)
            total = data["cursor"] - sum(data[key] for key in ("blank", "duplicates", "invalid", "skipped"))
            checkpointed = data["cursor"] > 0
            counts = dict(
                self.store.db.execute(
                    "SELECT state,COUNT(*) FROM x_queue WHERE run_id=? "
                    + ("AND state IN ('Done','InFlight') " if checkpointed else "")
                    + "GROUP BY state",
                    (identity,),
                ).fetchall()
            )
            if checkpointed:
                pending = total - sum(counts.values())
                if pending < 0:
                    raise ValueError("عدادات طابور X غير متسقة؛ يلزم مراجعة البيانات.")
                if pending:
                    counts["Pending"] = pending
            data.update(
                total=sum(counts.values()), completed=counts.get("Done", 0), pending=counts.get("Pending", 0)
            )
            spec = json.loads(data["spec"])
            generation = spec.get("generation")
            source = "generation" if generation else "files" if spec.get("files") else "manual"
            data.update(
                input_source=source,
                requested_candidates=generation["quantity"] if generation else None,
                generated_candidates=data["cursor"] if generation else 0,
                minimum_length=generation.get("minimum", generation.get("length")) if generation else None,
                maximum_length=generation.get("maximum", generation.get("length")) if generation else None,
            )
            return data

    def runs(self):
        with self.store.lock:
            return [dict(r) for r in self.store.db.execute("SELECT * FROM x_runs ORDER BY created_at DESC")]

    def preview(self, identity):
        with self.store.lock:
            return [
                r[0]
                for r in self.store.db.execute(
                    "SELECT username FROM x_queue WHERE run_id=? ORDER BY position LIMIT 20", (identity,)
                )
            ]

    def start(self, identity):
        with self.store.transaction() as db:
            if self.control().get("blocked"):
                raise ValueError("توقف X يتطلب مراجعة الدليل؛ لن تُرسل طلبات جديدة.")
            if db.execute("SELECT 1 FROM x_runs WHERE state='Running'").fetchone():
                raise ValueError("توجد عملية X قيد التشغيل.")
            if self.progress(identity)["state"] not in ("Ready", "Paused"):
                raise ValueError("لا يمكن استئناف حالة العملية الحالية.")
            gate = self.control().get("recovery_gate")
            if gate:
                row = db.execute(
                    "SELECT rowid,interval_seconds FROM x_runs WHERE id=?", (identity,)
                ).fetchone()
                if (
                    row["rowid"] <= gate["last_run_rowid"]
                    or row["interval_seconds"] < 5
                    or self.connection_for_run(identity) != gate["connection"]
                ):
                    raise ValueError("بعد التحرير، حضّر عملية جديدة بالاتصال الأصلي وفاصل 5 ثوانٍ أو أكثر.")
                self._control(db, recovery_gate=None, recovery_first_run_id=identity)
            db.execute("UPDATE x_runs SET state='Running',error='' WHERE id=?", (identity,))

    def claim(self, identity):
        with self.store.transaction() as db:
            task = db.execute(
                "SELECT * FROM x_queue WHERE run_id=? AND state='Pending' ORDER BY position LIMIT 1",
                (identity,),
            ).fetchone()
            if task:
                db.execute(
                    "UPDATE x_queue SET state='InFlight' WHERE run_id=? AND position=?",
                    (identity, task["position"]),
                )
            return dict(task) if task else None

    def release(self, task):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE x_queue SET state='Pending' WHERE run_id=? AND position=? AND state='InFlight'",
                (task["run_id"], task["position"]),
            )

    def finish(self, task, result, *, not_before=None, blocked=None):
        with self.store.transaction() as db:
            key = task["username_key"]
            previous = db.execute(
                "SELECT c.classification FROM x_names n JOIN x_checks c ON c.id=n.latest_id "
                "WHERE n.username_key=?",
                (key,),
            ).fetchone()
            previous = previous[0] if previous else None
            identity = db.execute(
                "INSERT INTO x_checks(username,username_key,checked_at,classification,evidence,"
                "previous_classification,changed,run_id,position) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    task["username"],
                    key,
                    result["checked_at"],
                    result["classification"],
                    json.dumps(result),
                    previous,
                    int(previous is not None and previous != result["classification"]),
                    task["run_id"],
                    task["position"],
                ),
            ).lastrowid
            db.execute(
                "INSERT INTO x_names VALUES(?,?,1) ON CONFLICT(username_key) DO UPDATE "
                "SET latest_id=excluded.latest_id,completed_checks=completed_checks+1",
                (key, identity),
            )
            db.execute(
                "UPDATE x_queue SET state='Done' WHERE run_id=? AND position=?",
                (task["run_id"], task["position"]),
            )
            changes = {}
            if not_before:
                changes["not_before"] = max(self.control().get("not_before", ""), not_before)
            if blocked:
                changes["blocked"] = blocked
                changes["restriction_check_id"] = identity
            self._control(db, **changes)

    def end(self, identity, state, error=""):
        with self.store.transaction() as db:
            db.execute("UPDATE x_runs SET state=?,error=? WHERE id=?", (state, error, identity))

    def recover(self):
        with self.store.lock:
            db = self.store.db
            while True:
                rows = db.execute(
                    "SELECT * FROM x_queue WHERE run_id IN (SELECT id FROM x_runs) "
                    "AND state='InFlight' LIMIT 100"
                ).fetchall()
                for row in rows:
                    result = x.result_for(
                        row["username"],
                        x.protocol.Response(error="interrupted_delivery_uncertain", complete=False),
                    )
                    result["delivery_uncertain"] = True
                    result["connection"] = self.connection_for_run(row["run_id"])
                    self.finish(dict(row), result, blocked="interrupted_delivery_uncertain")
                    self.end(row["run_id"], "Restricted", "interrupted_delivery_uncertain")
                if len(rows) < 100:
                    break
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE x_recovery_events SET state='Uncertain',evidence=? WHERE state='InFlight'",
                    (json.dumps({"error": "interrupted_recovery_uncertain"}),),
                )
                db.execute("UPDATE x_runs SET state='Paused' WHERE state='Running'")
                db.execute("UPDATE x_runs SET state='PreparationPaused' WHERE state='Preparing'")

    def results(self, classification="", search="", *, limit=100, offset=0, history=False):
        if classification and classification not in x.protocol.CLASSES:
            raise ValueError("تصنيف غير معروف.")
        base = (
            " FROM x_checks c LEFT JOIN x_names n ON n.username_key=c.username_key"
            if history
            else " FROM x_names n JOIN x_checks c ON c.id=n.latest_id"
        )
        base += " WHERE (?='' OR c.classification=?) AND instr(lower(c.username),lower(?))>0"
        args = (classification, classification, search)
        with self.store.lock:
            total = self.store.db.execute("SELECT COUNT(*)" + base, args).fetchone()[0]
            query = "SELECT c.*,n.completed_checks" + base + " ORDER BY c.id DESC"
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                args += (limit, offset)
            return [dict(r) for r in self.store.db.execute(query, args)], total

    def history(self, username):
        with self.store.lock:
            return [
                dict(r)
                for r in self.store.db.execute(
                    "SELECT * FROM x_checks WHERE username_key=? ORDER BY id DESC", (username.casefold(),)
                )
            ]

    def counts(self):
        with self.store.lock:
            return dict(
                self.store.db.execute(
                    "SELECT c.classification,COUNT(*) FROM x_names n JOIN x_checks c ON c.id=n.latest_id "
                    "GROUP BY c.classification"
                ).fetchall()
            )

    def export(self, path, classification="", search="", *, history=False):
        path = Path(path)
        database = self.store.path.resolve()
        if path.resolve() in {database, *(Path(str(database) + s) for s in ("-wal", "-shm", "-journal"))}:
            raise ValueError("لا يمكن استبدال قاعدة البيانات بالتصدير.")
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        fields = (
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
        count = 0
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            # Hold a consistent snapshot while paging; no entire-history allocation.
            with self.store.lock:
                while True:
                    rows, _ = self.results(classification, search, history=history, offset=count)
                    if not rows:
                        break
                    for row in rows:
                        evidence = json.loads(row["evidence"])
                        values = dict(
                            row,
                            api_reason=evidence.get("api_reason"),
                            preliminary=True,
                            independently_verified=False,
                            warning=x.WARNING,
                        )
                        writer.writerow({key: values.get(key, "") for key in fields})
                    count += len(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return count
