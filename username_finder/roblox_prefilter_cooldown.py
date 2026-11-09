"""Future-only, one-shot continuation of unattempted Bulk work after a reviewed 429."""

import base64
import hmac
import json
import math
from uuid import uuid4

from . import roblox_bulk_protocol as bulk
from .roblox_activity import RobloxActivity
from .roblox_cooldown import retry_after, utc


def owner(service):
    return service.manifest()["fingerprint"] + ":" + bulk.digest(str(service.path.resolve()).encode())


def capture(service, db, meta, request, stop):
    """Called inside the decision transaction, after raw evidence was committed."""
    if stop != "http_429" or service.manifest().get("cooldown_recovery") != 1:
        return
    raw = json.loads(request["evidence"])
    count = db.execute("SELECT count(*) FROM requests WHERE stop='http_429'").fetchone()[0]
    if count != 1:
        if meta.get("cooldown"):
            meta["cooldown"].update(stage="Escalated", escalation_request_id=request["id"])
        service.audit(db, "cooldown_escalated", {"request_id": request["id"], "count": count})
        return
    try:
        body = base64.b64decode(raw["body_base64"], validate=True)
        parsed = retry_after(raw["headers"].get("retry-after"), request["response_at"])
        if (
            raw["status"] != 429
            or raw["complete"] is not True
            or raw["error"]
            or raw["challenge"]
            or raw["body_omitted_for_sensitive_fields"]
            or bulk.digest(body) != raw["body_sha256"]
            or raw["headers"].get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"
            or raw["headers"].get("content-encoding", "identity").lower() not in ("", "identity")
            or not parsed["wait_supported"]
            or not parsed["source"].startswith("retry_after_")
            or any(word in body.lower() for word in (b"captcha", b"challenge"))
            or not isinstance(bulk.strict_json(body), (dict, list))
        ):
            return
    except (KeyError, TypeError, ValueError, OverflowError):
        return
    cooldown = {
        "version": 1,
        "request_id": request["id"],
        "phase": request["phase"],
        "response_at": request["response_at"],
        "evidence_sha256": bulk.digest(bulk.encoded(raw)),
        "not_before": parsed["not_before"],
        "stage": "PendingReview",
    }
    cooldown["restriction"] = bulk.digest(bulk.encoded([service.manifest()["fingerprint"], cooldown]))
    meta["cooldown"] = cooldown
    meta["phases"][request["phase"]].update(state="Cooldown", stop_reason="http_429")
    service.audit(db, "cooldown_checkpoint", cooldown)
    service._cooldown_clock = (cooldown["restriction"], service.clock(), utc(service.wall()))


def validate(service, db, meta):
    from .roblox_prefilter import VERIFIED_OPERATIONAL_BATCH, policy

    manifest = service.manifest()
    c = meta.get("cooldown", {})
    if (
        manifest.get("cooldown_recovery") != 1
        or manifest["policy"] != policy()
        or manifest["settings"]["batch_size"] > VERIFIED_OPERATIONAL_BATCH
        or c.get("version") != 1
        or c.get("stage") not in ("PendingReview", "Reviewed", "Authorized", "Consumed")
        or meta["fingerprint"] != manifest["fingerprint"]
    ):
        raise ValueError("لا توجد مهلة مستقبلية قابلة للاستمرار بهذه السياسة.")
    request = db.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()
    if (
        not request
        or request["id"] != c["request_id"]
        or request["state"] != "SafetyStop"
        or request["stop"] != "http_429"
        or request["phase"] != c["phase"]
        or request["response_at"] != c["response_at"]
        or bulk.digest(bulk.encoded(json.loads(request["evidence"]))) != c["evidence_sha256"]
        or db.execute("SELECT 1 FROM requests WHERE state IN ('Intent','Response','Uncertain')").fetchone()
        or db.execute("SELECT count(*) FROM requests WHERE stop='http_429'").fetchone()[0] != 1
    ):
        raise ValueError("تغير دليل القيد أو يوجد طلب غير محسوم؛ لا استمرار.")
    expected_state = "Running" if c["stage"] == "Consumed" else "Cooldown"
    if meta["phases"][c["phase"]]["state"] not in (
        ("Running", "Paused", "Completed") if expected_state == "Running" else ("Cooldown",)
    ):
        raise ValueError("حالة الطور لا تطابق قيد المهلة.")
    for name in json.loads(request["names"]):
        row = db.execute("SELECT state,request_id FROM names WHERE key=?", (name.casefold(),)).fetchone()
        if not row or row["state"] != "Unknown" or row["request_id"] != request["id"]:
            raise ValueError("تغيرت الأسماء غير المحسومة؛ يلزم تدقيق.")
    checkpoint = db.execute(
        "SELECT data FROM audit WHERE event='cooldown_checkpoint' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not checkpoint:
        raise ValueError("Missing durable cooldown audit")
    saved = json.loads(checkpoint[0])
    if any(saved.get(k) != c.get(k) for k in saved if k != "stage"):
        raise ValueError("Cooldown audit mismatch")
    if c["stage"] != "PendingReview":
        review_row = db.execute(
            "SELECT data FROM audit WHERE event='cooldown_review' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if (
            not review_row
            or json.loads(review_row[0]).get("reviewed_revision") != c.get("reviewed_revision")
            or json.loads(review_row[0]).get("restriction") != c["restriction"]
        ):
            raise ValueError("Missing matching operator review")
    if c["stage"] in ("Authorized", "Consumed"):
        event = "cooldown_authorization" if c["stage"] == "Authorized" else "cooldown_authorization_consumed"
        approval = db.execute(
            "SELECT data FROM audit WHERE event=? ORDER BY id DESC LIMIT 1", (event,)
        ).fetchone()
        if not approval or any(
            json.loads(approval[0]).get(k) != c.get(k) for k in ("restriction", "authorization_hash")
        ):
            raise ValueError("Missing matching authorization audit")
    return c


def matches(service, marker):
    with service.connect(read_only=True) as db:
        c = validate(service, db, service.meta(db))
    return marker == {"version": 1, "owner": owner(service), "restriction": c["restriction"], "state": "Hold"}


def pending_matches(service, marker):
    with service.connect(read_only=True) as db:
        c = validate(service, db, service.meta(db))
    return marker == ("Pending:" + service.manifest()["fingerprint"] + ":" + str(c["request_id"])).encode()


def shared(service):
    return RobloxActivity(
        service.database,
        recover_cooldown=lambda marker: matches(service, marker),
        recover_pending=lambda marker: pending_matches(service, marker),
        preserve_pending=True,
    )


def publish(service, guard, meta, stop):
    c = meta.get("cooldown", {})
    if stop == "http_429" and c.get("stage") == "PendingReview":
        guard.checkpoint_cooldown(owner(service), c["restriction"], "Hold")
        # Retain the owned pending sentinel too: older checkers do not know .cooldown.
        # The complete SafetyStop evidence distinguishes it from uncertain delivery.
    else:
        guard.restrict(stop)


def _reconcile_released(service):
    """Caller holds the operation lock; clear only its consumed 429's exact sentinel."""
    with shared(service) as guard:
        pending = b""
        if guard.pending.exists():
            with guard.pending.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 1024))
                pending = stream.read(1024)
        if not pending or pending.endswith(b"Clear\n"):
            return
        with service.connect(read_only=True) as db:
            c = validate(service, db, service.meta(db))
            authorization = db.execute(
                "SELECT data FROM audit WHERE event='cooldown_authorization' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        approved = json.loads(authorization[0]) if authorization else {}
        expected = ("Pending:" + service.manifest()["fingerprint"] + ":" + str(c["request_id"])).encode()
        released = {
            "version": 1,
            "owner": owner(service),
            "restriction": c["restriction"],
            "state": "Released",
        }
        if (
            c["stage"] != "Consumed"
            or guard.cooldown_state() != released
            or pending.rsplit(b"\n", 2)[-2:] != [expected, b""]
            or approved.get("restriction") != c["restriction"]
            or approved.get("authorization_hash") != c["authorization_hash"]
            or approved.get("revision") != c["authorized_revision"] - 1
        ):
            raise ValueError("علامة Pending لا تطابق تفويض429 المستهلك وReleased؛ لا تسوية.")
        guard.clear_pending()


def sync(service):
    """Repair a killed process's publication gap without dispatching or approving work."""
    with RobloxActivity(service.path):
        with service.connect(read_only=True) as db:
            meta = service.meta(db)
            c = meta.get("cooldown")
            marker = RobloxActivity(service.database).cooldown_state()
            if not c or c["stage"] == "Escalated":
                return
            if c["stage"] == "Consumed" and marker and marker["state"] == "Released":
                _reconcile_released(service)
                return
            c = validate(service, db, meta)
        with shared(service) as guard:
            guard.checkpoint_cooldown(owner(service), c["restriction"], "Hold")
            if c["stage"] == "Consumed":
                guard.checkpoint_cooldown(owner(service), c["restriction"], "Released")
                guard.clear_pending()


def require_hold(service, c):
    marker = RobloxActivity(service.database).cooldown_state()
    if marker != {"version": 1, "owner": owner(service), "restriction": c["restriction"], "state": "Hold"}:
        raise ValueError("يلزم استعادة checkpoint المهلة محليًا قبل المراجعة أو الاستمرار.")


def ready(service, c):
    """Wall deadlines survive restart; an observed clock discontinuity never shortens a wait."""
    now, tick = utc(service.wall()), service.clock()
    anchor = getattr(service, "_cooldown_clock", None)
    if not anchor or anchor[0] != c["restriction"]:
        anchor = service._cooldown_clock = (c["restriction"], tick, now)
    elapsed = tick - anchor[1]
    if (
        not math.isfinite(elapsed)
        or elapsed < 0
        or abs((now - anchor[2]).total_seconds() - elapsed) > 1
        or now < utc(c["response_at"])
    ):
        raise ValueError("تغيرت الساعة أثناء المراجعة؛ يلزم تدقيق الوقت قبل التفويض.")
    return now >= utc(c["not_before"]) and elapsed >= (utc(c["not_before"]) - anchor[2]).total_seconds()


def check_control(service):
    control = service.control()
    if (
        control.get("active_run")
        or control.get("blocked")
        or control.get("restriction_streak", 0)
        or (control.get("not_before") and utc(control["not_before"]) > utc(service.wall()))
    ):
        raise ValueError("فاحص Roblox نشط أو لديه قيد مستقل؛ لا استمرار.")


def review(service, restriction, revision):
    with RobloxActivity(service.path), shared(service):
        with service.connect() as db:
            meta = service.meta(db)
            c = validate(service, db, meta)
            require_hold(service, c)
            if c["restriction"] != restriction or meta["revision"] != revision or c["stage"] == "Consumed":
                raise ValueError("مراجعة قديمة أو غير مطابقة.")
            c.update(stage="Reviewed", reviewed_revision=meta["revision"] + 1)
            c.pop("authorization_hash", None)
            c.pop("authorized_revision", None)
            service.audit(
                db,
                "cooldown_review",
                {
                    "restriction": restriction,
                    "revision": revision,
                    "reviewed_revision": c["reviewed_revision"],
                },
            )
            service.save(db, meta)
    return service.summary()


def wait_reviewed(service):
    with RobloxActivity(service.path), shared(service):
        with service.connect(read_only=True) as db:
            c = validate(service, db, service.meta(db))
        require_hold(service, c)
        if c["stage"] != "Reviewed":
            raise ValueError("راجع الدليل أولًا؛ الانتظار لا يفوض الإرسال.")
        while not service.cancel.is_set() and not ready(service, c):
            left = (utc(c["not_before"]) - utc(service.wall())).total_seconds()
            before = service.clock()
            if service.wait(min(0.25, left) if left > 0 else 0.25):
                break
            if service.clock() <= before:
                raise ValueError("Cooldown clock did not progress")
    return service.summary()


def authorization_review(service):
    with RobloxActivity(service.path), shared(service):
        with service.connect(read_only=True) as db:
            meta = service.meta(db)
            c = validate(service, db, meta)
            require_hold(service, c)
        check_control(service)
        if c["stage"] != "Reviewed" or not ready(service, c):
            raise ValueError("يلزم إقرار المراجعة واكتمال المهلة قبل تفويض الاستمرار.")
        return bulk.digest(bulk.encoded([owner(service), meta["revision"], c]))


def authorize(service, approval):
    # Revalidate under both locks; never accept a digest of an older revision.
    with RobloxActivity(service.path), shared(service):
        with service.connect() as db:
            meta = service.meta(db)
            c = validate(service, db, meta)
            require_hold(service, c)
            check_control(service)
            expected = bulk.digest(bulk.encoded([owner(service), meta["revision"], c]))
            if (
                c["stage"] != "Reviewed"
                or not ready(service, c)
                or not hmac.compare_digest(approval, expected)
            ):
                raise ValueError("تفويض قديم أو غير مطابق أو مهلة لم تكتمل.")
            token = uuid4().hex
            c.update(
                stage="Authorized",
                authorization_hash=bulk.digest(token.encode()),
                authorized_revision=meta["revision"] + 1,
            )
            service.audit(
                db,
                "cooldown_authorization",
                {
                    "restriction": c["restriction"],
                    "revision": meta["revision"],
                    "authorization_hash": c["authorization_hash"],
                },
            )
            service.save(db, meta)
    return token


def consume(service, db, meta, phase, token):
    c = validate(service, db, meta)
    require_hold(service, c)
    check_control(service)
    if (
        c["stage"] != "Authorized"
        or c["phase"] != phase
        or c["authorized_revision"] != meta["revision"]
        or not hmac.compare_digest(c["authorization_hash"], bulk.digest(token.encode()))
        or not ready(service, c)
    ):
        raise ValueError("تفويض الاستمرار غير صالح أو مستهلك؛ لا إرسال.")
    c["stage"] = "Consumed"
    meta["phases"][phase].update(state="Paused", stop_reason="explicit_cooldown_continuation")
    service.audit(
        db,
        "cooldown_authorization_consumed",
        {"restriction": c["restriction"], "authorization_hash": c["authorization_hash"]},
    )
    service.save(db, meta)
