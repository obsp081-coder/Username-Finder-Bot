import copy
import math

import pytest

from username_finder.roblox_prefilter_estimates import calculate, duration_text


def snapshot(valid=45, pilot=21, batch=20, delay=10):
    return {
        "manifest": {"settings": {"delay": delay, "batch_size": batch, "pilot_names": pilot}},
        "counts": {"valid": valid},
        "phases": {},
        "import_state": "Ready",
        "timing_policy_current": True,
    }


def test_phase_partitioning_partial_batches_and_no_final_delay():
    data = snapshot()
    result = calculate(1, 1, 1, data)
    assert result["config"] == data["manifest"]["settings"]
    assert result["planned_requests"] == result["remaining_requests"] == 4
    assert result["pilot_requests"] == 2
    assert result["pilot_seconds"] == 11
    assert result["remaining_seconds"] == 32
    assert result["names_per_minute"] == pytest.approx(20 * 60 / 10.5)
    data["phases"] = {"pilot": {"processed": 21}, "full": {"processed": 4}}
    result = calculate(1, 1, 1, data)
    assert result["pilot_requests"] == result["pilot_seconds"] == 0
    assert result["remaining_requests"] == 1 and result["remaining_seconds"] == 0.5
    data["phases"]["full"]["processed"] = 24
    data["pacing_remaining_seconds"] = 10
    result = calculate(1, 1, 1, data)
    assert result["remaining_requests"] == result["remaining_seconds"] == 0


def test_checkpoint_first_wait_and_finite_large_count_model():
    data = snapshot(valid=1_700_000, pilot=100, delay=0.001)
    result = calculate(1, 1, 1, data)
    assert result["remaining_requests"] == 85_000
    assert result["remaining_seconds"] == pytest.approx(42584.999)
    data["pacing_remaining_seconds"] = 0.0007
    result = calculate(1, 1, 1, data)
    assert result["remaining_seconds"] == pytest.approx(42584.9997)
    huge = calculate(1, 1, 1, snapshot(valid=10**15, delay=0.001))
    assert math.isfinite(huge["remaining_seconds"])


def test_unknown_count_partial_import_and_empty_input():
    result = calculate(10, 20, 100)
    assert result["latency_source"] == "illustrative_assumption"
    assert result["remaining_requests"] is result["remaining_seconds"] is None
    assert result["pilot_requests"] == 5 and result["pilot_seconds"] == 42.5
    data = snapshot(valid=0)
    result = calculate(10, 20, 100, data)
    assert result["remaining_requests"] == result["remaining_seconds"] == 0
    data["import_state"] = "ImportPaused"
    result = calculate(10, 20, 100, data)
    assert not result["scope_complete"] and result["remaining_requests"] is None


def test_measured_latency_excludes_failed_phase_and_observed_timing_is_explicit():
    data = snapshot()
    data["phases"] = {
        "pilot": {
            "processed": 21,
            "elapsed_seconds": 11,
            "responses": 2,
            "safe_responses": 2,
            "latency_sum": 1,
        },
        "full": {
            "processed": 20,
            "elapsed_seconds": 2,
            "responses": 1,
            "safe_responses": 0,
            "latency_sum": 0.01,
            "state": "Restricted",
        },
    }
    before = copy.deepcopy(data)
    result = calculate(1, 1, 1, data)
    assert result["latency_source"] == "measured_safe_responses"
    assert result["latency_samples"] == 2 and result["latency_seconds"] == 0.5
    assert result["observed_names_per_minute"] == pytest.approx(41 * 60 / 13)
    assert result["restricted"]
    assert data == before
    data["phases"]["full"]["elapsed_incomplete"] = True
    assert calculate(1, 1, 1, data)["observed_names_per_minute"] is None
    data["phases"]["full"]["elapsed_incomplete"] = False
    data["timing_policy_current"] = False
    assert calculate(1, 1, 1, data)["observed_names_per_minute"] is None


def test_shared_provider_restriction_and_uncertain_review_remain_distinct():
    assert calculate(1, 20, 100, provider_blocked=True)["restricted"]
    data = snapshot()
    data["provider_blocked"] = True
    assert calculate(1, 1, 1, data)["restricted"]
    data["provider_blocked"] = False
    data["phases"] = {"pilot": {"processed": 2, "state": "ReviewRequired"}}
    result = calculate(1, 1, 1, data)
    assert result["review_required"] and not result["restricted"]


@pytest.mark.parametrize("delay", [0.001, 0.5, 1, 10, 86400])
def test_pacing_and_latency_both_determine_the_rate(delay):
    result = calculate(delay, 20, 100)
    assert result["names_per_minute"] == pytest.approx(1200 / (delay + 0.5))
    assert result["names_per_hour"] == pytest.approx(72000 / (delay + 0.5))


@pytest.mark.parametrize(
    "delay,batch,pilot",
    [(0, 20, 100), (-1, 20, 100), (float("nan"), 20, 100), (1, 1001, 100), (1, True, 100), (1, 20, 0)],
)
def test_invalid_inputs_never_produce_an_estimate(delay, batch, pilot):
    with pytest.raises(ValueError):
        calculate(delay, batch, pilot)


@pytest.mark.parametrize("value", [-1, "10", True])
def test_invalid_cached_counts(value):
    with pytest.raises(ValueError):
        calculate(1, 20, 100, snapshot(valid=value))


def test_duration_labels_remain_readable_at_every_scale():
    assert "غير معلوم" in duration_text(None)
    assert "ثانية" in duration_text(0.001)
    assert "دقيقة" in duration_text(60)
    assert "ساعة" in duration_text(3600)
    assert "يوم" in duration_text(86400)
