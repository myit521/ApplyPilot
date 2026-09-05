"""简历生成节点。

对应 docs/design.md 第 8.3 节：模型只能使用检索节点提供的事实
生成内容，输出为分区结构（education/skills/experience），每条
输出必须携带 fact_ids，量化指标必须逐字来自被引用事实的
metrics。生成结果是否合规由 validation 模块独立判定。
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from .jd_parser import JDParseError, _extract_json, _summarize
from .model_adapter import ModelAdapter
from .schemas import Fact, JobRequirements, ResumeSections


class ResumeGenerationError(Exception):
    """生成结果无法解析为合法结构。"""

    def __init__(self, message: str, raw_output_summary: str = ""):
        super().__init__(message)
        self.raw_output_summary = raw_output_summary


def build_prompt(requirements: JobRequirements, facts: list[Fact]) -> tuple[str, str]:
    """构造系统提示词和用户提示词。事实以 JSON 形式逐条列出。"""
    system = """你是简历撰写助手。根据岗位要求和提供的个人事实，为求职者生成定制简历内容。
硬性规则：
- 只能使用提供的事实，禁止创造任何未提供的经历、技能或数字。
- 每条内容必须携带 fact_ids，引用其依据的事实。
- 量化指标必须逐字复制所引用事实 metrics 字段的原文，不得改写或换算。
- "参与评审"类事实不得改写为"独立设计"或"负责实现"。
- education 分区只允许引用 education 类型事实；没有 education 事实时该分区留空。
- skills 分区的每条内容同样必须携带 fact_ids：只能汇总引用事实中出现过的技能，没有事实支撑的技能不要写。
只输出 JSON：
{"sections": {
  "education": [{"text": "...", "fact_ids": ["..."]}],
  "skills": [{"text": "...", "fact_ids": ["..."]}],
  "experience": [{"text": "...", "fact_ids": ["..."], "matched_requirements": ["..."]}]
}}"""

    fact_lines = []
    for f in facts:
        metrics = f"；可用量化指标：{'、'.join(f.metrics)}" if f.metrics else ""
        fact_lines.append(
            f"- fact_id: {f.id}｜类型: {f.fact_type}｜来源: {f.source_name}\n"
            f"  内容: {f.content}\n"
            f"  技能: {', '.join(f.skills)}{metrics}"
        )
    user = (
        f"岗位要求：\n必备条件：{'；'.join(requirements.required)}\n"
        f"加分条件：{'；'.join(requirements.preferred)}\n"
        f"职责：{'；'.join(requirements.responsibilities)}\n\n"
        f"可用事实（只允许使用以下事实）：\n" + "\n".join(fact_lines)
    )
    return system, user


def generate_resume(
    requirements: JobRequirements,
    facts: list[Fact],
    adapter: ModelAdapter,
    feedback: str = "",
) -> ResumeSections:
    """生成分区结构简历内容。

    feedback 为上一轮事实校验的错误摘要（第 8.4 节：失败结果
    附带错误码和修改建议退回生成节点）。
    """
    system, user = build_prompt(requirements, facts)
    if feedback:
        user += f"\n\n你上一轮的输出未通过事实校验，请按以下意见修正：\n{feedback}"
    output = adapter.complete(system, user)
    try:
        data = json.loads(_extract_json(output))
        return ResumeSections.model_validate(data["sections"])
    except (JDParseError, KeyError, TypeError, json.JSONDecodeError, ValidationError) as e:
        raise ResumeGenerationError(
            f"生成结果结构不合法: {e}", _summarize(output)
        ) from e
