"""Opt-in bounded transport recovery; provider restrictions are never released here."""

from __future__ import annotations

import errno
import json
import socket
import ssl
import urllib.error
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

MAX_CANDIDATE_RETRIES = 3
MAX_JOB_RETRIES = 12
BACKOFF_SECONDS = (30, 60, 120)
EVENT_LIMIT = 100
STATES = {
    "Armed": "ليلي مفعّل؛ ينتظر بدءًا صريحًا",
    "Running": "تشغيل ليلي نشط",
    "Connectivity": "انتظار الاتصال / محاولة مؤجلة",
    "Delay": "انتظار الموعد المسموح",
    "Interrupted": "متقطع؛ قابل للاستعادة الليلية",
    "Stopped": "متوقف يدويًا؛ لن يبدأ تلقائيًا",
    "Review": "يتطلب تدخّلًا يدويًا",
    "Completed": "مكتمل",
    "Disabled": "التشغيل الليلي معطّل",
    "Superseded": "متروك لصالح عملية أخرى؛ لا استئناف تلقائي",
}


def transient_exception(error):
    """TLS, configuration errors and opaque exceptions are not transport retry evidence."""
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    if isinstance(error, ssl.SSLError):
        return False
    if isinstance(error, (TimeoutError, ConnectionError, socket.gaierror)):
        return True
    return isinstance(error, OSError) and (
        error.errno in {errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ETIMEDOUT}
        or getattr(error, "winerror", None) in {10050, 10051, 10053, 10054, 10060, 10061, 11001, 11002}
    )


def retryable_response(response):
    return bool(
        getattr(response, "retryable", False)
        and response.error
        and response.status is None
        and not response.body
        and not response.headers
        and not getattr(response, "challenge", False)
    )


def transport_failure(response, error):
    # Optional metadata is deliberately outside the preserved protocol response schema/parser.
    response.retryable = transient_exception(error)
    return response


def now():
    return datetime.now(UTC)


class OvernightStore:
    """Small additive per-job metadata; candidates and observations remain in existing tables."""

    def __init__(self, store, platform):
        if platform not in ("Roblox", "X"):
            raise ValueError("Unsupported overnight platform")
        self.store, self.platform = store, platform
        self.prefix = "roblox" if platform == "Roblox" else "x"
        with store.lock:
            if store.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='overnight_jobs' AND type='table'"
            ).fetchone():
                return
        with store.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS overnight_jobs(platform TEXT NOT NULL,run_id TEXT NOT NULL,"
                "enabled INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(platform,run_id))"
            )

    def _read(self, db, identity):
        row = db.execute(
            "SELECT enabled,data FROM overnight_jobs WHERE platform=? AND run_id=?", (self.platform, identity)
        ).fetchone()
        return dict(json.loads(row["data"]), enabled=bool(row["enabled"])) if row else None

    def get(self, identity):
        with self.store.lock:
            return self._read(self.store.db, identity) if identity else None

    def may_resume(self, identity):
        data = self.get(identity) or {}
        return bool(
            data.get("enabled")
            and data.get("auto_resume")
            and data.get("state") in ("Running", "Connectivity", "Delay", "Interrupted")
        )

    def _save(self, db, identity, data, event=None, *, at=None):
        stamp = (at or now()).isoformat()
        if event:
            events = data.get("events", [])
            events.append(
                {
                    "at": stamp,
                    "event": event,
                    "reason": data.get("last_error", ""),
                    "attempt": data.get("attempts", 0),
                    "retry_at": data.get("retry_at"),
                }
            )
            data["events"] = events[-EVENT_LIMIT:]
            data["event_count"] = data.get("event_count", 0) + 1
        data["updated_at"] = stamp
        db.execute(
            "INSERT INTO overnight_jobs VALUES(?,?,?,?) ON CONFLICT(platform,run_id) DO UPDATE SET "
            "enabled=excluded.enabled,data=excluded.data",
            (self.platform, identity, int(data["enabled"]), json.dumps(data)),
        )

    def enable(self, identity, enabled, *, active=False, timeout_ms=None):
        with self.store.transaction(timeout_ms=timeout_ms) as db:
            if not db.execute(f"SELECT 1 FROM {self.prefix}_runs WHERE id=?", (identity,)).fetchone():
                raise ValueError("العملية غير موجودة.")
            data = self._read(db, identity) or {}
            data.update(
                enabled=bool(enabled),
                auto_resume=bool(enabled and active),
                state="Running" if enabled and active else "Armed" if enabled else "Disabled",
            )
            if active and enabled:
                data["session_started_at"] = now().isoformat()
            self._save(db, identity, data, "enabled" if enabled else "disabled")

    def begin(self, identity, *, automatic=False, clock=now, cancel=None):
        with self.store.transaction() as db:
            data = self._read(db, identity)
            if not data or not data["enabled"] or (cancel and cancel.is_set()):
                return
            for row in db.execute(
                "SELECT run_id FROM overnight_jobs WHERE platform=? AND run_id<>?", (self.platform, identity)
            ).fetchall():
                other = self._read(db, row["run_id"])
                if other.get("auto_resume"):
                    other.update(auto_resume=False, state="Superseded")
                    self._save(db, row["run_id"], other, "superseded", at=clock())
            if not automatic:
                data.update(
                    attempts=0,
                    retries_performed=0,
                    retry_started=False,
                    candidate_retries=0,
                    position=None,
                    session_started_at=clock().isoformat(),
                    last_error="",
                )
            data.update(state="Running", auto_resume=True)
            self._save(db, identity, data, "automatically_resumed" if automatic else "started", at=clock())

    def retry(self, task, response, finished):
        if not retryable_response(response):
            return False
        identity = task["run_id"]
        with self.store.transaction() as db:
            data = self._read(db, identity)
            if not data or not data["enabled"] or not data.get("auto_resume"):
                return False
            retries = data.get("candidate_retries", 0) if data.get("position") == task["position"] else 0
            data["last_error"] = response.error[
                :80
            ]  # Transport codes only, never exception messages or routes.
            if retries >= MAX_CANDIDATE_RETRIES or data.get("attempts", 0) >= MAX_JOB_RETRIES:
                data.update(auto_resume=False, state="Review")
                self._save(db, identity, data, "retry_budget_exhausted", at=finished)
                return False
            data.update(
                state="Connectivity",
                position=task["position"],
                candidate_retries=retries + 1,
                attempts=data.get("attempts", 0) + 1,
                retry_started=False,
                retry_at=(finished + timedelta(seconds=BACKOFF_SECONDS[retries])).isoformat(),
            )
            # Release and retry metadata are atomic: restart can distinguish a known failed transport
            # from an actually uncertain in-flight request. No result is invented or committed here.
            changed = db.execute(
                f"UPDATE {self.prefix}_queue SET state='Pending' "
                "WHERE run_id=? AND position=? AND state='InFlight'",
                (identity, task["position"]),
            ).rowcount
            if changed != 1:
                raise ValueError("Retry candidate is not in flight")
            self._save(db, identity, data, "temporary_transport_failure", at=finished)
            self._save(db, identity, data, "retry_scheduled", at=finished)
            return True

    def requesting(self, task, *, clock=now):
        with self.store.transaction() as db:
            data = self._read(db, task["run_id"])
            if not data or not data["enabled"] or not data.get("auto_resume"):
                return
            if data.get("retry_at") and not data.get("retry_started"):
                data["retries_performed"] = data.get("retries_performed", 0) + 1
                data["retry_started"] = True
            data.update(state="Running", waiting_until=None)
            self._save(db, task["run_id"], data, at=clock())

    def deadline(self, identity, control):
        data = self.get(identity) or {}
        return max(control.get("not_before") or "", data.get("retry_at") or "")

    def waiting(self, identity, deadline, *, clock=now):
        with self.store.transaction() as db:
            data = self._read(db, identity)
            if not data or not data["enabled"] or not data.get("auto_resume"):
                return
            state = "Connectivity" if data.get("retry_at") else "Delay"
            if data.get("state") != state or data.get("waiting_until") != deadline:
                data.update(state=state, waiting_until=deadline)
                self._save(db, identity, data, at=clock())

    def success(self, identity, *, clock=now):
        with self.store.transaction() as db:
            data = self._read(db, identity)
            if not data or not data["enabled"] or not data.get("auto_resume"):
                return
            recovered = bool(data.get("retry_at"))
            data.update(
                state="Running", retry_at=None, waiting_until=None, candidate_retries=0, last_error=""
            )
            self._save(db, identity, data, "connectivity_recovered" if recovered else None, at=clock())

    def stop(self, identity, *, timeout_ms=None):
        if self.get(identity) is None:
            return
        with self.store.transaction(timeout_ms=timeout_ms) as db:
            data = self._read(db, identity)
            if data:
                if data.get("state") == "Stopped" and not data.get("auto_resume"):
                    return
                data.update(auto_resume=False, state="Stopped")
                self._save(db, identity, data, "manually_stopped")

    def end(self, identity, state, error="", *, clock=now):
        with self.store.transaction() as db:
            data = self._read(db, identity)
            if not data or not data["enabled"]:
                return
            if data.get("state") == "Stopped":
                return
            if state in ("Restricted", "Error"):
                data.update(
                    state="Review",
                    auto_resume=False,
                    last_error=error[:80],
                    retry_at=None,
                    waiting_until=None,
                )
                event = "restriction_or_manual_review"
            elif state == "Completed":
                data.update(state="Completed", auto_resume=False, retry_at=None, waiting_until=None)
                event = "completed"
            else:
                data.update(state="Interrupted" if data.get("auto_resume") else "Armed")
                event = "interrupted" if data.get("auto_resume") else "waiting_for_explicit_start"
            self._save(db, identity, data, event, at=clock())

    def startup_candidates(self, repo):
        """Called once on launch after ordinary in-flight recovery; never used as a restart timer."""
        with self.store.lock:
            identities = [
                row[0]
                for row in self.store.db.execute(
                    "SELECT run_id FROM overnight_jobs WHERE platform=? AND enabled=1", (self.platform,)
                )
            ]
        eligible = []
        for identity in identities:
            data = self.get(identity)
            if not data.get("auto_resume"):
                continue
            try:
                run = repo.progress(identity)
            except ValueError:
                run = None
            if run is None:
                self.end(identity, "Error", "missing_job_review_required")
                continue
            if run["state"] == "Completed":
                self.end(identity, "Completed")
                continue
            # A Roblox uncertain recovery is deliberately not automatically rechecked either.
            with self.store.lock:
                uncertain = self.store.db.execute(
                    f"SELECT 1 FROM {self.prefix}_checks WHERE run_id=? AND classification='Unknown' "
                    "AND checked_at>=? LIMIT 1",
                    (identity, data.get("session_started_at", "")),
                ).fetchone()
            if repo.control().get("blocked") or run["state"] not in ("Ready", "Paused") or uncertain:
                self.end(identity, "Restricted", "startup_review_required")
                continue
            pending = run.get("pending", run.get("counts", {}).get("Pending", 0))
            if not pending:
                repo.end(identity, "Completed")
                self.end(identity, "Completed")
                continue
            eligible.append(identity)
        return eligible[:1]

    def label(self, identity):
        data = self.get(identity)
        if not data or not data["enabled"]:
            return STATES["Disabled"]
        text = STATES.get(data.get("state"), data.get("state", ""))
        text += f" • محاولات إعادة فعلية: {data.get('retries_performed', 0)}/{MAX_JOB_RETRIES}"
        if data.get("waiting_until") or data.get("retry_at"):
            value = data.get("waiting_until") or data["retry_at"]
            stamp = (
                datetime.fromisoformat(value)
                .astimezone(ZoneInfo("Asia/Riyadh"))
                .strftime("%Y/%m/%d %H:%M:%S")
            )
            text += " • الموعد الأدنى (الرياض): \u2066" + stamp + "\u2069"
        if data.get("last_error"):
            text += " • " + data["last_error"]
        return text
