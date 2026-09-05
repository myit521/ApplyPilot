"""有限状态 Agent 工作流。

对应 docs/design.md 第 7 节。使用 LangGraph 显式节点与条件边：
PARSING_JD -> RETRIEVING_FACTS -> GENERATING_RESUME -> VALIDATING_FACTS
-> WAITING_APPROVAL（人工审批中断点）-> READY_TO_APPLY

- 校验失败退回生成节点，最多两次，超限转 FAILED（第 8.4 节）。
- 通过 checkpointer 在每个节点完成后持久化状态，服务重启后从
  最后一个成功节点恢复（第 10 节）。生产使用 PostgreSQL
  checkpointer，测试使用内存 checkpointer，图结构不变。
- 表单填写节点（FILLING_FORM 及之后）在 Playwright 模块完成后接入。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Callable, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from .generator import generate_resume
from .jd_parser import JDParseError, parse_jd
from .model_adapter import ModelAdapter
from .retrieval import ScoredFact
from .schemas import Fact, JobRequirements, ResumeClaim, ValidationError
from .validation import validate_claims

MAX_VALIDATION_RETRIES = 2


class WorkflowStatus(StrEnum):
    PARSING_JD = "PARSING_JD"
    RETRIEVING_FACTS = "RETRIEVING_FACTS"
    GENERATING_RESUME = "GENERATING_RESUME"
    VALIDATING_FACTS = "VALIDATING_FACTS"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    READY_TO_APPLY = "READY_TO_APPLY"
    FAILED = "FAILED"


class WorkflowState(TypedDict, total=False):
    jd_text: str
    requirements: JobRequirements | None
    retrieved_facts: list[Fact]
    claims: list[ResumeClaim]
    validation_errors: list[ValidationError]
    validation_retries: int
    status: WorkflowStatus
    error: str


# 事实检索由数据库层提供（docs/design.md 第 8.2 节），
# 以 {fact_id: 得分} 形式返回全文/向量命中；此处只声明依赖。
FactRetriever = Callable[[JobRequirements], list[Fact]]


def build_graph(
    adapter: ModelAdapter,
    retriever: FactRetriever,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """构造工作流图。adapter 与 retriever 在部署时注入。"""

    def node_parse_jd(state: WorkflowState) -> dict:
        try:
            requirements = parse_jd(state["jd_text"], adapter)
        except JDParseError as e:
            return {"status": WorkflowStatus.FAILED, "error": str(e)}
        return {"requirements": requirements, "status": WorkflowStatus.RETRIEVING_FACTS}

    def node_retrieve(state: WorkflowState) -> dict:
        facts = retriever(state["requirements"])
        return {"retrieved_facts": facts, "status": WorkflowStatus.GENERATING_RESUME}

    def node_generate(state: WorkflowState) -> dict:
        claims = generate_resume(state["requirements"], state["retrieved_facts"], adapter)
        return {"claims": claims, "status": WorkflowStatus.VALIDATING_FACTS}

    def node_validate(state: WorkflowState) -> dict:
        errors = validate_claims(state["claims"], state["retrieved_facts"])
        retries = state.get("validation_retries", 0)
        if errors and retries < MAX_VALIDATION_RETRIES:
            return {
                "validation_errors": errors,
                "validation_retries": retries + 1,
                "status": WorkflowStatus.GENERATING_RESUME,
            }
        if errors:
            return {
                "validation_errors": errors,
                "status": WorkflowStatus.FAILED,
                "error": "事实校验连续失败，等待人工处理",
            }
        return {"validation_errors": [], "status": WorkflowStatus.WAITING_APPROVAL}

    def node_approval(state: WorkflowState) -> dict:
        decision = interrupt({"claims": state["claims"]})
        if decision.get("approved"):
            return {"status": WorkflowStatus.READY_TO_APPLY}
        # 用户退回：携带修改意见重新生成，重试计数清零
        return {
            "status": WorkflowStatus.GENERATING_RESUME,
            "validation_retries": 0,
        }

    def route_after_validation(state: WorkflowState) -> str:
        if state["status"] == WorkflowStatus.GENERATING_RESUME:
            return "generate"
        if state["status"] == WorkflowStatus.FAILED:
            return END
        return "approval"

    def route_after_approval(state: WorkflowState) -> str:
        if state["status"] == WorkflowStatus.READY_TO_APPLY:
            return END
        return "generate"

    graph = StateGraph(WorkflowState)
    graph.add_node("parse_jd", node_parse_jd)
    graph.add_node("retrieve", node_retrieve)
    graph.add_node("generate", node_generate)
    graph.add_node("validate", node_validate)
    graph.add_node("approval", node_approval)

    graph.set_entry_point("parse_jd")
    graph.add_conditional_edges(
        "parse_jd",
        lambda s: END if s["status"] == WorkflowStatus.FAILED else "retrieve",
        {"retrieve": "retrieve", END: END},
    )
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", "validate")
    graph.add_conditional_edges(
        "validate", route_after_validation, {"generate": "generate", "approval": "approval", END: END}
    )
    graph.add_conditional_edges(
        "approval", route_after_approval, {"generate": "generate", END: END}
    )

    return graph.compile(checkpointer=checkpointer)
