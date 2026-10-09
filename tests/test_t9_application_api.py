"""Manual application records stay separate from approval and site confirmation."""

import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

from applypilot import db
from applypilot.api import create_app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def case():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            db.init_schema(conn)
            job_id = conn.execute(
                "INSERT INTO jobs (company, title, raw_text) "
                "VALUES ('Example', 'Java 后端', 'JD') RETURNING id"
            ).fetchone()["id"]
            version_id = conn.execute(
                "INSERT INTO resume_versions (job_id, content, status) "
                "VALUES (%s, '{}'::jsonb, 'approved') RETURNING id", (job_id,)
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO workflow_runs (id, current_node, status) "
                "VALUES ('t9_api_run', 'approval', 'READY_TO_APPLY')"
            )
            conn.execute(
                "INSERT INTO workflow_approvals "
                "(run_id, draft_revision, content_sha256, version_id) "
                "VALUES ('t9_api_run', 1, %s, %s)", ("a" * 64, version_id)
            )
            unapproved_id = conn.execute(
                "INSERT INTO resume_versions (job_id, content, status) "
                "VALUES (%s, '{}'::jsonb, 'approved') RETURNING id", (job_id,)
            ).fetchone()["id"]
        with TestClient(create_app(dsn=dsn, checkpointer=object())) as client:
            yield client, dsn, job_id, version_id, unapproved_id


def test_manual_record_create_update_and_persist(case):
    client, dsn, job_id, version_id, _ = case
    body = {"job_id": job_id, "version_id": version_id, "channel": "官网",
            "status": "unknown", "occurred_at": None, "result": "尚无回执"}
    headers = {"Idempotency-Key": "t9-api-create-1"}
    created = client.post("/api/applications", json=body, headers=headers)
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["source"] == "user_reported"
    assert record["status"] == "unknown"
    assert client.post("/api/applications", json=body, headers=headers).json()["id"] == record["id"]
    assert client.post("/api/applications", json={**body, "result": "不同备注"},
                       headers=headers).status_code == 409
    assert client.post("/api/applications", json=body,
                       headers={"Idempotency-Key": "t9-api-create-2"}).status_code == 409

    updated = client.patch(f"/api/applications/{record['id']}", json={
        "expected_revision": record["revision"], "status": "submitted",
        "occurred_at": "2026-10-09T10:00:00+08:00", "result": "用户自述已提交",
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == record["revision"] + 1
    assert updated.json()["status"] == "submitted"
    assert client.patch(f"/api/applications/{record['id']}", json={
        "expected_revision": record["revision"], "status": "failed", "result": "过期修改",
    }).status_code == 409
    assert client.get(f"/api/applications/{record['id']}").json()["source"] == "user_reported"
    assert any(row["id"] == record["id"] for row in client.get("/api/applications").json())
    with db.connect(dsn) as conn:
        assert conn.execute("SELECT status FROM applications WHERE id=%s", (record["id"],)).fetchone()["status"] == "submitted"
        assert conn.execute(
            "SELECT count(*) AS n FROM audit_events "
            "WHERE event_type IN ('application.created', 'application.updated') "
            "AND payload->>'application_id'=%s", (str(record["id"]),)
        ).fetchone()["n"] == 2
    partial = client.patch(f"/api/applications/{record['id']}", json={
        "expected_revision": updated.json()["revision"], "status": "failed",
    })
    assert partial.status_code == 200, partial.text
    assert partial.json()["result"] == "用户自述已提交"
    assert partial.json()["occurred_at"] == updated.json()["occurred_at"]


def test_manual_record_boundaries(case):
    client, _, job_id, version_id, unapproved_id = case
    base = {"job_id": job_id, "version_id": unapproved_id, "channel": "邮箱",
            "status": "unknown", "occurred_at": None, "result": ""}
    assert client.post("/api/applications", json=base,
                       headers={"Idempotency-Key": "t9-unapproved"}).status_code == 409
    assert client.post("/api/applications", json={**base, "version_id": version_id,
                       "status": "site_confirmed"},
                       headers={"Idempotency-Key": "t9-invalid"}).status_code == 422
    assert client.post("/api/applications", json={**base, "version_id": version_id,
                       "source": "site_confirmed"},
                       headers={"Idempotency-Key": "t9-source-injection"}).status_code == 422
    assert client.post("/api/applications", json={**base, "version_id": version_id}).status_code == 422
    assert client.patch("/api/applications/1", json={
        "expected_revision": 1, "status": None,
    }).status_code == 422
    assert client.get("/api/applications/99999999").status_code == 404
    page = client.get(f"/applications?version_id={version_id}")
    assert page.status_code == 200
    assert "用户自述" in page.text
    assert f'value="{version_id}"' in page.text
