"""T2: old workflow drafts must not authorize changed source facts."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

from applypilot import db, embeddings
from applypilot.api import create_app

pytestmark = pytest.mark.integration


class SyntheticModel:
    fact_id = ""

    def complete(self, system, user):
        if "职位描述解析器" in system:
            return json.dumps({
                "job_title": "Python Developer", "required": ["Python"],
                "keywords": [{"term": "Python", "importance": "required"}],
            })
        if "事实一致性复核员" in system:
            return '{"violations": []}'
        return json.dumps({"sections": {"experience": [{
            "text": "Developed a Python tool",
            "fact_ids": [self.fact_id],
            "matched_requirements": ["Python"],
        }]}})


@pytest.fixture(scope="module")
def runtime():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            db.init_schema(conn)
        model = SyntheticModel()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(embeddings, "LocalEmbeddingProvider", embeddings.NullEmbeddingProvider)
            with TestClient(create_app(dsn=dsn, adapter=model)) as client:
                yield client, model


@pytest.mark.parametrize("mutation", ["edit", "disable"])
@pytest.mark.parametrize("approved", [True, False])
def test_changed_source_blocks_stale_workflow(runtime, mutation, approved):
    client, model = runtime
    created = client.post("/api/facts", json={
        "fact_type": "project", "source_name": "Synthetic project",
        "content": "Developed a Python tool", "skills": ["Python"],
    })
    assert created.status_code == 201, created.text
    fact = created.json()
    model.fact_id = fact["id"]
    path = "/api/facts/" + fact["id"]
    response = client.post(path + "/confirm", json={"expected_revision": fact["revision"]})
    assert response.status_code == 200, response.text
    confirmed = response.json()
    job = client.post("/api/jobs", json={"title": "Python Developer", "company": "Example", "raw_text": "Python developer"}).json()
    response = client.post("/api/workflows", json={"job_id": job["id"]})
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = client.get("/api/workflows/" + run_id)
        if response.status_code == 200 and response.json()["status"] == "WAITING_APPROVAL":
            break
        if response.status_code == 200 and response.json()["status"] == "FAILED":
            pytest.fail(response.text)
        time.sleep(0.1)
    else:
        pytest.fail("workflow did not reach approval")

    change = {"content": "Reviewed a Python tool"} if mutation == "edit" else {"enabled": False}
    changed = client.put(path, json={"expected_revision": confirmed["revision"], **change})
    assert changed.status_code == 200, changed.text
    if mutation == "edit":
        response = client.post(path + "/confirm", json={"expected_revision": changed.json()["revision"]})
        assert response.status_code == 200, response.text
    response = client.post("/api/workflows/" + run_id + "/approve",
                           json={"approved": approved, "expected_revision": 1, "feedback": "Please revise"})
    assert response.status_code == 409, response.text
    assert client.get("/api/workflows/" + run_id).json()["status"] != "READY_TO_APPLY"
