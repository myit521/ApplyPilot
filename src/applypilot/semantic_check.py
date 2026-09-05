"""语义层事实复核。

对应 docs/design.md 第 8.4 节后段：确定性规则通过后，由模型
检查表述语义是否超出事实含义。真实冒烟中确认了这一层的必要
性——"商城项目的测试基座"被写成"保障交易核心系统稳定性"，
不含虚构数字、技能也在标签内，确定性规则无法拦截。
"""

from __future__ import annotations

import json

from pydantic import ValidationError as PydanticValidationError

from .jd_parser import JDParseError, _extract_json
from .model_adapter import ModelAdapter
from .schemas import ErrorCode, Fact, ResumeClaim, ValidationError

_SYSTEM_PROMPT = """你是事实一致性复核员。给定简历表述及其引用的事实原文，判断表述是否超出事实含义。
判定越界的情况包括：
- 把"参与评审/参与联调"改写为"独立设计"或"负责实现"；
- 把组内提出的方案描述为个人原创；
- 把计划中、未验收的功能写成已完成；
- 添加事实中不存在的技术、业务背景或效果（例如事实说的是商城项目，表述却声称保障了其他系统）。
规则：只输出 JSON：{"violations": [{"claim_text": "...", "reason": "..."}]}；没有越界时 violations 为空数组。
"""


def _build_user_prompt(claims: list[ResumeClaim], facts_by_id: dict[str, Fact]) -> str:
    parts = []
    for claim in claims:
        cited = [facts_by_id[fid] for fid in claim.fact_ids if fid in facts_by_id]
        fact_text = "\n".join(f"  - {f.content}（技能: {', '.join(f.skills)}）" for f in cited)
        parts.append(f"表述: {claim.text}\n引用事实:\n{fact_text}")
    return "\n\n".join(parts)


def semantic_check(
    claims: list[ResumeClaim],
    facts: list[Fact],
    adapter: ModelAdapter,
) -> list[ValidationError]:
    """模型复核语义越界。模型输出无法解析时放行（不阻断流程，
    语义层是补充而非硬门），由下一次迭代收紧。"""
    if not claims:
        return []
    facts_by_id = {f.id: f for f in facts}
    output = adapter.complete(_SYSTEM_PROMPT, _build_user_prompt(claims, facts_by_id))
    try:
        data = json.loads(_extract_json(output))
        violations = data.get("violations", [])
        if not isinstance(violations, list):
            raise ValueError("violations 不是数组")
    except (JDParseError, ValueError, PydanticValidationError):
        return []

    return [
        ValidationError(
            code=ErrorCode.SEMANTIC_OVERRUN,
            claim_text=str(v.get("claim_text", "")),
            detail=str(v.get("reason", "语义超出事实含义")),
            suggestion="弱化表述至事实原文的含义范围内",
        )
        for v in violations
        if isinstance(v, dict)
    ]
