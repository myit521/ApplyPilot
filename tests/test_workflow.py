"""工作流测试：正常通过、校验退回、重试超限、人工批准/退回路径。

对应 docs/design.md 第 7、8.4、8.5 节与第 12.1 节工作流测试要求。
使用内存 checkpointer 验证节点级持久化与中断恢复。
"""

import json

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from applypilot.schemas import Fact, FactType
from applypilot.workflow import MAX_VALIDATION_RETRIES, WorkflowStatus, build_graph

FACTS = [
    Fact(
        id="fact_01",
        fact_type=FactType.INTERNSHIP,
        source_name="亚信实习",
        content="承担运维工具批量执行模块开发",
        skills=["Java", "Spring Boot"],
        metrics=["6 个批量接口"],
    )
]

JD_REQUIREMENTS = {
    "job_title": "Java 后端",
    "required": ["熟悉 Java"],
    "preferred": [],
    "responsibilities": [],
    "keywords": [],
    "unknowns": [],
}

GOOD_CLAIM = {
    "claims": [
        {"text": "承担批量执行模块开发，交付 6 个批量接口",
         "fact_ids": ["fact_01"], "matched_requirements": ["Java"]}
    ]
}

BAD_CLAIM = {
    "claims": [
        {"text": "独立完成架构设计，QPS 提升 999%",
         "fact_ids": ["fact_01"], "matched_requirements": []}
    ]
}


class ScriptedAdapter:
    """按脚本依次返回 JD 解析结果和生成结果。"""

    def complete(self, system: str, user: str) -> str:
        if "职位描述解析器" in system:
            return self.jd_output
        if "事实一致性复核员" in system:
            return self.semantic_output
        self.generate_calls += 1
        return self.generate_outputs.pop(0)

    def __init__(self, jd_output: str, generate_outputs: list[str], semantic_output: str = '{"violations": []}'):
        self.jd_output = jd_output
        self.generate_outputs = list(generate_outputs)
        self.semantic_output = semantic_output
        self.generate_calls = 0


def make_workflow(adapter):
    graph = build_graph(adapter, retriever=lambda req: FACTS, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "test-run"}}
    return graph, config


def run_until_interrupt(graph, config):
    return graph.invoke({"jd_text": "招聘 Java 后端工程师", "validation_retries": 0}, config)


def test_happy_path_reaches_approval_and_ready():
    adapter = ScriptedAdapter(json.dumps(JD_REQUIREMENTS), [json.dumps(GOOD_CLAIM)])
    graph, config = make_workflow(adapter)
    run_until_interrupt(graph, config)

    state = graph.get_state(config)
    assert state.values["status"] == WorkflowStatus.WAITING_APPROVAL
    assert state.next  # 停在审批节点

    graph.invoke(Command(resume={"approved": True}), config)
    assert graph.get_state(config).values["status"] == WorkflowStatus.READY_TO_APPLY


def test_validation_failure_retries_then_succeeds():
    adapter = ScriptedAdapter(
        json.dumps(JD_REQUIREMENTS),
        [json.dumps(BAD_CLAIM), json.dumps(GOOD_CLAIM)],
    )
    graph, config = make_workflow(adapter)
    run_until_interrupt(graph, config)

    state = graph.get_state(config)
    assert state.values["status"] == WorkflowStatus.WAITING_APPROVAL
    assert state.values["validation_retries"] == 1
    assert adapter.generate_calls == 2


def test_validation_failure_exceeds_retries_goes_failed():
    outputs = [json.dumps(BAD_CLAIM)] * (MAX_VALIDATION_RETRIES + 1)
    adapter = ScriptedAdapter(json.dumps(JD_REQUIREMENTS), outputs)
    graph, config = make_workflow(adapter)
    graph.invoke({"jd_text": "某 JD", "validation_retries": 0}, config)

    state = graph.get_state(config)
    assert state.values["status"] == WorkflowStatus.FAILED
    assert adapter.generate_calls == MAX_VALIDATION_RETRIES + 1


def test_human_rejection_regenerates():
    adapter = ScriptedAdapter(
        json.dumps(JD_REQUIREMENTS),
        [json.dumps(GOOD_CLAIM), json.dumps(GOOD_CLAIM)],
    )
    graph, config = make_workflow(adapter)
    run_until_interrupt(graph, config)

    graph.invoke(Command(resume={"approved": False, "feedback": "精简一点"}), config)
    state = graph.get_state(config)
    # 退回后重新生成并再次停在审批
    assert state.values["status"] == WorkflowStatus.WAITING_APPROVAL
    assert adapter.generate_calls == 2


def test_state_persisted_per_node():
    """每个节点完成后状态可见，支持从最后一个成功节点恢复。"""
    adapter = ScriptedAdapter(json.dumps(JD_REQUIREMENTS), [json.dumps(GOOD_CLAIM)])
    graph, config = make_workflow(adapter)
    run_until_interrupt(graph, config)

    history = [
        s.values["status"]
        for s in graph.get_state_history(config)
        if "status" in s.values
    ]
    assert WorkflowStatus.RETRIEVING_FACTS in history
    assert WorkflowStatus.GENERATING_RESUME in history
    assert WorkflowStatus.VALIDATING_FACTS in history
    assert history[0] == WorkflowStatus.WAITING_APPROVAL


def test_jd_parse_failure_goes_failed():
    adapter = ScriptedAdapter("这不是 JSON", [])
    adapter.jd_output = "仍然不是 JSON"
    graph, config = make_workflow(adapter)
    graph.invoke({"jd_text": "某 JD", "validation_retries": 0}, config)
    assert graph.get_state(config).values["status"] == WorkflowStatus.FAILED
