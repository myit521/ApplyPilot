"""混合检索的数据库层。

对应 docs/design.md 第 8.2 节：
1. 元数据硬过滤（enabled，在 SQL 中完成）；
2. 全文检索匹配明确技能词和业务词；
3. pgvector 语义召回，与全文结果合并去重。

中文在 PostgreSQL 内建分词下无法正确切词，全文得分采用
"JD 关键词在事实内容/来源/技能标签中的覆盖率"，对中英文
关键词都有效。合并打分由 retrieval.merge_and_score 完成。
"""

from __future__ import annotations

import psycopg

from .embeddings import EmbeddingProvider, NullEmbeddingProvider
from .retrieval import RetrievalWeights, merge_and_score
from .schemas import Fact, JobRequirements

# 全文检索返回的候选上限，之后由加权打分截断到 top_k
_CANDIDATE_LIMIT = 20


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def extract_terms(requirements: JobRequirements) -> list[str]:
    """从 JD 解析结果提取检索词：优先技术关键词，兜底必备条件原文。"""
    terms = [k.term for k in requirements.keywords if k.term.strip()]
    if not terms:
        terms = [r for r in requirements.required if r.strip()]
    return terms


def required_skill_terms(requirements: JobRequirements) -> list[str]:
    """必备级别的关键词，用于技能覆盖率打分。"""
    required = [k.term for k in requirements.keywords if k.importance == "required"]
    return required or extract_terms(requirements)


def fulltext_hits(
    conn: psycopg.Connection, terms: list[str], limit: int = _CANDIDATE_LIMIT
) -> dict[str, float]:
    """全文命中：得分为关键词覆盖率（0,1]。"""
    terms = [t.strip() for t in terms if t.strip()]
    if not terms:
        return {}
    patterns = [f"%{_escape_like(t)}%" for t in terms]
    rows = conn.execute(
        """
        SELECT id, matched::float / %(n)s AS score FROM (
            SELECT id, (
                SELECT count(*) FROM unnest(%(patterns)s::text[]) p
                WHERE content ILIKE p ESCAPE '\\'
                   OR source_name ILIKE p ESCAPE '\\'
                   OR EXISTS (
                        SELECT 1 FROM unnest(skills) s
                        WHERE p ILIKE concat('%%', s, '%%') ESCAPE '\\'
                      )
            ) AS matched
            FROM facts WHERE enabled
        ) scored
        WHERE matched > 0
        ORDER BY score DESC, id
        LIMIT %(limit)s
        """,
        {"patterns": patterns, "n": len(terms), "limit": limit},
    ).fetchall()
    return {r["id"]: float(r["score"]) for r in rows}


def vector_hits(
    conn: psycopg.Connection,
    query_embedding: list[float],
    limit: int = _CANDIDATE_LIMIT,
) -> dict[str, float]:
    """向量命中：余弦相似度（0,1]。"""
    literal = "[" + ",".join(str(x) for x in query_embedding) + "]"
    rows = conn.execute(
        """
        SELECT id, 1 - (embedding <=> %(v)s::vector) AS score
        FROM facts
        WHERE enabled AND embedding IS NOT NULL
        ORDER BY embedding <=> %(v)s::vector
        LIMIT %(limit)s
        """,
        {"v": literal, "limit": limit},
    ).fetchall()
    return {r["id"]: float(r["score"]) for r in rows}


def _query_text(requirements: JobRequirements) -> str:
    parts = [requirements.job_title, requirements.job_category]
    parts += requirements.required + requirements.responsibilities
    parts += [k.term for k in requirements.keywords]
    return " ".join(p for p in parts if p)


class PostgresFactRetriever:
    """工作流注入用的事实检索器（docs/design.md 第 8.2 节）。"""

    def __init__(
        self,
        conn: psycopg.Connection,
        embedding_provider: EmbeddingProvider | None = None,
        weights: RetrievalWeights | None = None,
        top_k: int = 5,
    ):
        self.conn = conn
        self.embedding_provider = embedding_provider or NullEmbeddingProvider()
        self.weights = weights
        self.top_k = top_k

    def __call__(self, requirements: JobRequirements) -> list[Fact]:
        terms = extract_terms(requirements)
        fts = fulltext_hits(self.conn, terms)

        vec: dict[str, float] = {}
        embedding = self.embedding_provider.embed(_query_text(requirements))
        if embedding is not None:
            vec = vector_hits(self.conn, embedding)

        hit_ids = set(fts) | set(vec)
        if not hit_ids:
            return []
        placeholders = ",".join(["%s"] * len(hit_ids))
        rows = self.conn.execute(
            f"SELECT * FROM facts WHERE id IN ({placeholders})",
            list(hit_ids),
        ).fetchall()
        facts = [Fact(**{k: r[k] for k in Fact.model_fields if k in r}) for r in rows]

        scored = merge_and_score(
            facts,
            required_skills=required_skill_terms(requirements),
            fulltext_hits=fts,
            vector_hits=vec,
            weights=self.weights,
            top_k=self.top_k,
        )
        return [s.fact for s in scored]
