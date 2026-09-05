"""简历生成节点测试（docs/design.md 第 8.3 节）。"""

import pytest

from applypilot.generator import (
    ResumeGenerationError,
    build_prompt,
    generate_resume,
)
from applypilot.schemas import Fact, FactType, JobRequirements
from applypilot.validation import validate_claims

REQUIREMENTS = JobRequirements(
    job_title="Java 后端开发",
    required=["熟悉 Java", "了解异步任务"],
    preferred=["有 Agent 经验优先"],
)

FACTS = [
    Fact(
        id="fact_01",
        fact_type=FactType.INTERNSHIP,
        source_name="亚信实习",
        content="承担运维工具批量执行模块开发",
        skills=["Java", "Spring Boot"],
        metrics=["6 个批量接口"],
    ),
    Fact(
        id="fact_02",
        fact_type=FactType.PROJECT,
        source_name="商城项目",
        content="搭建 Testcontainers 集成测试基座",
        skills=["Docker", "JUnit"],
    ),
]

GENERATED = """
{"claims": [
  {"text": "承担批量执行模块开发，交付 6 个批量接口",
   "fact_ids": ["fact_01"], "matched_requirements": ["Java", "异步任务"]}
]}
"""


class FakeAdapter:
    def __init__(self, output: str):
        self.output = output
        self.prompts: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.prompts.append((system, user))
        return self.output


def test_prompt_contains_facts_and_constraints():
    system, user = build_prompt(REQUIREMENTS, FACTS)
    assert "禁止创造" in system
    assert "逐字复制" in system
    assert "fact_01" in user and "6 个批量接口" in user
    assert "熟悉 Java" in user


def test_generate_returns_claims_with_fact_ids():
    adapter = FakeAdapter(GENERATED)
    claims = generate_resume(REQUIREMENTS, FACTS, adapter)
    assert len(claims) == 1
    assert claims[0].fact_ids == ["fact_01"]


def test_generated_claims_pass_validation():
    """生成结果经校验模块判定合规，形成生成-校验闭环。"""
    claims = generate_resume(REQUIREMENTS, FACTS, FakeAdapter(GENERATED))
    assert validate_claims(claims, FACTS) == []


def test_invalid_output_raises():
    with pytest.raises(ResumeGenerationError) as exc_info:
        generate_resume(REQUIREMENTS, FACTS, FakeAdapter("无法解析的输出"))
    assert exc_info.value.raw_output_summary
