"""Actual graph execution must retain worker identity across node threads/retries."""

import json
import logging
import uuid

import httpx
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from applypilot.deepseek_adapter import DeepSeekAdapter
from applypilot.schemas import Fact, FactType
from applypilot.task_worker import TaskWorker
from applypilot.workflow import WorkflowStatus, build_graph


def test_worker_retry_and_review_calls_keep_distinct_request_identity(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="applypilot.model_usage")
    run_id = str(uuid.uuid4())
    fact = Fact(id="t31_synthetic", status="confirmed", fact_type=FactType.PROJECT,
                source_name="合成项目", content="参与 Java 开发", skills=["Java"])
    generation_calls = []
    def post(*args, **kwargs):
        system = kwargs["json"]["messages"][0]["content"]
        if "职位描述解析器" in system:
            content = {"required": ["Java"], "keywords": [
                {"term": "Java", "importance": "required"}]}
        elif "事实一致性复核员" in system:
            content = {"violations": []}
        else:
            generation_calls.append(1)
            if len(generation_calls) == 1:
                raise httpx.TimeoutException("synthetic timeout")
            content = {"sections": {"experience": [
                {"text": "参与 Java 开发", "fact_ids": [fact.id]}]}}
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        })
    monkeypatch.setattr(httpx, "post", post)
    graph = build_graph(DeepSeekAdapter(api_key="test"), lambda requirements: [fact], MemorySaver())
    config = {"configurable": {"thread_id": run_id}}
    def execute(task):
        initial = {"jd_text": "招聘 Java 开发", "job_id": 83} if task["attempt_count"] == 1 else None
        graph.invoke(initial, config)
        return "waiting_approval"
    worker = TaskWorker("unused", execute)
    settlements = []
    monkeypatch.setattr(worker, "_settle", lambda *args, **kwargs: settlements.append(args[1]))
    worker._process({"run_id": run_id, "job_id": 83, "attempt_count": 1})
    worker._process({"run_id": run_id, "job_id": 83, "attempt_count": 2})
    assert settlements == ["retry_wait", "waiting_approval"]
    # A manual rejection runs on the request thread, not in worker attempt 2.
    graph.invoke(Command(resume={"approved": False, "feedback": "简洁表达"}), config)
    assert graph.get_state(config).values["status"] == WorkflowStatus.WAITING_APPROVAL
    observed = [json.loads(record.message) for record in caplog.records
                if record.name == "applypilot.model_usage"]
    finished = [e for e in observed if e["event"] == "model_call_finished"]
    assert [e["step"] for e in finished] == [
        "parse_jd", "generate_resume", "generate_resume", "semantic_review",
        "generate_resume", "semantic_review",
    ]
    assert [e["attempt_count"] for e in finished] == [1, 1, 2, 2, None, None]
    assert all(e["run_id"] == run_id and e["job_id"] == 83 for e in finished)
    assert len({e["call_id"] for e in finished}) == 6
    assert finished[1]["outcome"] == "retryable_error"
    assert finished[1]["usage_status"] == "unavailable"
    assert finished[1]["total_tokens"] is None
    assert all(e["usage_status"] == "reported" for i, e in enumerate(finished) if i != 1)


def test_invalid_context_tags_are_not_written_to_logs(monkeypatch, caplog):
    from applypilot.model_usage import model_call_scope

    caplog.set_level(logging.INFO, logger="applypilot.model_usage")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(503))
    from applypilot.model_adapter import RetryableModelError
    import pytest
    with model_call_scope(run_id="private-person-name", job_id=True,
                          attempt_count=-1, step="private-free-text"):
        with pytest.raises(RetryableModelError):
            DeepSeekAdapter(api_key="test").complete("s", "u")
    for record in caplog.records:
        if record.name == "applypilot.model_usage":
            item = json.loads(record.message)
            assert all(item[key] is None for key in ("run_id", "job_id", "attempt_count", "step"))
    assert "private-" not in caplog.text
