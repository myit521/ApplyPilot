"""Model transport failures are classified without leaking provider bodies."""

import httpx
import pytest

from applypilot.deepseek_adapter import DeepSeekAdapter
from applypilot.model_adapter import ModelError, RetryableModelError

MAX_RESPONSE_CHARS = 32768
SECRET = "test-key"


def _ok(content="{}", finish_reason="stop"):
    return httpx.Response(
        200,
        json={
            "choices": [
                {"finish_reason": finish_reason, "message": {"content": content}}
            ]
        },
    )


def _install(monkeypatch, responder, calls):
    def respond(*args, **kwargs):
        calls.append(kwargs)
        return responder(*args, **kwargs)

    monkeypatch.setattr(httpx, "post", respond)


# --------------------------------------------------------------------------
# existing tests (retained)
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# request payload
# --------------------------------------------------------------------------


def test_request_payload_carries_bounded_settings(monkeypatch):
    calls = []
    _install(monkeypatch, lambda *a, **k: _ok("{\"a\": 1}"), calls)

    adapter = DeepSeekAdapter(api_key=SECRET, model="deepseek-chat", timeout=12.5)
    assert adapter.max_output_tokens == 2048
    assert adapter.max_input_chars == 64000
    assert adapter.complete("system prompt", "user prompt") == '{"a": 1}'

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["timeout"] == 12.5
    assert kwargs["headers"]["Authorization"] == f"Bearer {SECRET}"
    payload = kwargs["json"]
    assert payload["max_tokens"] == 2048
    assert payload["model"] == "deepseek-chat"
    assert payload["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt"},
    ]


def test_configured_max_tokens_is_sent(monkeypatch):
    calls = []
    _install(monkeypatch, lambda *a, **k: _ok("ok"), calls)

    DeepSeekAdapter(api_key=SECRET, max_output_tokens=512).complete("s", "u")
    assert calls[0]["json"]["max_tokens"] == 512


# --------------------------------------------------------------------------
# preflight: overlong input rejected before any HTTP call
# --------------------------------------------------------------------------


def test_overlong_input_rejected_without_http(monkeypatch):
    calls = []
    _install(monkeypatch, lambda *a, **k: _ok("{}"), calls)

    adapter = DeepSeekAdapter(api_key=SECRET, max_input_chars=100)
    with pytest.raises(ModelError) as raised:
        adapter.complete("s" * 60, "u" * 60)

    assert calls == []
    assert not isinstance(raised.value, RetryableModelError)
    assert "u" * 60 not in str(raised.value)


def test_input_at_limit_is_allowed(monkeypatch):
    calls = []
    _install(monkeypatch, lambda *a, **k: _ok("ok"), calls)

    assert DeepSeekAdapter(api_key=SECRET, max_input_chars=10).complete("12345", "12345") == "ok"
    assert len(calls) == 1


# --------------------------------------------------------------------------
# configuration validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1.5, "2048", None])
def test_invalid_max_output_tokens_rejected(bad):
    with pytest.raises(ModelError) as raised:
        DeepSeekAdapter(api_key=SECRET, max_output_tokens=bad)
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize("bad", [True, False, 0, -2048, 2.0])
def test_invalid_max_input_chars_rejected(bad):
    with pytest.raises(ModelError):
        DeepSeekAdapter(api_key=SECRET, max_input_chars=bad)


def test_positive_configuration_accepted():
    adapter = DeepSeekAdapter(api_key=SECRET, max_output_tokens=1, max_input_chars=1)
    assert (adapter.max_output_tokens, adapter.max_input_chars) == (1, 1)


# --------------------------------------------------------------------------
# finish_reason handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", None])
def test_non_stop_finish_reason_is_non_retryable(monkeypatch, finish_reason):
    payload = {"choices": [{"finish_reason": finish_reason, "message": {"content": "{}"}}]}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=payload))
    with pytest.raises(ModelError) as raised:
        DeepSeekAdapter(api_key=SECRET).complete("s", "u")
    assert not isinstance(raised.value, RetryableModelError)


def test_missing_finish_reason_is_non_retryable(monkeypatch):
    payload = {"choices": [{"message": {"content": "{}"}}]}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=payload))
    with pytest.raises(ModelError) as raised:
        DeepSeekAdapter(api_key=SECRET).complete("s", "u")
    assert not isinstance(raised.value, RetryableModelError)


# --------------------------------------------------------------------------
# blank / non-string / malformed / oversized response
# --------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["", "   \n\t", None, 123, {"a": 1}, ["x"]])
def test_blank_or_non_string_content_rejected(monkeypatch, content):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ok(content))
    with pytest.raises(ModelError) as raised:
        DeepSeekAdapter(api_key=SECRET).complete("s", "u")
    assert not isinstance(raised.value, RetryableModelError)


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"finish_reason": "stop", "message": {}}]},
        {"unexpected": "shape"},
        {},
    ],
)
def test_malformed_envelope_rejected(monkeypatch, body):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=body))
    with pytest.raises(ModelError) as raised:
        DeepSeekAdapter(api_key=SECRET).complete("s", "u")
    assert not isinstance(raised.value, RetryableModelError)


def test_non_json_body_rejected(monkeypatch):
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: httpx.Response(200, text="{not json")
    )
    with pytest.raises(ModelError):
        DeepSeekAdapter(api_key=SECRET).complete("s", "u")


def test_oversized_content_rejected(monkeypatch):
    content = ("leak-" + SECRET + " ") * 4000
    assert len(content) > MAX_RESPONSE_CHARS
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ok(content))
    with pytest.raises(ModelError) as raised:
        DeepSeekAdapter(api_key=SECRET).complete("s", "u")
    assert SECRET not in str(raised.value)
    assert not isinstance(raised.value, RetryableModelError)


# --------------------------------------------------------------------------
# safe exception redaction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="sk-secret-provider-body"),
        httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "sk-secret"}}]}),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": None}}]}),
        httpx.Response(500, text="sk-secret-provider-body"),
        httpx.Response(401, text="sk-secret-provider-body"),
    ],
)
def test_errors_never_leak_body_or_key(monkeypatch, response):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response)
    with pytest.raises(ModelError) as raised:
        DeepSeekAdapter(api_key=SECRET).complete("s", "u")
    message = str(raised.value)
    assert "sk-secret-provider-body" not in message
    assert "sk-secret" not in message
    assert SECRET not in message
