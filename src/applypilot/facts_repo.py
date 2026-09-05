"""事实仓储：facts 表的读写。

对应 docs/design.md 第 6.1、11 节：删除采用停用（enabled=false）
而非物理删除，已生成版本继续保留引用快照。
"""

from __future__ import annotations

import psycopg

from .schemas import Fact, FactType

_COLUMNS = (
    "id, fact_type, source_name, content, start_date, end_date, "
    "skills, metrics, evidence_type, evidence_ref, enabled"
)


def _row_to_fact(row: dict) -> Fact:
    return Fact(**{k: row[k] for k in Fact.model_fields if k in row})


def upsert_fact(conn: psycopg.Connection, fact: Fact, embedding: list[float] | None = None) -> None:
    conn.execute(
        f"""
        INSERT INTO facts ({_COLUMNS}, embedding)
        VALUES (%(id)s, %(fact_type)s, %(source_name)s, %(content)s,
                %(start_date)s, %(end_date)s, %(skills)s, %(metrics)s,
                %(evidence_type)s, %(evidence_ref)s, %(enabled)s, %(embedding)s)
        ON CONFLICT (id) DO UPDATE SET
            fact_type = EXCLUDED.fact_type,
            source_name = EXCLUDED.source_name,
            content = EXCLUDED.content,
            start_date = EXCLUDED.start_date,
            end_date = EXCLUDED.end_date,
            skills = EXCLUDED.skills,
            metrics = EXCLUDED.metrics,
            evidence_type = EXCLUDED.evidence_type,
            evidence_ref = EXCLUDED.evidence_ref,
            enabled = EXCLUDED.enabled,
            embedding = COALESCE(EXCLUDED.embedding, facts.embedding),
            updated_at = now()
        """,
        {**fact.model_dump(), "embedding": embedding},
    )


def get_fact(conn: psycopg.Connection, fact_id: str) -> Fact | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM facts WHERE id = %s", (fact_id,)
    ).fetchone()
    return _row_to_fact(row) if row else None


def list_facts(
    conn: psycopg.Connection,
    fact_type: FactType | None = None,
    enabled_only: bool = True,
) -> list[Fact]:
    sql = f"SELECT {_COLUMNS} FROM facts WHERE TRUE"
    params: list = []
    if enabled_only:
        sql += " AND enabled"
    if fact_type is not None:
        sql += " AND fact_type = %s"
        params.append(fact_type.value)
    sql += " ORDER BY created_at"
    return [_row_to_fact(r) for r in conn.execute(sql, params).fetchall()]


def disable_fact(conn: psycopg.Connection, fact_id: str) -> bool:
    """停用事实。返回 False 表示事实不存在。"""
    result = conn.execute(
        "UPDATE facts SET enabled = FALSE, updated_at = now() WHERE id = %s",
        (fact_id,),
    )
    return result.rowcount > 0
