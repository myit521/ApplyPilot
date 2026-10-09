"""Model transport failures are classified without leaking provider bodies."""

import httpx
import pytest

from applypilot.deepseek_adapter import DeepSeekAdapter
from applypilot.model_adapter import ModelError, RetryableModelError


@pytest.mark.parametrize("status, retryable", [(429, True), (503, True), (401, False)])
def test_http_status_classification(monkeypatch, status, retryable):
    def respond(*args, **kwargs):
        return httpx.Response(status, text="private provider response")

    monkeypatch.setattr(httpx, "post", respond)
    error_type = RetryableModelError if retryable else ModelError
    with pytest.raises(error_type) as raised:
        DeepSeekAdapter(api_key="test-key").complete("system", "user")
    assert "private provider response" not in str(raised.value)
    if not retryable:
        assert not isinstance(raised.value, RetryableModelError)


def test_timeout_is_retryable_and_redacted(monkeypatch):
    def timeout(*args, **kwargs):
        raise httpx.TimeoutException("private request details")

    monkeypatch.setattr(httpx, "post", timeout)
    with pytest.raises(RetryableModelError) as raised:
        DeepSeekAdapter(api_key="test-key").complete("system", "user")
    assert "private request details" not in str(raised.value)
