"""Local, append-only mode/connection reviews. Never release legacy safety evidence."""

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .roblox_activity import RobloxActivity
from .roblox_restriction import _last_observation, checking_permission

POLICY = "rovalid-reviewed-permission-v1"
MODES = ("two_stage", "validator")
CONNECTIONS = ("direct", "private", "sources")
ENDPOINTS = {
    "two_stage": ["users.roblox.com/v1/usernames/users", "auth.roblox.com/v1/usernames/validate"],
    "validator": ["auth.roblox.com/v1/usernames/validate"],
}
STATE_LABELS = {
    "ReviewRequired": "مراجعة مطلوبة",
    "Authorized": "مفوّض لهذا النطاق",
    "Denied": "قرار برفض التشغيل",
    "HistoricalRestriction": "قيد محلي تاريخي محفوظ",
    "BlockedNewRestriction": "حجب بعد قيد جديد مرصود",
    "PendingOrRestricted": "عملية غير محسومة أو قيد قائم",
    "CooldownOrMalformed": "مهلة محفوظة أو دليلها غير سليم",
    "SafetyBlocked": "حاجز أمان قائم",
    "BlockedAfterReview": "تعذر استعمال التفويض السابق",
}


def scope(mode, connection):
    if mode not in MODES or connection not in CONNECTIONS:
        raise ValueError("Unsupported review scope")
    return dict(mode=mode, connection=connection, endpoints=list(ENDPOINTS[mode]))


def journal_path(database):
    # Separate from engine checkpoints and the historical SQLite store.
    return Path(database).resolve().with_name(Path(database).name + ".rovalid-reviews.jsonl")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _journal(database):
    path = journal_path(database)
    records, previous = [], "0" * 64
    if not path.exists():
        return records
    with path.open(encoding="utf8") as stream:
        for line in stream:
            if len(line) > 16384 or not line.endswith("\n"):
                raise ValueError("Incomplete review audit")
            item = json.loads(line)
            if not isinstance(item, dict) or set(item) != {
                "policy",
                "scope",
                "fingerprint",
                "snapshot",
                "decision",
                "reviewer",
                "evidence",
                "at",
                "previous",
                "hash",
            }:
                raise ValueError("Malformed review audit")
            body = {k: v for k, v in item.items() if k != "hash"}
            if item["hash"] != digest(body) or item["previous"] != previous or item["policy"] != POLICY:
                raise ValueError("Conflicting review audit")
            if item["scope"] != scope(item["scope"]["mode"], item["scope"]["connection"]):
                raise ValueError("Invalid review scope")
            if item["decision"] not in ("review", "authorize", "deny"):
                raise ValueError("Invalid review decision")
            for field in ("reviewer", "evidence"):
                if not isinstance(item[field], str) or not 1 <= len(item[field].strip()) <= 1000:
                    raise ValueError("Missing review evidence")
            if datetime.fromisoformat(item["at"]).tzinfo is None:
                raise ValueError("Invalid review timestamp")
            if digest(item["snapshot"]) != item["fingerprint"]:
                raise ValueError("Invalid reviewed fingerprint")
            records.append(item)
            previous = item["hash"]
    return records


def journal(database):
    try:
        # Readers must not use an authorization while its writer is still flushing it.
        with _audit_lock(database):
            return _journal(database)
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ValueError("Malformed review audit") from exc


def _file_evidence(path):
    if not path.exists():
        return None
    sha, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while block := stream.read(65536):
            sha.update(block)
            size += len(block)
    return dict(sha256=sha.hexdigest(), bytes=size)


def inspect(database, *, lock_held=False):
    from . import rovalid

    guard = RobloxActivity(database)
    with rovalid.readonly_repo(database) as repo:
        # The legacy guard is intentionally still cross-mode: a bare marker does not
        # establish an endpoint-specific exemption. No authorizations bypass it.
        status = checking_permission(repo, purpose="bulk", lock_held=lock_held)
        control = repo.control()
        selected = {
            key: control.get(key)
            for key in (
                "blocked",
                "active_run",
                "not_before",
                "unknown_streak",
                "restriction_streak",
            )
        }
        inflight = bool(
            repo.store.db.execute(
                "SELECT 1 FROM roblox_queue WHERE run_id IN (SELECT id FROM roblox_runs) "
                "AND state='InFlight' LIMIT 1"
            ).fetchone()
        )
        observation = _last_observation(repo)
        row = repo.store.db.execute(
            "SELECT id,evidence FROM roblox_checks WHERE source='roblox-api' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_individual = dict(id=row[0], sha256=hashlib.sha256(row[1].encode()).hexdigest()) if row else None
    markers = {name: _file_evidence(getattr(guard, name)) for name in ("block", "pending", "cooldown")}
    snapshot = dict(
        policy=POLICY,
        source=rovalid.REVISION,
        database=hashlib.sha256(str(Path(database).resolve()).encode()).hexdigest(),
        markers=markers,
        control=selected,
        inflight=inflight,
        last_individual=last_individual,
    )
    state = "ReviewRequired"
    if markers["block"]:
        state = "HistoricalRestriction"
        with guard.block.open("rb") as stream:
            if stream.read(256) == b"rovalid_review_required":
                state = "BlockedNewRestriction"
    elif markers["cooldown"] and not status["allowed"]:
        state = "CooldownOrMalformed"
    elif markers["pending"] and not status["allowed"]:
        state = "PendingOrRestricted"
    elif not status["allowed"]:
        state = "SafetyBlocked"
    return dict(
        snapshot=snapshot, fingerprint=digest(snapshot), local=status, state=state, observation=observation
    )


def permission(database, *, mode="two_stage", connection="direct", lock_held=False):
    try:
        target = scope(mode, connection)
        observed = inspect(database, lock_held=lock_held)
        records = journal(database)
        matching = [r for r in records if r["scope"] == target]
        last = matching[-1] if matching else None
        local = observed["local"]
        allowed = bool(
            local["allowed"]
            and last
            and last["decision"] == "authorize"
            and last["fingerprint"] == observed["fingerprint"]
        )
        state = "Authorized" if allowed else observed["state"]
        if last and last["decision"] == "deny":
            state = "Denied"
        elif last and last["decision"] == "authorize" and not allowed:
            state = (
                "BlockedNewRestriction"
                if observed["state"] == "BlockedNewRestriction"
                else "BlockedAfterReview"
            )
        reason = (
            f"النمط: {mode} • الاتصال: {connection} • {STATE_LABELS[state]} ({state}).\n"
            f"الحاجز المحلي: {local['reason']}\n"
            f"{observed['observation']}\n"
            + (
                "تفويض صريح مطابق للدليل الحالي؛ يلزم تأكيد التشغيل أيضًا."
                if allowed
                else "يلزم تدقيق الدليل وتسجيل قرار محدد؛ "
                "القيد التاريخي HTTP 429 لا يحرره نجاح CLI أو المتصفح."
            )
            + (
                f"\nآخر قرار مسجل لهذا النطاق: {last['decision']} • {last.get('at', 'غير مسجل')}."
                if last
                else ""
            )
            + (
                "\nتعذر استعمال التفويض: قيد قائم أو دليل أمان تغير؛ راجع الحالة وPending بعد الإيقاف."
                if state == "BlockedAfterReview"
                else ""
            )
            + "\nحالة الموفر الآن: غير معروفة؛ عرض الحالة محلي ولا يرسل طلبًا."
        )
        return dict(
            allowed=allowed,
            reason=reason,
            state=state,
            fingerprint=observed["fingerprint"],
            snapshot=observed["snapshot"],
            local_allowed=local["allowed"],
            scope=target,
        )
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, AttributeError, IndexError):
        return dict(
            allowed=False,
            local_allowed=False,
            state="ReviewInvalid",
            reason="تعذر تدقيق الحالة أو سجل التفويض؛ لا إرسال قبل المراجعة.",
        )


@contextmanager
def _audit_lock(database):
    path = journal_path(database).with_suffix(".lock")
    with path.open("a+b") as stream:
        if not path.stat().st_size:
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


def record_review(database, *, mode, connection, decision, reviewer, evidence, fingerprint):
    """Explicit local action only. Authorization additionally needs the ordinary shared guard."""
    target = scope(mode, connection)
    if decision not in ("review", "authorize", "deny"):
        raise ValueError("Unsupported decision")
    if any(not isinstance(v, str) or not 1 <= len(v.strip()) <= 1000 for v in (reviewer, evidence)):
        raise ValueError("Reviewer and evidence references required")

    def append():
        records = _journal(database)
        observed = inspect(database, lock_held=decision == "authorize")
        if observed["fingerprint"] != fingerprint:
            raise ValueError("Safety evidence changed; inspect again")
        if decision == "authorize" and not observed["local"]["allowed"]:
            raise ValueError("Active or uncertain restriction cannot be authorized here")
        item = dict(
            policy=POLICY,
            scope=target,
            fingerprint=fingerprint,
            snapshot=observed["snapshot"],
            decision=decision,
            reviewer=reviewer.strip(),
            evidence=evidence.strip(),
            at=datetime.now(UTC).isoformat(),
            previous=records[-1]["hash"] if records else "0" * 64,
        )
        item["hash"] = digest(item)
        with journal_path(database).open("a", encoding="utf8", newline="\n") as stream:
            stream.write(json.dumps(item, ensure_ascii=True, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return item

    try:
        with _audit_lock(database):
            if decision == "authorize":
                with RobloxActivity(database):
                    return append()
            return append()
    except (sqlite3.Error, TypeError, KeyError, AttributeError, IndexError) as exc:
        raise ValueError("Cannot verify review evidence") from exc
