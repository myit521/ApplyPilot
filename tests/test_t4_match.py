"""Evidence-focused matching: deterministic statuses and cited confirmed facts."""
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from testcontainers.postgres import PostgresContainer

from applypilot import db
from applypilot.api import create_app
from applypilot.matching import build_match_report
from applypilot.schemas import JobRequirements, KeywordImportance, KeywordRequirement


def requirements():
    return JobRequirements(
        required=["熟悉 Java、Redis 和 MySQL"],
        preferred=["了解 Kubernetes"],
        responsibilities=["参与 Java 服务开发"],
        keywords=[
            KeywordRequirement(term="Java", importance=KeywordImportance.REQUIRED),
            KeywordRequirement(term="Redis", importance=KeywordImportance.REQUIRED),
            KeywordRequirement(term="MySQL", importance=KeywordImportance.REQUIRED),
            KeywordRequirement(term="Kubernetes", importance=KeywordImportance.PREFERRED),
        ],
        unknowns=["团队规模无法从职位描述确定"],
    )


@pytest.fixture(scope="module")
def database():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            db.init_schema(conn)
        yield dsn


@pytest.fixture
def client(database):
    with db.connect(database) as conn:
        conn.execute("TRUNCATE jobs CASCADE")
        conn.execute("TRUNCATE facts CASCADE")
    with TestClient(create_app(dsn=database, checkpointer=MemorySaver())) as c:
        yield c


@pytest.mark.integration
def test_match_endpoint_returns_requirement_evidence_and_default_skips_vectors(client, database, monkeypatch):
    job = client.post("/api/jobs", json={
        "title": "Java 后端", "company": "演示公司", "raw_text": "JD",
    }).json()
    with db.connect(database) as conn:
        conn.execute("UPDATE jobs SET parsed=%s WHERE id=%s", (requirements().model_dump_json(), job["id"]))
    saved = client.post("/api/facts", json={
        "fact_type": "project", "source_name": "演示项目",
        "content": "开发 Java 服务并使用 Redis", "skills": ["Java", "Redis"],
    }).json()
    client.post(f"/api/facts/{saved['id']}/confirm", json={"expected_revision": saved["revision"]})
    js_fact = client.post("/api/facts", json={
        "fact_type": "project", "source_name": "前端项目",
        "content": "实现前端能力", "skills": ["JavaScript"],
    }).json()
    client.post(f"/api/facts/{js_fact['id']}/confirm", json={"expected_revision": js_fact["revision"]})
    draft = client.post("/api/facts", json={
        "fact_type": "project", "source_name": "草稿项目",
        "content": "使用 MySQL", "skills": ["MySQL"],
    }).json()
    disabled = client.post("/api/facts", json={
        "fact_type": "project", "source_name": "停用项目",
        "content": "使用 Kubernetes", "skills": ["Kubernetes"],
    }).json()
    confirmed_disabled = client.post(f"/api/facts/{disabled['id']}/confirm", json={"expected_revision": disabled["revision"]}).json()
    client.put(f"/api/facts/{disabled['id']}", json={"expected_revision": confirmed_disabled["revision"], "enabled": False})
    from applypilot import embeddings
    def forbidden_provider():
        raise AssertionError("keyword report should not load vector embeddings by default")
    monkeypatch.setattr(embeddings, "LocalEmbeddingProvider", forbidden_provider)
    response = client.get(f"/api/jobs/{job['id']}/match")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["semantic_candidates"] == []
    assert result["requirements"][0]["status"] == "partial"
    required = result["requirements"][0]
    assert required["evidence"][0]["fact_id"] == saved["id"]
    assert required["matched_terms"] == ["Java", "Redis"]
    assert draft["id"] not in {e["fact_id"] for e in required["evidence"]}
    assert js_fact["id"] not in {e["fact_id"] for e in required["evidence"]}
    assert disabled["id"] not in {e["fact_id"] for e in result["requirements"][1]["evidence"]}
    assert result["requirements"][1]["status"] == "no_evidence"
    assert result["requirements"][2]["status"] == "supported"
    assert result["unknowns"][0]["status"] == "unknown"
    assert client.get("/api/jobs/999999/match").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}/match?include_semantic=false").status_code == 200


@pytest.mark.integration
def test_match_requires_successful_jd_parse(client):
    job = client.post("/api/jobs", json={
        "title": "未解析", "company": "演示公司", "raw_text": "JD",
    }).json()
    response = client.get(f"/api/jobs/{job['id']}/match")
    assert response.status_code == 409
    assert "解析" in response.json()["detail"]
    assert client.get(f"/api/jobs/{job['id']}/match?include_semantic=perhaps").status_code == 422


@pytest.mark.integration
def test_semantic_candidates_never_upgrade_requirement_status(client, database, monkeypatch):
    from applypilot import embeddings

    job = client.post("/api/jobs", json={
        "title": "Kubernetes 工程师", "company": "演示公司", "raw_text": "JD",
    }).json()
    req = JobRequirements(
        required=["熟悉 Kubernetes"],
        keywords=[KeywordRequirement(term="Kubernetes", importance=KeywordImportance.REQUIRED)],
    )
    with db.connect(database) as conn:
        conn.execute("UPDATE jobs SET parsed=%s WHERE id=%s", (req.model_dump_json(), job["id"]))
    saved = client.post("/api/facts", json={
        "fact_type": "project", "source_name": "相似项目",
        "content": "设计分布式服务", "skills": ["Distributed Systems"],
    }).json()
    client.post(f"/api/facts/{saved['id']}/confirm", json={"expected_revision": saved["revision"]})

    class FakeEmbeddings:
        def embed(self, text):
            return [0.01] * 512

    monkeypatch.setattr(embeddings, "LocalEmbeddingProvider", FakeEmbeddings)
    result = client.get(f"/api/jobs/{job['id']}/match?include_semantic=true").json()
    assert result["requirements"][0]["status"] == "no_evidence"
    assert result["requirements"][0]["evidence"] == []
    assert result["semantic_candidates"][0]["fact"]["id"] == saved["id"]


def test_requirement_without_extracted_keywords_is_unknown_not_no_evidence():
    req = JobRequirements(required=["具备跨团队协作经验"])
    report = build_match_report(req, [])
    item = report["requirements"][0]
    assert item["status"] == "unknown"
    assert "可逐项匹配" in item["reason"]
    assert item["evidence"] == []


def test_evidence_excerpt_contains_keyword_after_first_500_characters():
    from applypilot.schemas import Fact

    req = JobRequirements(
        required=["熟悉 Kubernetes"],
        keywords=[KeywordRequirement(term="Kubernetes", importance=KeywordImportance.REQUIRED)],
    )
    fact = Fact(
        id="long-fact", status="confirmed", fact_type="project", source_name="演示项目",
        content="前文。" * 350 + "在项目中部署 Kubernetes 服务。",
    )
    item = build_match_report(req, [fact])["requirements"][0]
    assert item["status"] == "supported"
    assert "Kubernetes" in item["evidence"][0]["excerpt"]

