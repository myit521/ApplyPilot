"""确定性事实校验。

对应 docs/design.md 第 8.4 节中可由规则判定的部分：
- 引用的事实 ID 必须存在且处于启用状态；
- 表述中的数字必须逐字来自被引用事实的 metrics 或 content；
- 表述中出现的技能词必须在被引用事实的 skills 中。

语义层面的越界（如把"参与评审"改写为"独立设计"）由模型复核补充，
不在本模块职责内。
"""

from __future__ import annotations

import re

from .schemas import (
    ErrorCode,
    Fact,
    FactType,
    ResumeClaim,
    ResumeSections,
    ValidationError,
)

# 数字及其常见单位：6 个、82.4%、320ms、3 次、10w+ 等
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?(?:\s?(?:%|个|次|条|ms|s|w\+?|万|倍|人|天|月|年))?")

# 常见技术词表。设计第 8.4 节要求"新出现的技能必须能在引用事实
# 中找到"，但词表之外的技能无法识别——真实冒烟中"熟悉 Java"在
# 事实库完全没有 Java 的情况下通过了校验。该词表与事实库技能
# 标签取并集，用于识别表述中出现的技能词。
TECH_LEXICON = {
    "Java", "Python", "Golang", "Rust", "C++", "JavaScript", "TypeScript",
    "Spring Boot", "Spring Cloud", "Spring", "MySQL", "PostgreSQL", "Redis",
    "Kafka", "RabbitMQ", "Docker", "Kubernetes", "K8s", "LangGraph",
    "LangChain", "FastAPI", "Vue", "React", "Node.js", "Git", "Linux",
    "Nginx", "Elasticsearch", "MongoDB", "MyBatis", "JUnit", "pytest",
    "Playwright", "PyTorch", "TensorFlow", "Hadoop", "Spark", "Flink", "SQL",
}


def _term_in_text(term: str, text: str) -> bool:
    if term.isascii():
        return re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE) is not None
    return term in text


def _skill_covered(term: str, cited_skills: set[str]) -> bool:
    """词 term 是否被任一引用事实的技能覆盖。

    双向子串匹配：技能标签比词更具体（"SQL预览" 覆盖 "SQL"）或
    更宽泛（"Spring" 覆盖 "Spring Boot"）都视为覆盖，边界情况
    交由语义复核层判断。
    """
    folded = term.casefold()
    return any(folded in s.casefold() or s.casefold() in folded for s in cited_skills)


def _cited_facts(claim: ResumeClaim, facts_by_id: dict[str, Fact]) -> list[Fact]:
    return [facts_by_id[fid] for fid in claim.fact_ids if fid in facts_by_id]


def validate_claim(
    claim: ResumeClaim,
    facts_by_id: dict[str, Fact],
    skill_vocabulary: set[str],
) -> list[ValidationError]:
    """校验单条简历表述，返回全部错误（空列表表示通过）。

    `skill_vocabulary` 是事实库全部事实的技能标签集合，用于识别
    表述中出现的技能词；词表之外的词不参与技能边界检查。
    """
    errors: list[ValidationError] = []

    if not claim.fact_ids:
        errors.append(
            ValidationError(
                code=ErrorCode.MISSING_CITATION,
                claim_text=claim.text,
                detail="该表述没有任何事实引用",
                suggestion="为其补充至少一条事实引用，或删除该表述",
            )
        )
        return errors

    cited: list[Fact] = []
    for fid in claim.fact_ids:
        fact = facts_by_id.get(fid)
        if fact is None:
            errors.append(
                ValidationError(
                    code=ErrorCode.UNKNOWN_FACT,
                    claim_text=claim.text,
                    detail=f"引用的事实 {fid} 不存在",
                    suggestion="修正为事实库中存在的 fact_id",
                )
            )
        elif not fact.enabled:
            errors.append(
                ValidationError(
                    code=ErrorCode.FACT_DISABLED,
                    claim_text=claim.text,
                    detail=f"引用的事实 {fid} 已停用",
                    suggestion="改用其他启用状态的事实，或删除该表述",
                )
            )
        else:
            cited.append(fact)

    if not cited:
        return errors

    # 数字边界：表述中的每个数字必须逐字出现在被引用事实的 metrics 或 content 中
    allowed_text = " ".join(
        [m for f in cited for m in f.metrics] + [f.content for f in cited]
    )
    for number in set(_NUMBER_RE.findall(claim.text)):
        if number.strip() not in allowed_text:
            errors.append(
                ValidationError(
                    code=ErrorCode.UNSUPPORTED_NUMBER,
                    claim_text=claim.text,
                    detail=f"数字 '{number.strip()}' 在被引用事实中找不到依据",
                    suggestion="改用被引用事实 metrics 中的原始数值，或删除该数字",
                )
            )

    # 技能边界：表述中命中的技能词必须被引用事实的技能覆盖
    cited_skills = {s for f in cited for s in f.skills}
    for skill in skill_vocabulary:
        if _term_in_text(skill, claim.text) and not _skill_covered(skill, cited_skills):
            errors.append(
                ValidationError(
                    code=ErrorCode.UNSUPPORTED_SKILL,
                    claim_text=claim.text,
                    detail=f"技能 '{skill}' 超出被引用事实的技能范围",
                    suggestion="引用包含该技能的事实，或删除该技能表述",
                )
            )

    return errors


def validate_claims(
    claims: list[ResumeClaim],
    facts: list[Fact],
) -> list[ValidationError]:
    """校验整份简历的全部表述。"""
    facts_by_id = {f.id: f for f in facts}
    skill_vocabulary = {s for f in facts for s in f.skills} | TECH_LEXICON
    errors: list[ValidationError] = []
    for claim in claims:
        errors.extend(validate_claim(claim, facts_by_id, skill_vocabulary))
    return errors


def validate_sections(
    sections: ResumeSections,
    facts: list[Fact],
) -> list[ValidationError]:
    """校验分区结构简历（docs/design.md 第 8.3 节）。

    全部分区适用统一事实校验；education 分区额外要求只能引用
    education 类型事实。
    """
    errors = validate_claims(sections.all_claims(), facts)
    facts_by_id = {f.id: f for f in facts}
    for claim in sections.education:
        for fid in claim.fact_ids:
            fact = facts_by_id.get(fid)
            if fact is not None and fact.fact_type != FactType.EDUCATION:
                errors.append(
                    ValidationError(
                        code=ErrorCode.INVALID_FACT_TYPE,
                        claim_text=claim.text,
                        detail=f"教育背景分区引用了 {fact.fact_type} 类型事实 {fid}",
                        suggestion="教育背景只允许引用 education 类型事实",
                    )
                )
    return errors
