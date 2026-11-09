"""Bounded, read-only evidence review. This module never authorizes dispatch or unlocks."""

import base64
import json

from . import roblox_bulk_protocol as bulk
from .roblox_activity import RobloxActivity
from .roblox_cooldown import advisory, utc
from .roblox_prefilter import VERIFIED_OPERATIONAL_BATCH, policy


def inspect_operation(service):
    snapshot = service.summary(read_only=True)
    manifest = snapshot["manifest"]
    marker = RobloxActivity(service.database).block
    reason = ""
    if marker.exists():
        with marker.open("rb") as stream:
            reason = stream.read(256).decode("utf-8", errors="replace")
    last = snapshot.get("last_request")
    evidence_report = None
    with service.connect(read_only=True) as db:
        pending = [
            dict(row)
            for row in db.execute(
                "SELECT id,state,stop FROM requests WHERE state IN ('Intent','Response','Uncertain') "
                "ORDER BY id DESC LIMIT 2"
            )
        ]
        reviews = [
            dict(row)
            for row in db.execute(
                "SELECT id,event,at,data FROM audit WHERE event='restriction_review' ORDER BY id DESC LIMIT 5"
            )
        ]
        row = db.execute("SELECT * FROM requests WHERE id=?", (last["id"],)).fetchone() if last else None
        if row and row["evidence"]:
            evidence = json.loads(row["evidence"])
            body = base64.b64decode(evidence["body_base64"], validate=True)
            intact = (
                not evidence["body_omitted_for_sensitive_fields"]
                and bulk.digest(body) == evidence["body_sha256"]
            )
            evidence_report = {
                "request_id": row["id"],
                "reason": row["stop"],
                "status": evidence["status"],
                "request_at": row["request_at"],
                "response_at": row["response_at"],
                "affected_names": len(json.loads(row["names"])),
                "body_sha256": evidence["body_sha256"],
                "body_integrity_verified": intact,
                "response_complete": evidence.get("complete") is True,
                "transport_error": evidence.get("error", "legacy_transport_state_unverified"),
                "challenge": evidence.get("challenge", True),
                "headers": {
                    k: str(v)[:512] for k, v in evidence["headers"].items() if k in bulk.SAFE_HEADERS
                },
            }
            # Parse the complete Retry-After value, not the bounded display preview.
            full_headers = evidence["headers"]
        else:
            full_headers = {}
        streak = 0
        for previous in db.execute("SELECT stop FROM requests ORDER BY id DESC LIMIT 2"):
            if previous["stop"] != "http_429":
                break
            streak += 1
    blockers = []
    if marker.exists():
        blockers.append("shared_provider_restriction_preserved")
    if any(p["state"] == "Restricted" for p in snapshot["phases"].values()):
        blockers.append("recorded_restriction_requires_provider_permission_evidence")
    if manifest["policy"] != policy():
        blockers.append("source_policy_mismatch_requires_technical_compatibility_review")
    if pending:
        blockers.append("unfinished_or_uncertain_requests_never_replay")
    if evidence_report and not evidence_report["body_integrity_verified"]:
        blockers.append("raw_evidence_integrity_unverified")
    if manifest["settings"]["batch_size"] > VERIFIED_OPERATIONAL_BATCH:
        blockers.append("unverified_batch_size_dispatch_disabled")
    previous_plan = {}
    previous_deadlines = []
    for review in reviews:
        data = json.loads(review["data"])
        candidate = data.get("rate_limit_advisory", {})
        if candidate.get("version") == 1 and candidate.get("not_before"):
            try:
                previous_deadlines.append(utc(candidate["not_before"]))
            except (TypeError, ValueError):
                pass  # Invalid audit hints never authorize sending or alter a provider restriction.
        if (
            not previous_plan
            and evidence_report
            and candidate.get("version") == 1
            and candidate.get("request_id") == evidence_report["request_id"]
            and candidate.get("body_sha256") == evidence_report["body_sha256"]
        ):
            previous_plan = candidate
    rate = advisory(
        dict(evidence_report, headers=full_headers) if evidence_report else None,
        now=service.wall(),
        interval=manifest["settings"]["delay"],
        streak=max(1, streak),
        previous_not_before=max(previous_deadlines).isoformat() if previous_deadlines else None,
        review_recorded=previous_plan.get("review_recorded", False),
        blockers=blockers,
        pending=bool(pending),
    )
    report = {
        "version": 1,
        "manifest_fingerprint": manifest["fingerprint"],
        "saved_policy": manifest["policy"],
        "current_policy": policy(),
        "policy_matches": manifest["policy"] == policy(),
        "shared_restriction_reason": reason,
        "last_response_evidence": evidence_report,
        "pending_requests": pending,
        "recent_reviews": reviews,
        "resume_blockers": blockers,
        "dispatch_authorized": False,
        "automatic_unlock_supported": False,
        "recovery_decision": "Blocked" if blockers else "Separate explicit phase authorization required",
        "explanation_ar": (
            "مراجعة محلية فقط. انتهاء Retry-After أو إعادة ضبط الحصة لا يثبت إذن الموفر بالاستئناف. "
            "لا توجد هنا أدلة مستقلة ترفع قيد الموفر، ولا زر تحرير أو طلب تحقق حي. "
            "تبقى النتائج والأدلة قابلة للعرض والتصدير؛ أي تحقق شبكي لاحق يحتاج تفويضًا منفصلًا."
        ),
        "rate_limit_advisory": rate,
    }
    if snapshot.get("cooldown", {}).get("stage") in ("PendingReview", "Reviewed", "Authorized"):
        report["operational_cooldown"] = snapshot["cooldown"]
        report["recovery_decision"] = "Explicit cooldown review and separate one-time authorization required"
        report["explanation_ar"] = (
            "مهلة429 مستقبلية: راجع الدليل وانتظر الموعد المحفوظ، ثم فوّض الاستمرار منفصلًا. "
            "لا إرسال من هذا التقرير أو من انتهاء المهلة؛ دفعة429 تبقى Unknown ولا تعاد. "
            "أي قيد تاريخي أو مستقل مقدم دائمًا على هذا المسار."
        )
    return dict(snapshot, restriction_review=report)


def record_review(service, fingerprint):
    """Explicit acknowledgement only; append audit without changing results or restrictions."""
    snapshot = inspect_operation(service)
    if fingerprint != snapshot["manifest"]["fingerprint"]:
        raise ValueError("Explicit matching manifest review required")
    with RobloxActivity(service.path):
        with service.connect() as db:
            service.audit(
                db,
                "restriction_review",
                {
                    "manifest_fingerprint": fingerprint,
                    "decision": "Evidence reviewed; no unlock, network authorization or reclassification",
                    "resume_blockers": snapshot["restriction_review"]["resume_blockers"],
                    "last_response_evidence": snapshot["restriction_review"]["last_response_evidence"],
                    "rate_limit_advisory": dict(
                        snapshot["restriction_review"]["rate_limit_advisory"], review_recorded=True
                    ),
                },
            )
            service.save(db, service.meta(db))
    return inspect_operation(service)
