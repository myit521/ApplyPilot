"""JD 解析节点测试（docs/design.md 第 8.1、10 节）。"""

import pytest

from applypilot.jd_parser import JDParseError, parse_jd
from applypilot.schemas import JobRequirements

VALID_OUTPUT = """
{
  "job_title": "Java 后端开发工程师",
  "job_category": "后端开发",
  "location": "杭州",
  "required": ["熟悉 Java 和 Spring Boot", "2027 届本科及以上"],
  "preferred": ["有 Agent 开发经验者优先"],
  "responsibilities": ["参与后端服务开发"],
  "keywords": [
    {"term": "Java", "importance": "required"},
    {"term": "LangGraph", "importance": "preferred"}
  ],
  "education_requirement": "本科及以上",
  "graduation_time_requirement": "2027 届",
  "experience_requirement": "有实习经验者优先",
  "unknowns": ["薪资范围"]
}
"""


class FakeAdapter:
    """按预设脚本依次返回输出的假适配器。"""

    def __init__(self, outputs: list[str]):
        self.outputs = outputs
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        return self.outputs.pop(0)


def test_valid_output_parsed():
    adapter = FakeAdapter([VALID_OUTPUT])
    result = parse_jd("某 JD 原文", adapter)
    assert result.job_title == "Java 后端开发工程师"
    assert result.required == ["熟悉 Java 和 Spring Boot", "2027 届本科及以上"]
    assert result.preferred == ["有 Agent 开发经验者优先"]
    assert len(adapter.calls) == 1


def test_code_fence_tolerated():
    adapter = FakeAdapter(["好的，解析结果如下：\n```json\n" + VALID_OUTPUT + "\n```"])
    result = parse_jd("某 JD 原文", adapter)
    assert result.location == "杭州"


def test_invalid_output_repaired_once():
    adapter = FakeAdapter(["这不是 JSON", VALID_OUTPUT])
    result = parse_jd("某 JD 原文", adapter)
    assert result.job_title == "Java 后端开发工程师"
    assert len(adapter.calls) == 2
    assert "校验错误" in adapter.calls[1]


def test_invalid_twice_raises_with_summary():
    adapter = FakeAdapter(["不是 JSON", "{\"job_title\": 但只有半截"])
    with pytest.raises(JDParseError) as exc_info:
        parse_jd("某 JD 原文", adapter)
    assert exc_info.value.raw_output_summary


def test_schema_defaults_for_missing_fields():
    adapter = FakeAdapter(['{"job_title": "后端实习生"}'])
    result = parse_jd("某 JD 原文", adapter)
    assert result.required == []
    assert result.unknowns == []


def test_required_preferred_are_separate_fields():
    """结构上保证加分项无法被混入必备条件。"""
    result = JobRequirements.model_validate(
        {"required": ["A"], "preferred": ["B"]}
    )
    assert "B" not in result.required
