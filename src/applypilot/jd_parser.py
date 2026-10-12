"""JD 解析节点。

对应 docs/design.md 第 8.1 节：将职位原文解析为 JobRequirements。
错误处理对应第 10 节：模型输出解析失败时保留原始输出摘要，
使用结构修复提示重试一次，仍失败则抛出 JDParseError。

系统提示词要求区分必备、加分与非要求；Schema 分离字段，
明确的非要求误归类再由解析边界触发一次修复。
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from .model_adapter import ModelAdapter, ModelError
from .jd_polarity import is_explicit_non_requirement
from .schemas import JobRequirements

_SYSTEM_PROMPT = """你是职位描述解析器。将用户给出的招聘 JD 原文解析为 JSON，结构如下：
{
  "job_title": "岗位名称",
  "job_category": "职位类别",
  "location": "工作地点",
  "required": ["必备条件，逐条来自原文"],
  "preferred": ["加分条件，逐条来自原文"],
  "responsibilities": ["岗位职责"],
  "keywords": [{"term": "技术关键词", "importance": "required|preferred|mentioned"}],
  "education_requirement": "学历要求",
  "graduation_time_requirement": "毕业时间要求",
  "experience_requirement": "实习或项目经验要求",
  "unknowns": ["无法从原文确定的信息"]
}
规则：
- 只输出 JSON，不要输出任何解释。
- required 只收录原文明确表述为必备/必须的条件；加分项、优先条件必须放入 preferred，禁止提升为必备。
- 原文没有的信息留空字符串或放入 unknowns，禁止编造。
- “不限制”“不要求”“无需”“非必需”“可选”等非要求，不得写入 required、preferred 或经验要求；其中的技术词只能标为 mentioned。
"""

_REPAIR_PROMPT = """你上一次的输出不是合法 JSON 或不符合要求。请根据下面的校验错误修正，只输出修正后的 JSON。

校验错误：
{error}

你上一次的输出（摘要）：
{output}
"""


class JDParseError(Exception):
    """JD 解析最终失败（含一次修复重试之后）。"""

    def __init__(self, message: str, raw_output_summary: str = ""):
        super().__init__(message)
        self.raw_output_summary = raw_output_summary


def _summarize(text: str, limit: int = 200) -> str:
    return text[:limit] + ("..." if len(text) > limit else "")


def _extract_json(text: str) -> str:
    """从模型输出中提取 JSON 对象，容忍 ```json 代码围栏。"""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise JDParseError("模型输出中找不到 JSON 对象", _summarize(text))
    return match.group(0)


def _parse_output(text: str) -> JobRequirements:
    try:
        data = json.loads(_extract_json(text))
        return JobRequirements.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as e:
        raise JDParseError(f"输出结构不合法: {e}", _summarize(text)) from e


def _parse_checked(text: str, jd_text: str) -> JobRequirements:
    parsed = _parse_output(text)
    clauses = parsed.required + parsed.preferred + [parsed.experience_requirement]
    if any(is_explicit_non_requirement(clause) for clause in clauses):
        raise JDParseError("非要求或可选项不能列为必备、加分或经验要求", _summarize(text))
    negative_segments = [segment for segment in re.split(r"[；。，,:：]", jd_text)
                         if is_explicit_non_requirement(segment)]
    for keyword in parsed.keywords:
        if keyword.importance == "mentioned" or not keyword.term.strip():
            continue
        pattern = rf"(?<![A-Za-z0-9]){re.escape(keyword.term.strip())}(?![A-Za-z0-9])"
        if any(re.search(pattern, segment, flags=re.IGNORECASE)
               for segment in negative_segments):
            raise JDParseError("非要求中的技术词不能标为必备或加分", _summarize(text))
    return parsed


def parse_jd(jd_text: str, adapter: ModelAdapter) -> JobRequirements:
    """解析 JD 原文。模型输出不合法时用修复提示重试一次。"""
    try:
        output = adapter.complete(_SYSTEM_PROMPT, jd_text)
    except ModelError:
        raise

    try:
        return _parse_checked(output, jd_text)
    except JDParseError as first_error:
        repair_user = _REPAIR_PROMPT.format(
            error=str(first_error), output=first_error.raw_output_summary
        )
        repaired = adapter.complete(_SYSTEM_PROMPT, repair_user)
        try:
            return _parse_checked(repaired, jd_text)
        except JDParseError as second_error:
            raise second_error from None
