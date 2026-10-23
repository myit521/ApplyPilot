"""Request observations must not turn unknown provider usage into zero cost."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import uuid

import httpx
import pytest

from applypilot.deepseek_adapter import DeepSeekAdapter
from applypilot.model_adapter import ModelError, RetryableModelError


def events(caplog):
    return [json.loads(record.message) for record in caplog.records
            if record.name == "applypilot.model_usage"]


def response(usage=None, **choice):
    payload = {"choices": [{"finish_reason": "stop", "message": {"content": "synthetic"},
                            **choice}]}
    if usage is not None:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


@pytest.mark.parametrize("usage,expected", [
    ({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}, "reported"),
    ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "reported"),
    (None, "missing"),
    ({}, "invalid"),
    ({"prompt_tokens": True, "completion_tokens": 4, "total_tokens": 5}, "invalid"),
    ({"prompt_tokens": -1, "completion_tokens": 4, "total_tokens": 3}, "invalid"),
    ({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 15}, "invalid"),
    ({"prompt_tokens": "10", "completion_tokens": 4, "total_tokens": 14}, "invalid"),
    ([], "invalid"),
])
def test_success_usage_is_reported_or_explicitly_unknown(monkeypatch, caplog, usage, expected):
    caplog.set_level(logging.INFO, logger="applypilot.model_usage")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response(usage))
    assert DeepSeekAdapter(api_key="secret-key").complete("private-system", "private-user") == "synthetic"
    records = events(caplog)
    assert [e["event"] for e in records] == ["model_call_started", "model_call_finished"]
    start, end = records
    assert start["call_id"] == end["call_id"]
    uuid.UUID(end["call_id"])
    assert end["outcome"] == "success"
    assert end["usage_status"] == expected
    assert end["duration_ms"] >= 0
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        assert end[field] == (usage[field] if expected == "reported" else None)
    assert "private-system" not in caplog.text
    assert "private-user" not in caplog.text
    assert "secret-key" not in caplog.text
    assert "synthetic" not in caplog.text


@pytest.mark.parametrize("failure,error_type,outcome", [
    (httpx.TimeoutException("private-timeout"), RetryableModelError, "retryable_error"),
    (httpx.Response(503, text="private-response"), RetryableModelError, "retryable_error"),
    (httpx.Response(401, text="private-response"), ModelError, "error"),
    (httpx.Response(200, text="private-invalid-json"), ModelError, "error"),
])
def test_failed_call_keeps_unknown_usage_and_redacts_details(
    monkeypatch, caplog, failure, error_type, outcome,
):
    caplog.set_level(logging.INFO, logger="applypilot.model_usage")
    def post(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(error_type):
        DeepSeekAdapter(api_key="secret-key").complete("private-system", "private-user")
    start, end = events(caplog)
    assert start["call_id"] == end["call_id"]
    assert end["outcome"] == outcome
    assert end["usage_status"] == "unavailable"
    assert end["total_tokens"] is None
    assert "private-" not in caplog.text
    assert "secret-key" not in caplog.text


def test_truncated_response_can_still_have_provider_usage(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="applypilot.model_usage")
    usage = {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response(usage, finish_reason="length"))
    with pytest.raises(ModelError):
        DeepSeekAdapter(api_key="test").complete("s", "u")
    end = events(caplog)[-1]
    assert end["outcome"] == "error"
    assert end["usage_status"] == "reported"
    assert end["total_tokens"] == 8


def test_preflight_rejection_is_not_an_http_attempt(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="applypilot.model_usage")
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(k))
    with pytest.raises(ModelError):
        DeepSeekAdapter(api_key="test", max_input_chars=1).complete("s", "u")
    records = events(caplog)
    assert len(records) == 1
    assert records[0]["event"] == "model_call_finished"
    assert records[0]["outcome"] == "input_rejected"
    assert records[0]["request_started"] is False
    assert records[0]["usage_status"] == "unavailable"
    assert calls == []


def test_scopes_reset_and_parallel_calls_do_not_share_task_tags(monkeypatch, caplog):
    from applypilot.model_usage import model_call_scope

    caplog.set_level(logging.INFO, logger="applypilot.model_usage")
    barrier = Barrier(2)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response())
    adapter = DeepSeekAdapter(api_key="test")
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    def call(index):
        with model_call_scope(run_id=ids[index], job_id=index + 1, attempt_count=index + 1):
            barrier.wait(timeout=5)
            with model_call_scope(step="generate_resume"):
                adapter.complete("s", "u")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(call, [0, 1]))
    adapter.complete("s", "u")
    finished = [e for e in events(caplog) if e["event"] == "model_call_finished"]
    assert len({e["call_id"] for e in finished}) == 3
    for index, run_id in enumerate(ids):
        item = next(e for e in finished if e["run_id"] == run_id)
        assert item["job_id"] == index + 1
        assert item["attempt_count"] == index + 1
        assert item["step"] == "generate_resume"
    assert finished[-1]["run_id"] is None
    assert finished[-1]["step"] is None


def test_log_handler_failure_does_not_change_model_result(monkeypatch):
    from applypilot import model_usage

    def broken(*args, **kwargs):
        raise OSError("synthetic log failure")
    monkeypatch.setattr(model_usage.log, "info", broken)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response())
    assert DeepSeekAdapter(api_key="test").complete("s", "u") == "synthetic"
