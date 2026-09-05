"""简历事实导入。

对应 docs/design.md 第 2.1、3.1 节：从现有简历提取结构化事实
草稿，人工修正后进入事实库。提取结果一律以 self_report 证据
类型入库，由用户在审核流程中补充证据引用。
"""

from __future__ import annotations

import json
import uuid

from pydantic import ValidationError

from .jd_parser import _extract_json
from .model_adapter import ModelAdapter
from .schemas import Fact

_SYSTEM_PROMPT = """你是简历事实提取器。从用户给出的简历文本中提取个人事实，输出 JSON：
{"facts": [{
  "fact_type": "internship|project|skill|award|education",
  "source_name": "经历来源，如公司名、项目名",
  "content": "不可继续拆分的事实描述，一条事实只表达一个动作、结果或技能",
  "skills": ["涉及的技能标签"],
  "metrics": ["原文中出现的量化指标，逐字复制"],
  "start_date": "YYYY-MM-DD 或 null",
  "end_date": "YYYY-MM-DD 或 null"
}]}
规则：
- 只输出 JSON。
- 只提取简历原文中存在的内容，禁止补充或润色出原文没有的事实。
- 量化指标必须逐字来自原文。
"""


class FactImportError(Exception):
    pass


def extract_facts(resume_text: str, adapter: ModelAdapter) -> tuple[list[Fact], int]:
    """从简历文本提取事实草稿，返回 (事实列表, 丢弃条数)。

    日期等单字段不合法时置空后保留事实，避免整条经历被静默丢弃。
    """
    output = adapter.complete(_SYSTEM_PROMPT, resume_text)
    try:
        data = json.loads(_extract_json(output))
    except Exception as e:
        raise FactImportError(f"模型输出无法解析: {e}") from e

    facts: list[Fact] = []
    skipped = 0
    for item in data.get("facts", []):
        item["id"] = f"fact_{uuid.uuid4().hex[:12]}"
        item.setdefault("evidence_type", "self_report")
        try:
            facts.append(Fact.model_validate(item))
        except ValidationError:
            item["start_date"] = None
            item["end_date"] = None
            try:
                facts.append(Fact.model_validate(item))
            except ValidationError:
                skipped += 1
    if not facts and skipped:
        raise FactImportError(f"全部 {skipped} 条事实结构不合法")
    return facts, skipped
