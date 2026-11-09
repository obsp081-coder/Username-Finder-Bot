"""Disk-backed bulk prefilter. Never writes registration history or production SQL."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from . import roblox_bulk_protocol as bulk
from . import roblox_prefilter_cooldown as cooldown_protocol
from . import roblox_prefilter_transport as transport_protocol
from .roblox import input_spec, input_values, syntax_error
from .roblox_activity import RobloxActivity
from .roblox_bulk import write_new
from .roblox_prefilter_transport import PrefilterTransport
from .roblox_protocol import Response

MATCH = "CurrentAccountMatchByAPI"
UNKNOWN = "Unknown"
UNSAFE = "UnsafeOrContradictoryResponse"
GROUPS = {
    "current": "state='CurrentAccountMatchByAPI'",
    "candidates": "state NOT IN ('CurrentAccountMatchByAPI','LocalInvalid')",
    "invalid": "state='LocalInvalid'",
    "unresolved": "state IN ('Unknown','UnsafeOrContradictoryResponse')",
}
MAX_CONFIGURED_BATCH = transport_protocol.MAX_BATCH_SIZE
VERIFIED_OPERATIONAL_BATCH = 20


def settings(delay=10, batch_size=20, pilot_names=100):
    if (
        isinstance(delay, bool)
        or not isinstance(delay, (int, float))
        or not math.isfinite(delay)
        or delay <= 0
        or delay > threading.TIMEOUT_MAX
    ):
        raise ValueError("الفاصل يجب أن يكون عددًا موجبًا ومحدودًا؛ الصفر غير مدعوم هنا.")
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_CONFIGURED_BATCH:
        raise ValueError("حجم الدفعة عدد صحيح من 1 إلى 1000.")
    if type(pilot_names) is not int or not 1 <= pilot_names <= 200:
        raise ValueError("التجربة الأولية من 1 إلى 200 اسم صالح فريد.")
    return {"delay": float(delay), "batch_size": batch_size, "pilot_names": pilot_names}


def policy():
    return bulk.digest(
        Path(__file__).read_bytes()
        + Path(bulk.__file__).read_bytes()
        + Path(transport_protocol.__file__).read_bytes()
        + Path(cooldown_protocol.__file__).read_bytes()
        + Path(__file__).with_name("roblox_activity.py").read_bytes()
        + Path(__file__).with_name("roblox_cooldown.py").read_bytes()
    )


def memory_peak():
    """Whole-process OS peak, not a claimed incremental cost of this workflow."""
    if os.name != "nt":
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    import ctypes
    from ctypes import wintypes

    class Memory(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (n, ctypes.c_size_t)
            for n in (
                "peak",
                "working",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
            )
        ]

    data = Memory()
    data.cb = ctypes.sizeof(data)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    api = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    api.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    return data.peak if api(kernel.GetCurrentProcess(), ctypes.byref(data), data.cb) else None


class Prefilter:
    def __init__(
        self,
        directory,
        database,
        *,
        control=lambda: {},
        transport=None,
        clock=time.monotonic,
        wall=lambda: datetime.now(UTC),
        wait=None,
        updated=lambda: None,
    ):
        self.directory, self.database = Path(directory), Path(database)
        self.path = self.directory / "work.sqlite3"
        self.manifest_path = self.directory / "manifest.json"
        self.control, self.transport = control, transport
        self.clock, self.wall, self.updated = clock, wall, updated
        self.cancel = threading.Event()
        self.wait = wait or self.cancel.wait

    @contextmanager
    def connect(self, *, create=False, read_only=False):
        if not create and not self.path.exists():
            raise ValueError("ملف العمل غير موجود.")
        target = self.path.resolve().as_uri() + "?mode=ro" if read_only else self.path
        db = sqlite3.connect(target, timeout=5, uri=read_only)
        db.row_factory = sqlite3.Row
        if read_only:
            db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA cache_size=-4096")
        db.execute("PRAGMA temp_store=FILE")
        db.execute("PRAGMA mmap_size=0")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def meta(db):
        return json.loads(db.execute("SELECT value FROM meta").fetchone()[0])

    @staticmethod
    def save(db, meta):
        meta["revision"] += 1
        db.execute("UPDATE meta SET value=?", (bulk.encoded(meta).decode(),))

    def manifest(self):
        value = bulk.strict_json(self.manifest_path.read_bytes())
        fingerprint = value.pop("fingerprint")
        if bulk.digest(bulk.encoded(value)) != fingerprint:
            raise ValueError("بصمة الإعدادات غير مطابقة؛ لا تشغيل.")
        settings(**value["settings"])
        if (
            value["version"] != 1
            or value["endpoint"] != bulk.BULK_URL
            or value["excludeBannedUsers"] is not False
            or value["timeout_seconds"] != 10
        ):
            raise ValueError("معاملات أو إصدار غير مدعوم؛ لا تشغيل.")
        value["fingerprint"] = fingerprint
        return value

    def hash_source(self, path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            while data := stream.read(1024 * 1024):
                if self.cancel.is_set():
                    raise InterruptedError("أُلغي فحص بصمة المصدر؛ لم يبدأ اتصال.")
                digest.update(data)
        return digest.hexdigest()

    def prepare(self, source=None, *, delay=10, batch_size=20, pilot_names=100):
        if not self.manifest_path.exists():
            config = settings(delay, batch_size, pilot_names)
            spec = input_spec(files=[source])
            if Path(source).suffix.casefold() not in (".txt", ".csv"):
                raise ValueError("اختر TXT أو CSV بترميز UTF-8.")
            value = {
                "version": 1,
                "cooldown_recovery": 1,
                "source": spec,
                "source_sha256": self.hash_source(source),
                "settings": config,
                "policy": policy(),
                "created_at": bulk.utcnow(),
                "endpoint": bulk.BULK_URL,
                "excludeBannedUsers": False,
                "timeout_seconds": 10,
                "reuse": "Only durable observations within this frozen import; no historical cache",
            }
            value["fingerprint"] = bulk.digest(bulk.encoded(value))
            self.directory.mkdir(parents=True, exist_ok=True)
            write_new(self.manifest_path, value)
        manifest = self.manifest()
        if manifest["policy"] != policy():
            raise ValueError("تغيرت سياسة المعالجة؛ يلزم تدقيق ولا تعاد الطلبات.")
        with RobloxActivity(self.path):
            with self.connect(create=True) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS meta(value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS names(
                        id INTEGER PRIMARY KEY,key TEXT UNIQUE NOT NULL,username TEXT NOT NULL,
                        source_record INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,
                        request_id INTEGER,account_id INTEGER,current_name TEXT,evidence_at TEXT);
                    CREATE INDEX IF NOT EXISTS pending ON names(state,id);
                    CREATE INDEX IF NOT EXISTS account_names ON names(account_id,id);
                    CREATE TABLE IF NOT EXISTS identities(
                        account_id INTEGER PRIMARY KEY,current_key TEXT UNIQUE);
                    CREATE TABLE IF NOT EXISTS requests(
                        id INTEGER PRIMARY KEY,phase TEXT,state TEXT,names TEXT,request_at TEXT,
                        response_at TEXT,evidence TEXT,latency REAL,stop TEXT NOT NULL DEFAULT '');
                    CREATE TABLE IF NOT EXISTS decisions(
                        id INTEGER PRIMARY KEY,name_id INTEGER,request_id INTEGER,previous TEXT,state TEXT,
                        reason TEXT,record TEXT,at TEXT);
                    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,event TEXT,at TEXT,data TEXT);
                """)
                if not db.execute("SELECT 1 FROM meta").fetchone():
                    counts = dict.fromkeys(
                        (
                            "imported",
                            "blank",
                            "duplicates",
                            "unique",
                            "invalid",
                            "valid",
                            "processed",
                            "excluded",
                            "unknown",
                            "unsafe",
                            "failed_requests",
                            "completed_requests",
                            "attempts",
                        ),
                        0,
                    )
                    db.execute(
                        "INSERT INTO meta VALUES(?)",
                        (
                            json.dumps(
                                {
                                    "revision": 0,
                                    "fingerprint": manifest["fingerprint"],
                                    "import_state": "Preparing",
                                    "cursor": 0,
                                    "counts": counts,
                                    "phases": {},
                                    "peak_memory_bytes": memory_peak(),
                                }
                            ),
                        ),
                    )
                meta = self.meta(db)
                if meta["fingerprint"] != manifest["fingerprint"]:
                    raise ValueError("ملف العمل لا ينتمي إلى هذه العينة.")
                if meta["import_state"] == "Ready":
                    return self.summary()
            if self.hash_source(manifest["source"]["files"][0]["path"]) != manifest["source_sha256"]:
                raise ValueError("تغير المصدر؛ لا تعديل للدليل ولا إرسال.")
            with self.connect() as db:
                meta = self.meta(db)
                meta.pop("import_error", None)
                resume_cursor = meta["cursor"]
                try:
                    for ordinal, value in enumerate(input_values(manifest["source"]), 1):
                        if self.cancel.is_set():
                            meta["import_state"] = "ImportPaused"
                            break
                        if ordinal <= resume_cursor:
                            continue
                        name = value.strip()
                        meta["counts"]["imported"] += 1
                        if not name:
                            meta["counts"]["blank"] += 1
                        else:
                            reason = syntax_error(name)
                            inserted = db.execute(
                                "INSERT OR IGNORE INTO names(key,username,source_record,state,reason) "
                                "VALUES(?,?,?,?,?)",
                                (
                                    ("invalid:" if reason else "") + name.casefold(),
                                    name,
                                    ordinal,
                                    "LocalInvalid" if reason else "Pending",
                                    reason or "",
                                ),
                            ).rowcount
                            meta["counts"]["duplicates"] += not inserted
                            meta["counts"]["unique"] += inserted
                            meta["counts"]["invalid" if reason else "valid"] += inserted
                        meta["cursor"] = ordinal
                        if ordinal % 1000 == 0:
                            meta["peak_memory_bytes"] = memory_peak()
                            self.save(db, meta)
                            db.commit()
                            self.updated()
                    else:
                        meta["import_state"] = "Ready"
                        target = min(meta["counts"]["valid"], manifest["settings"]["pilot_names"])
                        cutoff = db.execute(
                            "SELECT id FROM names WHERE state='Pending' ORDER BY id LIMIT 1 OFFSET ?",
                            (max(0, target - 1),),
                        ).fetchone()
                        meta["pilot_cutoff"] = cutoff[0] if cutoff else 0
                        meta["phases"] = {
                            "pilot": {
                                "state": "Ready",
                                "target": target,
                                "processed": 0,
                                "elapsed_seconds": 0,
                                "stop_reason": "",
                            }
                        }
                except Exception as exc:
                    # SQLite FULL/IO failures may roll back the whole current chunk.
                    # Reload counters and cursor from the last committed checkpoint too.
                    db.rollback()
                    meta = self.meta(db)
                    meta["import_state"] = "ImportError"
                    meta["import_error"] = type(exc).__name__ + ": " + str(exc)
                self.save(db, meta)
            self.updated()
        return self.summary()

    def summary(self, *, read_only=False):
        manifest = self.manifest()
        with self.connect(read_only=True) if read_only else self.connect() as db:
            meta = self.meta(db)
            if meta["fingerprint"] != manifest["fingerprint"]:
                raise ValueError("Manifest/work mismatch")
            c, config = meta["counts"], manifest["settings"]
            batch = config["batch_size"]
            requests = (c["valid"] + batch - 1) // batch
            pilot = min(c["valid"], config["pilot_names"])
            budget = (pilot + batch - 1) // batch + (max(0, c["valid"] - pilot) + batch - 1) // batch
            last = db.execute(
                "SELECT id,phase,state,names,response_at,stop FROM requests ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last_request = (
                {k: last[k] for k in ("id", "phase", "state", "response_at", "stop")} if last else None
            )
            if last_request:
                last_request["affected_names"] = len(json.loads(last["names"]))
            pacing_remaining = 0.0
            if last and last["response_at"]:
                pacing_remaining = max(
                    0.0,
                    config["delay"]
                    - (self.wall() - datetime.fromisoformat(last["response_at"])).total_seconds(),
                )
            meta.update(
                manifest=manifest,
                remaining_candidates=c["valid"] - c["excluded"],
                unprocessed=c["valid"] - c["processed"],
                planned_requests=budget,
                unpartitioned_requests=requests,
                output_directory=str(self.directory / "exports"),
                estimated_pacing_seconds=max(0, budget - 1) * config["delay"],
                estimate_note="Pacing lower bound; excludes response, import, disk and user review time",
                last_request=last_request,
                pacing_remaining_seconds=pacing_remaining,
                provider_blocked=RobloxActivity(self.database).block.exists(),
                shared_cooldown=RobloxActivity(self.database).cooldown_state(),
                timing_policy_current=manifest["policy"] == policy(),
            )
            meta["samples"] = [
                dict(r)
                for r in db.execute(
                    "SELECT id,substr(username,1,80) AS username,source_record,state,reason,request_id,"
                    "account_id,current_name,evidence_at,length(username)>80 AS preview_truncated "
                    "FROM names ORDER BY id LIMIT 30"
                )
            ]
            meta["quality"] = {}
            for phase in ("pilot", "full"):
                phase_meta = meta["phases"].get(phase, {})
                count = phase_meta.get("attempts", 0)
                response_count = phase_meta.get("responses", 0)
                latency = phase_meta.get("latency_sum", 0) / response_count if response_count else None
                complete = phase_meta.get("safe_responses", 0)
                elapsed = meta["phases"].get(phase, {}).get("elapsed_seconds", 0)
                trustworthy_time = elapsed and not phase_meta.get("elapsed_incomplete")
                matches = (
                    db.execute(
                        "SELECT count(*) FROM names WHERE state=? AND id<=?",
                        (MATCH, meta.get("pilot_cutoff", 0)),
                    ).fetchone()[0]
                    if phase == "pilot"
                    else c["excluded"]
                )
                meta["quality"][phase] = {
                    "request_attempts": count,
                    "safe_responses": complete or 0,
                    "mean_latency_seconds": latency,
                    "requests_per_minute": count * 60 / elapsed if trustworthy_time else None,
                    "names_per_minute": meta["phases"].get(phase, {}).get("processed", 0) * 60 / elapsed
                    if trustworthy_time
                    else None,
                    "pilot_exclusion_fraction": matches / phase_meta["processed"]
                    if phase_meta.get("processed") and phase == "pilot"
                    else None,
                    "exclusion_denominator": "Processed pilot names, including uncertain outcomes",
                    "pilot_coverage": phase_meta.get("processed", 0) / pilot
                    if pilot and phase == "pilot"
                    else None,
                }
            return meta

    @staticmethod
    def audit(db, event, data):
        db.execute("INSERT INTO audit(event,at,data) VALUES(?,?,?)", (event, bulk.utcnow(), json.dumps(data)))

    def _decide(self, db, meta, row, state, reason, request_id, record=None):
        counters = {MATCH: "excluded", UNKNOWN: "unknown", UNSAFE: "unsafe"}
        if row["state"] in counters:
            meta["counts"][counters[row["state"]]] -= 1
        if state in counters:
            meta["counts"][counters[state]] += 1
        at = self.wall().isoformat()
        db.execute(
            "INSERT INTO decisions(name_id,request_id,previous,state,reason,record,at) VALUES(?,?,?,?,?,?,?)",
            (row["id"], request_id, row["state"], state, reason, json.dumps(record), at),
        )
        db.execute(
            "UPDATE names SET state=?,reason=?,request_id=?,account_id=?,current_name=?,evidence_at=? "
            "WHERE id=?",
            (
                state,
                reason,
                request_id,
                record.get("id") if record else row["account_id"],
                record.get("name") if record else row["current_name"],
                at,
                row["id"],
            ),
        )

    def _finish(self, db, request, response, meta):
        names = json.loads(request["names"])
        mapping, stop = bulk.interpret_bulk(names, response)
        evidence = json.loads(request["evidence"])
        if evidence["body_omitted_for_sensitive_fields"]:
            stop = "unexpected_sensitive_response"
        conflicts = set()
        if not stop:
            for value in mapping.values():
                record = value.get("record")
                if not record:
                    continue
                if record["id"] > 9223372036854775807:
                    stop = "unsafe_bulk_record_account_id_out_of_storage_range"
                    break
                for old in db.execute(
                    "SELECT * FROM identities WHERE account_id=? OR current_key=?",
                    (record["id"], record["name"].lower()),
                ):
                    if old["account_id"] != record["id"] or old["current_key"] != record["name"].lower():
                        stop = "cross_batch_conflicting_identity"
                        conflicts.add(old["account_id"])
        if stop:
            for uid in conflicts:
                cursor = 0
                while rows := db.execute(
                    "SELECT * FROM names WHERE account_id=? AND id>? ORDER BY id LIMIT 256", (uid, cursor)
                ).fetchall():
                    for row in rows:
                        self._decide(db, meta, row, UNSAFE, stop, request["id"])
                    cursor = rows[-1]["id"]
        for name in names:
            row = db.execute("SELECT * FROM names WHERE key=?", (name.casefold(),)).fetchone()
            value = mapping[name.casefold()]
            state = (
                (
                    UNSAFE
                    if any(s in stop for s in ("json", "schema", "record", "conflict", "duplicate"))
                    else UNKNOWN
                )
                if stop
                else value["state"]
            )
            self._decide(
                db,
                meta,
                row,
                state,
                stop or value["reason"],
                request["id"],
                None if stop else value.get("record"),
            )
            if not stop and (record := value.get("record")):
                db.execute(
                    "INSERT OR IGNORE INTO identities VALUES(?,?)", (record["id"], record["name"].lower())
                )
        c = meta["counts"]
        c["processed"] += len(names)
        c["completed_requests"] += bool(response.complete and response.status is not None)
        c["failed_requests"] += bool(stop)
        phase = meta["phases"][request["phase"]]
        phase["responses"] = phase.get("responses", 0) + 1
        phase["latency_sum"] = phase.get("latency_sum", 0) + response.elapsed
        phase["safe_responses"] = phase.get("safe_responses", 0) + (not stop)
        phase["processed"] += len(names)
        if stop:
            phase.update(state="Restricted", stop_reason=stop)
        elif phase["processed"] == phase["target"]:
            phase.update(state="Completed", stop_reason="phase_boundary")
        db.execute(
            "UPDATE requests SET state=?,stop=? WHERE id=?",
            ("SafetyStop" if stop else "Completed", stop, request["id"]),
        )
        self.audit(db, "decision_checkpoint", {"request_id": request["id"], "stop": stop})
        cooldown_protocol.capture(self, db, meta, request, stop)
        self.save(db, meta)
        return stop

    def recover(self):
        if self.manifest()["policy"] != policy():
            # Never reinterpret old pending evidence under a different source policy.
            return self.summary(read_only=True)
        with RobloxActivity(self.path):
            with self.connect() as db:
                meta = self.meta(db)
                before = bulk.encoded(meta)
                recovered_safe = set()
                pending = db.execute(
                    "SELECT * FROM requests WHERE state IN ('Intent','Response') ORDER BY id LIMIT 2"
                ).fetchall()
                if len(pending) > 1:
                    raise ValueError("More than one unfinished request; technical review required")
                for request in pending:
                    # A killed process may not have checkpointed the last request's wall time.
                    meta["phases"][request["phase"]]["elapsed_incomplete"] = True
                    if request["evidence"] is None:
                        for name in json.loads(request["names"]):
                            row = db.execute("SELECT * FROM names WHERE key=?", (name.casefold(),)).fetchone()
                            self._decide(
                                db, meta, row, UNKNOWN, "uncertain_delivery_never_replay", request["id"]
                            )
                        count = len(json.loads(request["names"]))
                        meta["counts"]["processed"] += count
                        meta["counts"]["failed_requests"] += 1
                        phase = meta["phases"][request["phase"]]
                        phase["processed"] += count
                        phase.update(state="ReviewRequired", stop_reason="uncertain_delivery_never_replay")
                        db.execute(
                            "UPDATE requests SET state='Uncertain',stop='uncertain_delivery' WHERE id=?",
                            (request["id"],),
                        )
                        self.audit(db, "uncertain_delivery", {"request_id": request["id"]})
                    else:
                        evidence = json.loads(request["evidence"])
                        body = base64.b64decode(evidence["body_base64"], validate=True)
                        if (
                            not evidence["body_omitted_for_sensitive_fields"]
                            and bulk.digest(body) != evidence["body_sha256"]
                        ):
                            raise ValueError("Raw response hash mismatch; technical review required")
                        stop = self._finish(
                            db,
                            request,
                            Response(
                                evidence["status"],
                                body,
                                evidence["headers"],
                                evidence["latency_seconds"],
                                evidence["error"],
                                evidence["complete"],
                                evidence["challenge"],
                            ),
                            meta,
                        )
                        if not stop:
                            recovered_safe.add(request["phase"])
                for name, phase in meta["phases"].items():
                    if phase["state"] == "Running" or (
                        name in recovered_safe and phase["state"] == "ReviewRequired"
                    ):
                        phase.update(
                            state="Paused", stop_reason="interrupted_safe_checkpoint", elapsed_incomplete=True
                        )
                if bulk.encoded(meta) != before:
                    self.save(db, meta)
        cooldown_protocol.sync(self)
        return self.summary()

    def review_uncertain(self, confirmation):
        if confirmation != self.manifest()["fingerprint"]:
            raise ValueError("Explicit evidence review is required")
        with RobloxActivity(self.path):
            with self.connect() as db:
                meta = self.meta(db)
                if not any(
                    p["state"] == "ReviewRequired" and p["stop_reason"] == "uncertain_delivery_never_replay"
                    for p in meta["phases"].values()
                ):
                    raise ValueError("لا يوجد طلب غير محسوم صالح لهذه المراجعة؛ القيود لا تُمسح.")
                for phase in meta["phases"].values():
                    if phase["state"] == "ReviewRequired":
                        phase.update(state="Paused", stop_reason="reviewed_skip_uncertain_no_replay")
                self.audit(
                    db,
                    "manual_uncertain_review",
                    {
                        "fingerprint": confirmation,
                        "decision": "Skip uncertain names; no replay or provider restriction bypass",
                    },
                )
                self.save(db, meta)

    def review(self, phase):
        if phase not in ("pilot", "full"):
            raise ValueError("Unknown phase")
        summary = self.summary()
        if summary["manifest"]["settings"]["batch_size"] > VERIFIED_OPERATIONAL_BATCH:
            raise ValueError("حجم أكبر من20 للتقدير والتحضير فقط؛ دعم الموفر غير مثبت والإرسال محظور.")
        if summary["provider_blocked"]:
            raise ValueError("قيد Bulk محفوظ؛ راجع الدليل. لا تفويض أو تحرير تلقائي.")
        if summary["shared_cooldown"] and summary["shared_cooldown"]["state"] == "Hold":
            raise ValueError("مهلة مشتركة محفوظة؛ راجع دليلها وفوّض الاستمرار بصورة منفصلة.")
        if summary["import_state"] != "Ready" or not summary["counts"]["valid"]:
            raise ValueError("يلزم اكتمال استيراد صالح محليًا أولًا.")
        if summary["manifest"]["policy"] != policy():
            raise ValueError("تغيرت السياسة؛ لا تشغيل دون تدقيق.")
        phases = summary["phases"]
        if phase == "full" and phases["pilot"]["state"] != "Completed":
            raise ValueError("راجع تجربة أولية مكتملة قبل تفويض الباقي.")
        current = phases.get(phase, {"state": "Ready"})
        if current["state"] not in ("Ready", "Paused"):
            raise ValueError("الطور مكتمل أو يحتاج مراجعة؛ لا إرسال.")
        if phase == "full" and not summary["unprocessed"]:
            raise ValueError("لا أسماء متبقية.")
        return bulk.digest(bulk.encoded([summary["manifest"]["fingerprint"], phase, summary["revision"]]))

    def _can_clear_pending(self, pending):
        prefix = ("Pending:" + self.manifest()["fingerprint"] + ":").encode()
        if not pending.startswith(prefix):
            return False
        try:
            identity = int(pending[len(prefix) :])
        except ValueError:
            return False
        with self.connect() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (identity,)).fetchone()
            if row is None:
                return False
            if row["state"] == "Completed" and not row["stop"]:
                return True
            return row["state"] == "Uncertain" and bool(
                db.execute(
                    "SELECT 1 FROM audit WHERE event='manual_uncertain_review' AND id>"
                    "(SELECT max(id) FROM audit WHERE event='uncertain_delivery')"
                ).fetchone()
            )

    def run(self, phase, authorization):
        with self.connect(read_only=True) as db:
            continuation = self.meta(db).get("cooldown", {}).get("stage") == "Authorized"
        if not authorization or (not continuation and authorization != self.review(phase)):
            raise ValueError("يلزم تفويض صريح للحالة الحالية.")
        with (
            RobloxActivity(self.path),
            (
                cooldown_protocol.shared(self)
                if continuation
                else RobloxActivity(self.database, recover_pending=self._can_clear_pending)
            ) as guard,
        ):
            if not continuation and authorization != self.review(phase):
                raise ValueError("تغيرت الحالة بعد المراجعة.")
            control = self.control()
            if (
                control.get("active_run")
                or control.get("blocked")
                or control.get("restriction_streak", 0)
                or (control.get("not_before") and datetime.fromisoformat(control["not_before"]) > self.wall())
            ):
                raise ValueError("فاحص Roblox نشط أو قيد/انتظار موفر لم يُحل.")
            config = self.manifest()["settings"]
            transport = self.transport or PrefilterTransport()
            started, last_finished = self.clock(), None
            with self.connect() as db:
                meta = self.meta(db)
                if continuation:
                    cooldown_protocol.consume(self, db, meta, phase, authorization)
                if phase not in meta["phases"]:
                    meta["phases"][phase] = {
                        "state": "Ready",
                        "target": meta["counts"]["valid"] - meta["counts"]["processed"],
                        "processed": 0,
                        "elapsed_seconds": 0,
                        "stop_reason": "",
                    }
                p = meta["phases"][phase]
                baseline = p["elapsed_seconds"]
                p.update(state="Running", stop_reason="")
                self.audit(
                    db,
                    "authorization",
                    {
                        "phase": phase,
                        "token": bulk.digest(authorization.encode()) if continuation else authorization,
                        "settings": config,
                    },
                )
                self.save(db, meta)
                db.commit()
                if continuation:
                    guard.checkpoint_cooldown(
                        cooldown_protocol.owner(self), meta["cooldown"]["restriction"], "Released"
                    )
                    guard.clear_pending()
                try:
                    while p["processed"] < p["target"] and not self.cancel.is_set():
                        rows = db.execute(
                            "SELECT * FROM names WHERE state='Pending' AND id<=? ORDER BY id LIMIT ?",
                            (
                                meta["pilot_cutoff"] if phase == "pilot" else 9223372036854775807,
                                min(config["batch_size"], p["target"] - p["processed"]),
                            ),
                        ).fetchall()
                        if not rows:
                            raise ValueError("Queue checkpoint mismatch")
                        delay = (
                            max(0, config["delay"] - (self.clock() - last_finished))
                            if last_finished is not None
                            else 0
                        )
                        if last_finished is None:
                            last = db.execute(
                                "SELECT response_at FROM requests WHERE response_at IS NOT NULL "
                                "ORDER BY id DESC LIMIT 1"
                            ).fetchone()
                            if last:
                                delay = max(
                                    0,
                                    config["delay"]
                                    - (self.wall() - datetime.fromisoformat(last[0])).total_seconds(),
                                )
                        if delay:
                            self.audit(db, "pacing_intent", {"seconds": delay})
                            db.commit()
                            before = self.clock()
                            cancelled = self.wait(delay)
                            self.audit(db, "pacing_completed", {"seconds": self.clock() - before})
                            db.commit()
                            if cancelled or self.cancel.is_set():
                                break
                        if self.cancel.is_set():
                            break
                        names = [r["username"] for r in rows]
                        identity = db.execute(
                            "INSERT INTO requests(phase,state,names,request_at) VALUES(?,'Intent',?,?)",
                            (phase, json.dumps(names), self.wall().isoformat()),
                        ).lastrowid
                        db.executemany(
                            "UPDATE names SET state='InFlight',request_id=? WHERE id=?",
                            ((identity, r["id"]) for r in rows),
                        )
                        meta["counts"]["attempts"] += 1
                        p["attempts"] = p.get("attempts", 0) + 1
                        self.save(db, meta)
                        guard.mark_pending(self.manifest()["fingerprint"] + ":" + str(identity))
                        db.commit()  # Intent precedes dispatch; unresolved intent is never replayed.
                        response = transport(
                            {"kind": "bulk", "method": "POST", "url": bulk.BULK_URL, "usernames": names}
                        )
                        last_finished = self.clock()
                        evidence = bulk.public_response(response)
                        db.execute(
                            "UPDATE requests SET state='Response',response_at=?,evidence=?,latency=? "
                            "WHERE id=? AND state='Intent'",
                            (self.wall().isoformat(), json.dumps(evidence), response.elapsed, identity),
                        )
                        db.commit()  # Raw response precedes classification.
                        request = db.execute("SELECT * FROM requests WHERE id=?", (identity,)).fetchone()
                        p["elapsed_seconds"] = baseline + self.clock() - started
                        meta["peak_memory_bytes"] = memory_peak()
                        stop = self._finish(db, request, response, meta)
                        db.commit()
                        if stop:
                            cooldown_protocol.publish(self, guard, meta, stop)
                        else:
                            guard.clear_pending()
                        self.updated()
                        if stop:
                            break
                except BaseException as exc:
                    db.rollback()
                    meta = self.meta(db)
                    p = meta["phases"][phase]
                    p.update(
                        state="ReviewRequired", stop_reason="unexpected_execution_error:" + type(exc).__name__
                    )
                    self.audit(db, "execution_error", {"type": type(exc).__name__})
                    if not isinstance(exc, (Exception, KeyboardInterrupt)):
                        raise
                finally:
                    if p["state"] == "Running":
                        p.update(
                            state="Completed" if p["processed"] == p["target"] else "Paused",
                            stop_reason="phase_boundary" if p["processed"] == p["target"] else "cancelled",
                        )
                    p["elapsed_seconds"] = baseline + self.clock() - started
                    self.save(db, meta)
                    db.commit()
                    if self.transport is None:
                        transport.close()
            write_new(self.directory / ("report-" + uuid4().hex + ".json"), self.summary())
            self.updated()
        return self.summary()

    @staticmethod
    def safe_cell(value):
        return "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value

    def export(self, destination=None):
        """Stream category files; restart only unfinished files of the same snapshot."""
        with RobloxActivity(self.path):
            summary = self.summary()
            root = Path(destination) if destination else self.directory / "exports" / uuid4().hex
            root.mkdir(parents=True, exist_ok=True)
            state_path = root / "export-state.json"
            if state_path.exists():
                state = bulk.strict_json(state_path.read_bytes())
                if (state["fingerprint"], state["revision"]) != (
                    summary["manifest"]["fingerprint"],
                    summary["revision"],
                ):
                    raise ValueError("تغيرت النتائج منذ التصدير الجزئي؛ اختر مجلدًا جديدًا.")
            else:
                state = {
                    "fingerprint": summary["manifest"]["fingerprint"],
                    "revision": summary["revision"],
                    "files": {},
                    "complete": False,
                }
                write_new(state_path, state)
                write_new(root / "manifest.json", summary["manifest"])
                write_new(root / "summary.json", summary)

            def checkpoint():
                temporary = root / (uuid4().hex + ".partial")
                write_new(temporary, state)
                os.replace(temporary, state_path)

            state["complete"] = False
            checkpoint()
            with self.connect() as db:
                specs = [
                    (
                        group + "." + ext,
                        "SELECT *, CASE WHEN state='LocalInvalid' THEN 'local_syntax' "
                        "WHEN state='Pending' THEN 'not_requested' ELSE 'roblox_bulk_account_lookup' END "
                        "AS evidence_source FROM names WHERE " + where + " ORDER BY id",
                        ext,
                    )
                    for group, where in GROUPS.items()
                    for ext in ("csv", "json")
                ]
                specs += [
                    (
                        "individual-candidates.txt",
                        "SELECT username FROM names WHERE " + GROUPS["candidates"] + " ORDER BY id",
                        "txt",
                    ),
                    ("requests.json", "SELECT * FROM requests ORDER BY id", "json"),
                    ("decisions.json", "SELECT * FROM decisions ORDER BY id", "json"),
                    ("audit.json", "SELECT * FROM audit ORDER BY id", "json"),
                ]
                for filename, query, kind in specs:
                    if filename in state["files"]:
                        if (
                            not (root / filename).is_file()
                            or self.hash_source(root / filename) != state["files"][filename]["sha256"]
                        ):
                            raise ValueError("تغير ملف تصدير مكتمل؛ لم يُستبدل ولم يُعلن الاكتمال.")
                        continue
                    temporary = root / (filename + "." + uuid4().hex + ".partial")
                    count = 0
                    with temporary.open(
                        "x", encoding="utf-8-sig" if kind == "csv" else "utf-8", newline=""
                    ) as stream:
                        cursor = db.execute(query)
                        fields = [col[0] for col in cursor.description]
                        if kind == "csv":
                            writer = csv.writer(stream)
                            writer.writerow(fields)
                        elif kind == "json":
                            stream.write("[\n")
                        for row in cursor:
                            if self.cancel.is_set():
                                checkpoint()
                                return root
                            if kind == "csv":
                                writer.writerow([self.safe_cell(v) for v in row])
                            elif kind == "txt":
                                stream.write(row[0] + "\n")
                            else:
                                stream.write(
                                    (",\n" if count else "") + json.dumps(dict(row), ensure_ascii=False)
                                )
                            count += 1
                        if kind == "json":
                            stream.write("\n]\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    if (root / filename).exists():
                        raise ValueError("ملف تصدير سابق غير معتمد؛ لم يُستبدل.")
                    fingerprint = self.hash_source(temporary)
                    temporary.rename(root / filename)
                    state["files"][filename] = {"rows": count, "sha256": fingerprint}
                    checkpoint()
                    self.updated()
            state["complete"] = True
            checkpoint()
            return root
