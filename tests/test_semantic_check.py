"""语义复核层测试（docs/design.md 第 8.4 节后段）。"""

import json

import pytest
from langgraph.checkpoint.memory import MemorySaver

from applypilot.schemas import Fact, FactType, ResumeClaim
from applypilot.semantic_check import semantic_check
from applypilot.workflow import WorkflowStatus, build_graph
from tests.test_workflow import (
    FACTS,
    GOOD_CLAIM,
    JD_REQUIREMENTS,
    ScriptedAdapter,
)

OVERRUN_CLAIM = ResumeClaim(
    text="使用 Testcontainers 搭建集成测试基座，保障交易核心系统后端服务的稳定性",
    fact_ids=["fact_01"],
)


class SemanticAdapter:
    def __init__(self, output: str):
        self.output = output

    def complete(self, system: str, user: str) -> str:
        assert "事实一致性复核员" in system
        assert "引用事实" in user
        return self.output


def test_overrun_detected():
    output = json.dumps({"violations": [{"claim_text": OVERRUN_CLAIM.text, "reason": "事实未提及保障交易系统"}]})
    errors = semantic_check([OVERRUN_CLAIM], FACTS, SemanticAdapter(output))
    assert len(errors) == 1
    assert errors[0].code == "SEMANTIC_OVERRUN"


def test_no_violation_passes():
    errors = semantic_check([OVERRUN_CLAIM], FACTS, SemanticAdapter('{"violations": []}'))
    assert errors == []


def test_complete_json_code_fence_passes():
    errors = semantic_check(
        [OVERRUN_CLAIM], FACTS, SemanticAdapter('```json\n{"violations": []}\n```')
    )
    assert errors == []


@pytest.mark.parametrize("output", [
    "无法解析",
    "",
    '结果如下: {"violations": []}',
    "{}",
    '{"violations": null}',
    '{"violations": [], "violations": [{"claim_text": "越界", "reason": "reason"}]}',
    '{"violations": [{"claim_text": "表述"}]}',
    '{"violations": [{"claim_text": "未知表述", "reason": "越界"}]}',
])
def test_invalid_review_output_blocks_as_unavailable(output):
    errors = semantic_check([OVERRUN_CLAIM], FACTS, SemanticAdapter(output))
    assert len(errors) == 1
    assert errors[0].code == "SEMANTIC_REVIEW_UNAVAILABLE"


def test_review_timeout_blocks_as_unavailable():
    class TimeoutAdapter:
        def complete(self, system: str, user: str) -> str:
            raise TimeoutError("review timed out")

    errors = semantic_check([OVERRUN_CLAIM], FACTS, TimeoutAdapter())
    assert len(errors) == 1
    assert errors[0].code == "SEMANTIC_REVIEW_UNAVAILABLE"


def test_workflow_retries_on_semantic_overrun():
    """语义越界触发与确定性失败相同的退回-重试路径。"""
    claim_text = GOOD_CLAIM["sections"]["experience"][0]["text"]
    violations = json.dumps({"violations": [{"claim_text": claim_text, "reason": "超出事实含义"}]})
    adapter = ScriptedAdapter(json.dumps(JD_REQUIREMENTS), [json.dumps(GOOD_CLAIM), json.dumps(GOOD_CLAIM)])
    # 第一次语义复核报越界，第二次放行
    outputs = [violations, '{"violations": []}']
    original_complete = adapter.complete

    def complete(system: str, user: str) -> str:
        if "事实一致性复核员" in system:
            return outputs.pop(0)
        return original_complete(system, user)

    adapter.complete = complete
    graph = build_graph(adapter, retriever=lambda req: FACTS, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "semantic-retry"}}
    graph.invoke({"jd_text": "某 JD", "validation_retries": 0}, config)

    state = graph.get_state(config)
    assert state.values["status"] == WorkflowStatus.WAITING_APPROVAL
    assert state.values["validation_retries"] == 1
    assert adapter.generate_calls == 2


def test_workflow_stops_for_manual_handling_when_semantic_review_is_unavailable():
    adapter = ScriptedAdapter(json.dumps(JD_REQUIREMENTS), [json.dumps(GOOD_CLAIM)], semantic_output="")
    graph = build_graph(adapter, retriever=lambda req: FACTS, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "review-unavailable"}}
    graph.invoke({"jd_text": "某 JD", "validation_retries": 0}, config)

    state = graph.get_state(config).values
    assert state["status"] == WorkflowStatus.FAILED
    assert state["validation_errors"][0].code == "SEMANTIC_REVIEW_UNAVAILABLE"
    assert "人工" in state["error"]
    assert adapter.generate_calls == 1
