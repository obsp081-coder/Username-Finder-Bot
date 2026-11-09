"""Offline cooldown advice. No network, dispatch authorization or restriction-file operations."""

import math
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

FALLBACK_SECONDS = 300
MAX_WAIT_SECONDS = 86400
MAX_PROPOSED_INTERVAL = 300


def utc(value):
    date = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(date, datetime) or date.tzinfo is None or date.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return date.astimezone(UTC)


def retry_after(value, received_at):
    """Anchor even fallback advice to receipt, never to a later review/restart time."""
    at = utc(received_at)
    kind = "fallback_missing" if value is None else "fallback_malformed"
    try:
        if not isinstance(value, str) or not value.strip():
            raise ValueError
        text = value.strip()
        if len(text) > 128:
            return {"not_before": None, "source": "excessive_review_required", "wait_supported": False}
        if text.isascii() and text.isdigit():
            if len(text) > 12:
                raise OverflowError
            deadline = at + timedelta(seconds=int(text))
            kind = "retry_after_seconds"
        else:
            deadline = max(at, utc(parsedate_to_datetime(text)))
            kind = "retry_after_http_date"
    except OverflowError:
        return {"not_before": None, "source": "excessive_review_required", "wait_supported": False}
    except (TypeError, ValueError):
        deadline = at + timedelta(seconds=FALLBACK_SECONDS)
    return {
        "not_before": deadline.isoformat(),
        "source": kind,
        "wait_supported": (deadline - at).total_seconds() <= MAX_WAIT_SECONDS,
    }


def advisory(
    evidence,
    *,
    now,
    interval=10,
    streak=1,
    previous_not_before=None,
    review_recorded=False,
    blockers=(),
    pending=False,
):
    """Describe a review state, never a runnable engine phase. Expiry cannot authorize I/O."""
    now = utc(now)
    result = {
        "version": 1,
        "state": "ReadyForSeparateAuthorization",
        "not_before": None,
        "source": "no_429_evidence",
        "review_recorded": bool(review_recorded),
        "dispatch_authorized": False,
        "restriction_cleared": False,
        "pacing_applied": False,
        "wait_supported": False,
        "proposed_interval_seconds": None,
        "consecutive_429": 0,
    }
    if blockers:
        result["state"] = "RestrictedPendingReview"
    if pending:
        result["state"] = "UnfinishedDeliveryReview"
        return result
    if set(blockers) - {
        "shared_provider_restriction_preserved",
        "recorded_restriction_requires_provider_permission_evidence",
    }:
        result["state"] = "UnsafeEvidenceReview"
        return result
    if not evidence or evidence.get("status") != 429:
        return result
    result.update(request_id=evidence["request_id"], body_sha256=evidence["body_sha256"])
    if (
        not evidence.get("body_integrity_verified")
        or not evidence.get("response_complete")
        or evidence.get("transport_error")
        or evidence.get("challenge")
    ):
        result["state"] = "UnsafeEvidenceReview"
        return result
    try:
        if (
            type(streak) is not int
            or streak < 1
            or isinstance(interval, bool)
            or not math.isfinite(interval)
            or interval <= 0
        ):
            raise ValueError("Invalid pacing advice inputs")
        parsed = retry_after(evidence.get("headers", {}).get("retry-after"), evidence["response_at"])
        result.update(parsed)
        if previous_not_before:
            prior = utc(previous_not_before)
            if result["not_before"]:
                result["not_before"] = max(utc(result["not_before"]), prior).isoformat()
            else:
                result["not_before"] = prior.isoformat()
        result["consecutive_429"] = streak
        result["proposed_interval_seconds"] = max(
            interval, min(MAX_PROPOSED_INTERVAL, max(10, interval) * 2 ** min(streak, 5))
        )
        remaining = (
            max(0, (utc(result["not_before"]) - now).total_seconds()) if result["not_before"] else None
        )
        result["remaining_seconds"] = remaining
        result["wait_supported"] &= remaining is not None and remaining <= MAX_WAIT_SECONDS
        if streak >= 2:
            result.update(state="RepeatedRateLimitReview", wait_supported=False)
        elif not result["wait_supported"]:
            result["state"] = "ExcessiveWaitReview"
        elif not parsed["source"].startswith("retry_after_"):
            result["state"] = "FallbackReviewRequired"
        elif remaining:
            result["state"] = "CooldownReviewed" if review_recorded else "CooldownPendingReview"
        else:
            result["state"] = (
                "AwaitingProviderAuthorization" if review_recorded else "RestrictedPendingReview"
            )
    except (KeyError, TypeError, ValueError, OverflowError):
        result.update(state="UnsafeEvidenceReview", wait_supported=False)
    return result


def wait_offline(plan, *, wall, clock, wait):
    """Cancellation-aware test/review timer only. Never called by a production request runner.

    `wait(seconds)` returns True on cancellation. Neither completion nor an explicit
    review authorizes network I/O; clock discontinuities stop for manual review.
    """
    if (
        not plan.get("review_recorded")
        or not plan.get("wait_supported")
        or plan.get("dispatch_authorized")
        or plan.get("consecutive_429", 0) >= 2
        or plan.get("state")
        not in {"CooldownReviewed", "FallbackReviewRequired", "AwaitingProviderAuthorization"}
    ):
        return "manual_review_required"
    remaining = max(0, (utc(plan["not_before"]) - utc(wall())).total_seconds())
    if remaining > MAX_WAIT_SECONDS:
        return "manual_review_required"
    started = clock()
    end = started + remaining
    while True:
        tick = clock()
        wall_left = (utc(plan["not_before"]) - utc(wall())).total_seconds()
        if not math.isfinite(tick) or tick < started or wall_left > end - tick + 1:
            return "manual_review_required"
        left = max(end - tick, wall_left, 0)
        if left <= 0:
            return "elapsed_review_required"
        if wait(min(left, 0.25)):
            return "cancelled"
        if clock() <= tick:
            return "manual_review_required"
