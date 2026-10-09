"""语义层事实复核。

对应 docs/design.md 第 8.4 节后段：确定性规则通过后，由模型
检查表述语义是否超出事实含义。真实冒烟中确认了这一层的必要
性——"商城项目的测试基座"被写成"保障交易核心系统稳定性"，
不含虚构数字、技能也在标签内，确定性规则无法拦截。
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError as PydanticValidationError

from .model_adapter import ModelAdapter, RetryableModelError
from .schemas import (
    ErrorCode,
    Fact,
    ResumeClaim,
    SemanticReviewResponse,
    ValidationError,
)

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
        fact_lines = []
        for fact in cited:
            line = f"  - {fact.content}（技能: {', '.join(fact.skills)}）"
            if fact.fact_type == "education":
                education = fact.model_dump(mode="json", include={
                    "source_name", "school", "degree", "major", "start_date", "end_date",
                })
                line += "\n    教育资料: " + json.dumps(education, ensure_ascii=False)
            fact_lines.append(line)
        fact_text = "\n".join(fact_lines)
        parts.append(f"表述: {claim.text}\n引用事实:\n{fact_text}")
    return "\n\n".join(parts)


def semantic_check(
    claims: list[ResumeClaim],
    facts: list[Fact],
    adapter: ModelAdapter,
) -> list[ValidationError]:
    """复核语义越界；复核服务或响应无效时返回阻断错误。"""
    if not claims:
        return []
    facts_by_id = {f.id: f for f in facts}
    try:
        output = adapter.complete(_SYSTEM_PROMPT, _build_user_prompt(claims, facts_by_id))
    except RetryableModelError:
        raise
    except Exception:
        return [_unavailable_error(claims)]
    try:
        data = json.loads(_semantic_json_text(output), object_pairs_hook=_unique_object)
        response = SemanticReviewResponse.model_validate(data)
    except (ValueError, PydanticValidationError, TypeError):
        return [_unavailable_error(claims)]

    claim_texts = {claim.text for claim in claims}
    if any(violation.claim_text not in claim_texts for violation in response.violations):
        return [_unavailable_error(claims)]

    return [
        ValidationError(
            code=ErrorCode.SEMANTIC_OVERRUN,
            claim_text=violation.claim_text,
            detail=violation.reason,
            suggestion="弱化表述至事实原文的含义范围内",
        )
        for violation in response.violations
    ]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous JSON objects with repeated keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _semantic_json_text(output: str) -> str:
    """Accept one JSON object, optionally inside a complete Markdown fence."""
    stripped = output.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL | re.IGNORECASE)
    return fenced.group(1) if fenced else stripped


def _unavailable_error(claims: list[ResumeClaim]) -> ValidationError:
    return ValidationError(
        code=ErrorCode.SEMANTIC_REVIEW_UNAVAILABLE,
        claim_text=claims[0].text,
        detail="语义复核未能返回有效结果，不能确认该简历内容",
        suggestion="检查复核服务后重新运行，或转人工复核",
    )
