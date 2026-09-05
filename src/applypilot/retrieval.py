"""混合检索的合并与加权评分。

对应 docs/design.md 第 8.2 节。检索分三步：元数据硬过滤、
PostgreSQL 全文检索、pgvector 语义召回。本模块负责第三步之后
的纯逻辑部分：合并去重、可解释加权打分、Top-K 截断。

全文检索和向量相似度的具体命中由数据库层产生，以
`{fact_id: 归一化得分}` 的形式传入，本模块不依赖数据库，
便于独立测试。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from .schemas import Fact


class MatchSource(StrEnum):
    FULLTEXT = "fulltext"
    VECTOR = "vector"


class RetrievalWeights(BaseModel):
    """可解释的加权分数权重，之和为 1。"""

    skill: float = 0.5
    fulltext: float = 0.2
    vector: float = 0.3


class ScoredFact(BaseModel):
    """一条召回事实及其得分构成，保留检索来源用于解释。"""

    fact: Fact
    score: float
    sources: list[MatchSource]
    skill_coverage: float = Field(description="该事实覆盖 JD 必备技能的比例")
    fulltext_score: float = 0.0
    vector_score: float = 0.0


def hard_filter(facts: list[Fact]) -> list[Fact]:
    """元数据硬过滤：只有启用状态的事实允许进入检索。"""
    return [f for f in facts if f.enabled]


def merge_and_score(
    facts: list[Fact],
    required_skills: list[str],
    fulltext_hits: dict[str, float],
    vector_hits: dict[str, float],
    weights: RetrievalWeights | None = None,
    top_k: int = 5,
) -> list[ScoredFact]:
    """合并全文与向量命中，按加权分数排序并返回前 top_k 条。

    加权分数 = 必备技能覆盖率 * w_skill + 全文得分 * w_fts + 向量得分 * w_vec
    """
    weights = weights or RetrievalWeights()
    facts_by_id = {f.id: f for f in hard_filter(facts)}
    hit_ids = (set(fulltext_hits) | set(vector_hits)) & set(facts_by_id)

    required_set = set(required_skills)
    scored: list[ScoredFact] = []
    for fid in hit_ids:
        fact = facts_by_id[fid]
        coverage = (
            len(required_set & set(fact.skills)) / len(required_set)
            if required_set
            else 0.0
        )
        fts = fulltext_hits.get(fid, 0.0)
        vec = vector_hits.get(fid, 0.0)
        score = (
            coverage * weights.skill
            + fts * weights.fulltext
            + vec * weights.vector
        )
        sources = []
        if fid in fulltext_hits:
            sources.append(MatchSource.FULLTEXT)
        if fid in vector_hits:
            sources.append(MatchSource.VECTOR)
        scored.append(
            ScoredFact(
                fact=fact,
                score=round(score, 6),
                sources=sources,
                skill_coverage=coverage,
                fulltext_score=fts,
                vector_score=vec,
            )
        )

    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[:top_k]
