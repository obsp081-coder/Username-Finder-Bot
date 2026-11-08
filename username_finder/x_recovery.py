"""Explicit, single-request recovery reviews; never restart or rewrite historical jobs."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from uuid import uuid4

from . import x
from .x_runner import deadline

TEST_USERNAME = "OpenAI"


class XRecovery:
    def __init__(self, repo, *, clock=None):
        self.repo = repo
        self.clock = clock or (lambda: datetime.now(UTC))

    def events(self, *, limit=None):
        with self.repo.store.lock:
            rows = self.repo.store.db.execute(
                "SELECT * FROM x_recovery_events ORDER BY rowid DESC" + (" LIMIT ?" if limit else ""),
                (limit,) if limit else (),
            ).fetchall()
            return [
                dict(
                    row,
                    restriction=json.loads(row["restriction"]),
                    connection=json.loads(row["connection"]),
                    evidence=json.loads(row["evidence"]),
                )
                for row in rows
            ]

    def status(self):
        with self.repo.store.lock:
            info = self.repo.restriction_info()
            events = self.events(limit=1)
            latest = events[0] if events else None
            status = dict(info=info, latest=latest, eligible=False, release_eligible=False, wait_until=None)
            if not info:
                return dict(status, state="Released" if latest and latest["released_at"] else "None")
            matching = bool(
                latest
                and latest["restriction"]["check_id"] == info["check_id"]
                and latest["restriction"]["raw_sha256"] == info["raw_sha256"]
            )
            status["latest"] = latest if matching else None
            if (
                info["http_status"] != 429
                or info["reason"] != "unexpected_http_status:429"
                or not info["connection"]
            ):
                return dict(status, state="ReviewRequired")
            if info["invalid_wait_headers"]:
                return dict(status, state="ReviewRequired")
            if matching and latest["evidence"]:
                probe_wait = x.restriction_wait(
                    latest["evidence"].get("headers", {}), latest["evidence"].get("checked_at")
                )
                if probe_wait["invalid_wait_headers"]:
                    return dict(status, state="ReviewRequired")
            dates = []
            for value in (
                info["minimum_retry_at"],
                info["local_not_before"],
                self.repo.control().get("recovery_not_before"),
            ):
                if not value:
                    continue
                try:
                    date = datetime.fromisoformat(value)
                    if date.tzinfo is None:
                        raise ValueError
                    dates.append(date)
                except (ValueError, TypeError):
                    return dict(status, state="ReviewRequired")
            if dates and max(dates) > self.clock():
                return dict(status, state="Waiting", wait_until=max(dates).isoformat())
            if self.repo.store.db.execute(
                "SELECT 1 FROM x_recovery_events WHERE state='InFlight' LIMIT 1"
            ).fetchone():
                return dict(status, state="InFlight")
            if matching and latest["state"] == "Succeeded" and not latest["released_at"]:
                return dict(status, state="AwaitingRelease", release_eligible=True)
            state = (
                latest["state"]
                if matching
                else "NewRestriction"
                if latest and latest["released_at"]
                else "Eligible"
            )
            return dict(status, state=state, eligible=True)

    def begin(self, *, confirmed=False):
        if not confirmed:
            raise ValueError("يلزم تأكيد صريح قبل طلب استعادة واحد.")
        with self.repo.store.transaction() as db:
            status = self.status()
            if not status["eligible"] or db.execute("SELECT 1 FROM x_runs WHERE state='Running'").fetchone():
                raise ValueError("فحص الاستعادة غير مسموح الآن؛ راجع الانتظار والحالة الحالية.")
            identity = uuid4().hex
            info = status["info"]
            db.execute(
                "INSERT INTO x_recovery_events(id,restriction,started_at,connection,state) "
                "VALUES(?,?,?,?,'InFlight')",
                (identity, json.dumps(info), self.clock().isoformat(), json.dumps(info["connection"])),
            )
            return identity, info["connection"]

    def check(self, *, confirmed=False, fetcher=None, cancel=None, updated=lambda: None):
        cancel = cancel or threading.Event()
        if cancel.is_set():
            return None
        identity, route = self.begin(confirmed=confirmed)
        updated()
        try:
            response = (
                x.protocol.Response(error="cancelled_before_dispatch", complete=False)
                if cancel.is_set()
                else fetcher(TEST_USERNAME, 15.0)
                if fetcher
                else x.protocol.fetch(TEST_USERNAME, 15.0, connection=route)
            )
        except Exception as error:
            response = x.protocol.Response(error=type(error).__name__, complete=False)
        try:
            finished = self.clock()
            result = x.result_for(TEST_USERNAME, response, checked_at=finished.isoformat())
            result["connection"] = route
            success = response.status == 200 and result["classification"] != "Unknown"
            state = "Cancelled" if cancel.is_set() else "Succeeded" if success else "Failed"
            wait_until = deadline(response.headers, finished, 5)
        except BaseException:
            with self.repo.store.transaction() as db:
                db.execute("UPDATE x_recovery_events SET state='Uncertain' WHERE id=?", (identity,))
            raise
        with self.repo.store.transaction() as db:
            changed = db.execute(
                "UPDATE x_recovery_events SET state=CASE WHEN state='InFlight' THEN ? ELSE state END,"
                "evidence=? WHERE id=? AND released_at IS NULL",
                (state, json.dumps(result), identity),
            ).rowcount
            if changed:
                self.repo._control(
                    db,
                    recovery_not_before=max(self.repo.control().get("recovery_not_before", ""), wait_until),
                )
        return identity

    def release(self, *, acknowledged=False):
        if not acknowledged:
            raise ValueError("يلزم إقرار صريح بالمخاطر قبل تحرير القيد.")
        with self.repo.store.transaction() as db:
            status = self.status()
            if not status["release_eligible"]:
                raise ValueError("لم ينجح فحص استعادة صالح أو لم تنتهِ مهلة الانتظار.")
            event = status["latest"]
            db.execute(
                "UPDATE x_recovery_events SET released_at=? WHERE id=? AND released_at IS NULL",
                (self.clock().isoformat(), event["id"]),
            )
            last_rowid = db.execute("SELECT COALESCE(MAX(rowid),0) FROM x_runs").fetchone()[0]
            self.repo._control(
                db,
                blocked=None,
                preferred_interval_seconds=5.0,
                not_before=max(
                    self.repo.control().get("not_before", ""),
                    self.repo.control().get("recovery_not_before", ""),
                ),
                recovery_gate=dict(
                    event_id=event["id"], last_run_rowid=last_rowid, connection=event["connection"]
                ),
            )

    def can_start(self, identity):
        with self.repo.store.lock:
            gate = self.repo.control().get("recovery_gate")
            if not gate:
                return True
            row = self.repo.store.db.execute(
                "SELECT rowid,interval_seconds FROM x_runs WHERE id=?", (identity,)
            ).fetchone()
            return bool(
                row
                and row["rowid"] > gate["last_run_rowid"]
                and row["interval_seconds"] >= 5
                and self.repo.connection_for_run(identity) == gate["connection"]
            )
