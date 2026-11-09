"""Constant-space sequential Bulk estimates. Never reads files, SQL, or the network."""

import math

from .roblox_prefilter import VERIFIED_OPERATIONAL_BATCH, settings

ILLUSTRATIVE_LATENCY_SECONDS = 0.5


def _nonnegative(value):
    if type(value) is not int or value < 0:
        raise ValueError("Invalid cached name count")
    return value


def calculate(delay, batch_size, pilot_names, snapshot=None, *, provider_blocked=False):
    """Estimate from a cached checkpoint; saved settings always win over UI drafts.

    Durations include request latency, phase-partitioned partial batches, post-response
    gaps, and the checkpoint's remaining first wait. Import/export, local processing,
    future throttling and manual phase review time are excluded, so these are models.
    """
    config = (
        settings(**snapshot["manifest"]["settings"]) if snapshot else settings(delay, batch_size, pilot_names)
    )
    batch, gap = config["batch_size"], config["delay"]
    phases = snapshot.get("phases", {}) if snapshot else {}
    total_latency, responses = 0.0, 0
    for phase in phases.values():
        count, safe = phase.get("responses", 0), phase.get("safe_responses", 0)
        latency = phase.get("latency_sum", 0)
        if count and count == safe and math.isfinite(latency) and latency > 0:
            responses += count
            total_latency += latency
    latency = total_latency / responses if responses else ILLUSTRATIVE_LATENCY_SECONDS
    rate = batch * 60 / (latency + gap)
    valid = _nonnegative(snapshot["counts"]["valid"]) if snapshot else None
    if snapshot and snapshot.get("import_state") != "Ready":
        valid = None  # A partial import does not establish the full-file request budget or ETA.
    pilot = min(valid, config["pilot_names"]) if valid is not None else config["pilot_names"]
    pilot_left = max(0, pilot - _nonnegative(phases.get("pilot", {}).get("processed", 0)))
    full_left = (
        max(0, valid - pilot - _nonnegative(phases.get("full", {}).get("processed", 0)))
        if valid is not None
        else None
    )

    def requests(names):
        return (names + batch - 1) // batch

    first_wait = snapshot.get("pacing_remaining_seconds", 0) if snapshot else 0
    if not isinstance(first_wait, (int, float)) or not math.isfinite(first_wait) or first_wait < 0:
        raise ValueError("Invalid cached pacing wait")

    def duration(count):
        return count * latency + max(0, count - 1) * gap + (first_wait if count else 0)

    pilot_requests = requests(pilot_left)
    remaining_requests = pilot_requests + requests(full_left) if full_left is not None else None
    active = [p for p in phases.values() if p.get("processed", 0)]
    observed = None
    if snapshot and snapshot.get("timing_policy_current") and active:
        elapsed = sum(p.get("elapsed_seconds", 0) for p in active)
        if (
            all(not p.get("elapsed_incomplete") and p.get("elapsed_seconds", 0) > 0 for p in active)
            and math.isfinite(elapsed)
            and elapsed > 0
        ):
            observed = sum(p["processed"] for p in active) * 60 / elapsed
    restricted = (
        provider_blocked
        or bool(snapshot and snapshot.get("provider_blocked"))
        or any(p.get("state") == "Restricted" for p in phases.values())
    )
    return {
        "config": config,
        "latency_seconds": latency,
        "latency_source": "measured_safe_responses" if responses else "illustrative_assumption",
        "latency_samples": responses,
        "names_per_minute": rate,
        "names_per_second": rate / 60,
        "names_per_hour": rate * 60,
        "pilot_requests": pilot_requests,
        "pilot_seconds": duration(pilot_requests),
        "remaining_requests": remaining_requests,
        "planned_requests": requests(pilot) + requests(valid - pilot) if valid is not None else None,
        "remaining_seconds": duration(remaining_requests) if remaining_requests is not None else None,
        "observed_names_per_minute": observed,
        "restricted": restricted,
        "review_required": any(p.get("state") == "ReviewRequired" for p in phases.values()),
        "scope_complete": bool(snapshot and snapshot.get("import_state") == "Ready"),
        "first_wait_seconds": first_wait,
        "batch_supported": batch <= VERIFIED_OPERATIONAL_BATCH,
    }


def duration_text(seconds):
    if seconds is None:
        return "غير معلوم قبل الاستيراد"
    if seconds < 60:
        return f"{seconds:.2f} ثانية"
    if seconds < 3600:
        return f"{seconds / 60:,.2f} دقيقة"
    if seconds < 86400:
        return f"{seconds / 3600:,.2f} ساعة"
    return f"{seconds / 86400:,.2f} يوم"


def capacity_plan(
    names,
    batch_size=20,
    delay=10,
    *,
    latency=0.5,
    local_seconds_per_request=0,
    hours=24,
    pilot_names=100,
    safe_outcome_fraction=1,
):
    """Offline feasibility arithmetic, never permission or evidence of provider capacity.

    Uses the existing phase-partitioned request budget. Local processing is separate
    from HTTP latency; no final delay, retries, outage guesses or registration claims.
    Fractional safe outcomes leave unresolved names without scheduling rechecks.
    """
    names = _nonnegative(names)
    if not names or names > 2**63 - 1:
        raise ValueError("Planning count must be a positive signed 64-bit integer")
    for value, positive in ((latency, False), (local_seconds_per_request, False), (hours, True)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            or (positive and value == 0)
        ):
            raise ValueError("Invalid timing assumption")
    if (
        isinstance(safe_outcome_fraction, bool)
        or not isinstance(safe_outcome_fraction, (int, float))
        or not math.isfinite(safe_outcome_fraction)
        or not 0 <= safe_outcome_fraction <= 1
    ):
        raise ValueError("Safe-outcome fraction must be between zero and one")
    config = settings(delay, batch_size, pilot_names)
    snapshot = {
        "manifest": {"settings": config},
        "counts": {"valid": names},
        "phases": {},
        "import_state": "Ready",
    }
    budget = calculate(delay, batch_size, pilot_names, snapshot)
    count = budget["planned_requests"]
    target = hours * 3600
    elapsed = count * (latency + local_seconds_per_request) + max(0, count - 1) * delay
    if not all(math.isfinite(v) for v in (target, elapsed)) or target <= 0 or elapsed <= 0:
        raise ValueError("Timing assumptions exceed the finite planning range")
    one_phase = (names + batch_size - 1) // batch_size
    max_gap = (target - count * (latency + local_seconds_per_request)) / (count - 1) if count > 1 else None
    safe_names = min(names, int(names * safe_outcome_fraction))
    rates = (names / target, count / target, names / elapsed, safe_names / elapsed, target / count)
    if not all(math.isfinite(rate) for rate in rates):
        raise ValueError("Derived rates exceed the finite planning range")
    return {
        "names": names,
        "settings": config,
        "unpartitioned_requests": one_phase,
        "planned_requests": count,
        "phase_boundary_extra_requests": count - one_phase,
        "required_names_per_second": rates[0],
        "required_requests_per_second": rates[1],
        "average_cycle_budget_seconds": rates[4],
        "latency_assumption_seconds": latency,
        "local_processing_assumption_seconds": local_seconds_per_request,
        "maximum_post_response_delay_seconds": max_gap,
        "modeled_elapsed_seconds": elapsed,
        "modeled_names_per_second": rates[2],
        "modeled_safe_names": safe_names,
        "modeled_unresolved_names": names - safe_names,
        "modeled_safe_names_per_second": rates[3],
        "time_budget_met_if_uninterrupted": elapsed <= target,
        "all_names_have_safe_outcomes_in_model": safe_outcome_fraction == 1,
        "within_operational_batch_guard": budget["batch_supported"],
        "provider_capacity_verified": False,
        "dispatch_authorized": False,
        "automatic_retries": 0,
        "assumptions": "Uninterrupted sequential model; excludes manual review, import/export and outages. "
        "A restriction stops the actual runner. Safe outcomes are not registration availability.",
    }
