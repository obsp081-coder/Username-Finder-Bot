from username_finder import windows_awake
from username_finder.windows_awake import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, AwakeRequest


def test_request_flags_are_minimal_idempotent_and_released_on_cleanup():
    calls = []
    request = AwakeRequest(api=lambda flags: calls.append(flags) or 1)
    request.update(False)
    assert not calls
    request.update(True)
    request.update(True)
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]
    request.close()
    request.close()
    request.update(True)
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS]
    assert not request.active


def test_native_failure_is_visible_and_never_claims_success():
    request = AwakeRequest(api=lambda _: 0)
    request.update(True)
    assert request.error and not request.active


def test_unsupported_environment_is_safe(monkeypatch):
    monkeypatch.setattr(windows_awake, "windows_api", lambda: None)
    request = AwakeRequest()
    request.update(True)
    request.close()
    assert not request.active and request.error


def test_release_failure_retains_uncertainty_and_cleanup_can_retry():
    outcomes = iter([1, 0, 1])
    request = AwakeRequest(api=lambda _: next(outcomes))
    request.update(True)
    request.update(False)
    assert request.active and request.error
    request.close()
    assert not request.active and not request.error
