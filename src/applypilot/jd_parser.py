"""JD 解析节点。

对应 docs/design.md 第 8.1 节：将职位原文解析为 JobRequirements。
错误处理对应第 10 节：模型输出解析失败时保留原始输出摘要，
使用结构修复提示重试一次，仍失败则抛出 JDParseError。

模型不得把"加分项"提升为"硬性要求"，该约束写在系统提示词中，
并由 required/preferred 分离的 Schema 在结构上兜底。
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from .model_adapter import ModelAdapter, ModelError
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
"""

_REPAIR_PROMPT = """你上一次的输出不是合法 JSON 或不符合要求的结构。请根据下面的校验错误修正，只输出修正后的 JSON。

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


def parse_jd(jd_text: str, adapter: ModelAdapter) -> JobRequirements:
    """解析 JD 原文。模型输出不合法时用修复提示重试一次。"""
    try:
        output = adapter.complete(_SYSTEM_PROMPT, jd_text)
    except ModelError:
        raise

    try:
        return _parse_output(output)
    except JDParseError as first_error:
        repair_user = _REPAIR_PROMPT.format(
            error=str(first_error), output=first_error.raw_output_summary
        )
        repaired = adapter.complete(_SYSTEM_PROMPT, repair_user)
        try:
            return _parse_output(repaired)
        except JDParseError:
            raise first_error from None
