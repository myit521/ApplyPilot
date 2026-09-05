"""数据库集成测试：真实 PostgreSQL 容器（testcontainers）。

覆盖设计文档第 12.1 节：事实版本（停用而非删除）、
重复投递约束、全文与向量检索命中。
"""

import pytest
from testcontainers.postgres import PostgresContainer

from applypilot import db, facts_repo, search
from applypilot.schemas import Fact, FactType, JobRequirements, KeywordRequirement


@pytest.fixture(scope="module")
def conn():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        url = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        connection = db.connect(url)
        db.init_schema(connection)
        yield connection


@pytest.fixture()
def clean(conn):
    for table in ("applications", "resume_claims", "resume_versions",
                  "jobs", "workflow_runs", "audit_events", "facts"):
        conn.execute(f"TRUNCATE {table} CASCADE")
    yield conn


def make_fact(fid: str, content: str, skills: list[str], enabled: bool = True) -> Fact:
    return Fact(
        id=fid, fact_type=FactType.INTERNSHIP, source_name="亚信实习",
        content=content, skills=skills, enabled=enabled,
    )


def test_upsert_get_list(clean):
    conn = clean
    facts_repo.upsert_fact(conn, make_fact("f1", "承担批量执行模块开发", ["Java", "MySQL"]))
    facts_repo.upsert_fact(conn, make_fact("f2", "搭建集成测试基座", ["Docker"]))

    fact = facts_repo.get_fact(conn, "f1")
    assert fact.content == "承担批量执行模块开发"
    assert fact.skills == ["Java", "MySQL"]
    assert len(facts_repo.list_facts(conn)) == 2

    # upsert 同一 id 更新而非重复插入
    facts_repo.upsert_fact(conn, make_fact("f1", "更新后的内容", ["Java"]))
    assert facts_repo.get_fact(conn, "f1").content == "更新后的内容"
    assert len(facts_repo.list_facts(conn)) == 2


def test_disable_not_delete(clean):
    conn = clean
    facts_repo.upsert_fact(conn, make_fact("f1", "某事实", ["Java"]))
    assert facts_repo.disable_fact(conn, "f1") is True
    assert facts_repo.disable_fact(conn, "f_missing") is False

    assert facts_repo.list_facts(conn) == []
    # 物理记录仍在，引用快照可追溯
    assert facts_repo.list_facts(conn, enabled_only=False)[0].enabled is False


def test_fulltext_hits_chinese_terms(clean):
    conn = clean
    facts_repo.upsert_fact(conn, make_fact("f1", "使用 Java 和 MySQL 开发批量执行模块", ["Java", "MySQL"]))
    facts_repo.upsert_fact(conn, make_fact("f2", "前端页面联调", ["Vue"]))
    facts_repo.upsert_fact(conn, make_fact("f3", "Java 相关的另一段经历", ["Java"]))

    hits = search.fulltext_hits(conn, ["Java", "MySQL"])
    assert set(hits) == {"f1", "f3"}
    assert hits["f1"] == 1.0      # 覆盖 2/2 关键词
    assert hits["f3"] == 0.5      # 覆盖 1/2 关键词


def test_fulltext_excludes_disabled(clean):
    conn = clean
    facts_repo.upsert_fact(conn, make_fact("f1", "Java 开发", ["Java"], enabled=False))
    assert search.fulltext_hits(conn, ["Java"]) == {}


def test_vector_hits_by_cosine(clean):
    conn = clean
    dim = 512
    near = [1.0] + [0.0] * (dim - 1)
    far = [0.0, 1.0] + [0.0] * (dim - 2)
    facts_repo.upsert_fact(conn, make_fact("f1", "语义相近", ["Java"]), embedding=near)
    facts_repo.upsert_fact(conn, make_fact("f2", "语义相远", ["Vue"]), embedding=far)

    hits = search.vector_hits(conn, near)
    assert list(hits)[0] == "f1"
    assert hits["f1"] > hits["f2"]


def test_retriever_merges_fts_and_vector(clean):
    conn = clean
    dim = 512
    emb = [1.0] + [0.0] * (dim - 1)
    # f1 只被全文命中；f2 只被向量命中（内容不含关键词）
    facts_repo.upsert_fact(conn, make_fact("f1", "Java 批量执行", ["Java"]))
    facts_repo.upsert_fact(conn, make_fact("f2", "与关键词无关的内容", ["Spring Boot"]), embedding=emb)

    requirements = JobRequirements(
        job_title="Java 后端",
        keywords=[KeywordRequirement(term="Java", importance="required")],
    )

    class FixedEmbedding:
        def embed(self, text):
            return emb

    retriever = search.PostgresFactRetriever(conn, embedding_provider=FixedEmbedding())
    facts = retriever(requirements)
    ids = {f.id for f in facts}
    assert ids == {"f1", "f2"}


def test_duplicate_application_rejected(clean):
    """同一职位、简历版本和渠道不得重复创建有效投递（第 9 节）。"""
    conn = clean
    job = conn.execute(
        "INSERT INTO jobs (raw_text) VALUES ('某 JD') RETURNING id"
    ).fetchone()
    version = conn.execute(
        "INSERT INTO resume_versions (job_id, content) VALUES (%s, '{}') RETURNING id",
        (job["id"],),
    ).fetchone()

    insert = (
        "INSERT INTO applications (job_id, version_id, channel, idempotency_key) "
        "VALUES (%s, %s, 'web', %s)"
    )
    conn.execute(insert, (job["id"], version["id"], "key-1"))

    import psycopg.errors

    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(insert, (job["id"], version["id"], "key-2"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO applications (job_id, version_id, channel, idempotency_key) "
            "VALUES (%s, %s, 'mobile', %s)",
            (job["id"], version["id"], "key-1"),
        )
