"""The live probe must remain bounded and keep credentials out of reports."""

from scripts.evaluate_t14_live_probe import request_once


def test_request_once_caps_output_and_reports_usage_without_key(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {
                "model": "deepseek-chat",
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            }

    def fake_post(url, *, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        return Response()

    monkeypatch.setattr("scripts.evaluate_t14_live_probe.httpx.post", fake_post)
    result = request_once("secret-test-key", "system", "user", max_tokens=300)

    assert len(calls) == 1
    assert calls[0][2]["max_tokens"] == 300
    assert calls[0][3] == 60.0
    assert result["content"] == "{}"
    assert result["usage"]["total_tokens"] == 15
    assert "secret-test-key" not in str(result)


def test_request_once_does_not_retry_and_does_not_copy_error_body(monkeypatch):
    calls = []

    class Response:
        status_code = 429
        text = "secret-test-key"

    def fake_post(*args, **kwargs):
        calls.append(1)
        return Response()

    monkeypatch.setattr("scripts.evaluate_t14_live_probe.httpx.post", fake_post)
    result = request_once("secret-test-key", "system", "user", max_tokens=300)

    assert len(calls) == 1
    assert result == {"http_status": 429, "error": "http_error"}


def test_request_once_rejects_truncated_or_oversized_success(monkeypatch):
    class Response:
        status_code = 200

        def __init__(self, finish_reason, content):
            self.finish_reason = finish_reason
            self.content = content

        def json(self):
            return {"model": "deepseek-chat", "usage": {}, "choices": [{
                "finish_reason": self.finish_reason, "message": {"content": self.content},
            }]}

    responses = iter([Response("length", "{}"), Response("stop", "x" * 8193)])
    monkeypatch.setattr("scripts.evaluate_t14_live_probe.httpx.post",
                        lambda *args, **kwargs: next(responses))

    truncated = request_once("key", "system", "user", max_tokens=300)
    oversized = request_once("key", "system", "user", max_tokens=300)

    assert truncated == {"http_status": 200, "finish_reason": "length",
                         "error": "incomplete_completion"}
    assert oversized == {"http_status": 200, "finish_reason": "stop",
                         "error": "oversized_completion"}
