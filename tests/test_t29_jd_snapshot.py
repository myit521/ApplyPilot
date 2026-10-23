"""A queued run must use its admitted JD, not later job edits."""

import json
import time
import uuid

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
import pytest
from testcontainers.postgres import PostgresContainer

from applypilot import db, embeddings, facts_repo, task_repo
from applypilot.api import create_app
from applypilot.schemas import Fact, FactType

pytestmark = pytest.mark.integration
PARSED = {"job_title": "合成 Java 岗位", "required": ["Java"], "preferred": [],
          "responsibilities": [], "keywords": [{"term": "Java", "importance": "required"}],
          "unknowns": []}


class Adapter:
    def __init__(self, fact_id):
        self.fact_id = fact_id
        self.parse_calls = 0
        self.generated_for = []

    def complete(self, system, user):
        if "职位描述解析器" in system:
            self.parse_calls += 1
            return json.dumps(PARSED)
        if "事实一致性复核员" in system:
            return '{"violations": []}'
        self.generated_for.append(user)
        return json.dumps({"sections": {"education": [], "skills": [], "experience": [
            {"text": "参与 Java 开发", "fact_ids": [self.fact_id]}]}})


@pytest.fixture(scope="module")
def database():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            db.init_schema(conn)
        yield dsn


@pytest.fixture
def case(database, monkeypatch):
    monkeypatch.setattr(embeddings, "LocalEmbeddingProvider", embeddings.NullEmbeddingProvider)
    fact_id = "t29_" + uuid.uuid4().hex
    with db.connect(database) as conn:
        facts_repo.create_fact(conn, Fact(id=fact_id, status="confirmed", fact_type=FactType.PROJECT,
            source_name="合成项目", content="参与 Java 开发", skills=["Java"]))
    return database, Adapter(fact_id)


def waiting(client, run_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = client.get(f"/api/workflows/{run_id}").json()
        if state["status"] in ("WAITING_APPROVAL", "FAILED"):
            assert state["status"] == "WAITING_APPROVAL", state
            return state
        time.sleep(.1)
    pytest.fail("synthetic workflow did not settle")


def test_queue_snapshot_survives_job_changes_and_is_frozen_at_approval(case):
    dsn, adapter = case
    with db.connect(dsn) as conn:
        job_id = conn.execute(
            "INSERT INTO jobs (source,company,title,raw_text,parsed) "
            "VALUES ('paste','原合成公司','原合成岗位','Java 原始 JD',%s) RETURNING id",
            (Jsonb(PARSED),)).fetchone()["id"]
        reserved = task_repo.reserve_workflow_task(conn, job_id=job_id, idempotency_key="t29-snapshot")
        original = task_repo.get_task(conn, reserved["run_id"])["job_snapshot"]
        assert original["raw_text"] == "Java 原始 JD"
        conn.execute("UPDATE jobs SET company='变更后公司',title='变更后岗位',raw_text='Python 新 JD',parsed=%s WHERE id=%s",
                     (Jsonb({**PARSED, "required": ["Python"]}), job_id))
        replay = task_repo.reserve_workflow_task(conn, job_id=job_id, idempotency_key="t29-snapshot")
        assert replay["run_id"] == reserved["run_id"]
        assert task_repo.get_task(conn, reserved["run_id"])["job_snapshot"] == original
    with TestClient(create_app(dsn=dsn, adapter=adapter)) as client:
        state = waiting(client, reserved["run_id"])
        assert adapter.parse_calls == 0
        assert "必备条件：Java" in adapter.generated_for[0]
        approved = client.post(f"/api/workflows/{reserved['run_id']}/approve", json={
            "approved": True, "expected_revision": state["draft_revision"]})
        assert approved.status_code == 200, approved.text
        with db.connect(dsn) as conn:
            content = conn.execute("SELECT content FROM resume_versions WHERE id=%s",
                (approved.json()["resume_version_id"],)).fetchone()["content"]
        assert content["job_snapshot"]["title"] == "原合成岗位"
        assert content["job_snapshot"]["company"] == "原合成公司"
        assert content["job_snapshot"]["raw_text"] == "Java 原始 JD"
        assert content["job_snapshot"]["parsed"]["required"] == ["Java"]
        page = client.get(f"/applications?version_id={approved.json()['resume_version_id']}")
        assert page.status_code == 200
        assert "原合成公司 · 原合成岗位" in page.text
        assert "变更后公司 · 变更后岗位" not in page.text


def test_explicit_parse_is_not_repeated_when_workflow_starts(case):
    dsn, adapter = case
    with TestClient(create_app(dsn=dsn, adapter=adapter)) as client:
        job = client.post("/api/jobs", json={"title": "合成岗位", "company": "合成公司",
                                           "raw_text": "招聘 Java 后端。"}).json()
        assert client.post(f"/api/jobs/{job['id']}/parse").status_code == 200
        run = client.post("/api/workflows", json={"job_id": job["id"]}).json()
        waiting(client, run["run_id"])
        assert adapter.parse_calls == 1


def test_unparsed_input_is_frozen_and_parsed_once(case):
    dsn, adapter = case
    with db.connect(dsn) as conn:
        job_id = conn.execute("INSERT INTO jobs (raw_text) VALUES ('招聘 Java 后端。') RETURNING id").fetchone()["id"]
        run = task_repo.reserve_workflow_task(conn, job_id=job_id)
        assert task_repo.get_task(conn, run["run_id"])["job_snapshot"]["parsed"] is None
    with TestClient(create_app(dsn=dsn, adapter=adapter)) as client:
        state = waiting(client, run["run_id"])
        assert adapter.parse_calls == 1
        approved = client.post(f"/api/workflows/{run['run_id']}/approve", json={
            "approved": True, "expected_revision": state["draft_revision"]})
        assert approved.status_code == 200
        with db.connect(dsn) as conn:
            content = conn.execute("SELECT content FROM resume_versions WHERE id=%s",
                (approved.json()["resume_version_id"],)).fetchone()["content"]
        assert content["job_snapshot"]["parsed"]["required"] == ["Java"]


def test_legacy_task_has_no_fabricated_reservation_snapshot(case):
    dsn, adapter = case
    run_id = "wf_t29_legacy"
    with db.connect(dsn) as conn:
        job_id = conn.execute("INSERT INTO jobs (raw_text) VALUES ('招聘 Java 后端。') RETURNING id").fetchone()["id"]
        conn.execute("INSERT INTO workflow_runs (id,current_node,status) VALUES (%s,'queued','queued')", (run_id,))
        conn.execute("INSERT INTO workflow_tasks (run_id,job_id) VALUES (%s,%s)", (run_id, job_id))
        db.migrate(conn)
        db.migrate(conn)
        assert task_repo.get_task(conn, run_id)["job_snapshot"] is None
    with TestClient(create_app(dsn=dsn, adapter=adapter)) as client:
        waiting(client, run_id)
        assert adapter.parse_calls == 1
    with db.connect(dsn) as conn:
        assert task_repo.get_task(conn, run_id)["job_snapshot"] is None
