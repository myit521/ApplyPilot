"""T3 contracts: validation, durable manual jobs and explicit model parsing."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from pydantic import ValidationError
from testcontainers.postgres import PostgresContainer

from applypilot import db
from applypilot.api import JobCreateRequest, create_app
from applypilot.model_adapter import ModelError

PAYLOAD = {"title": "Java 后端", "company": "模拟公司", "raw_text": "招聘 Java"}

@pytest.mark.parametrize("field,limit", [("title", 200), ("company", 200), ("source", 100), ("raw_text", 30000)])
def test_required_text_bounds(field, limit):
    for value in ("", " \t\n", None, 12, [], "x" * (limit + 1), "x\x00y"):
        with pytest.raises(ValidationError):
            JobCreateRequest.model_validate({**PAYLOAD, field: value})
    assert getattr(JobCreateRequest.model_validate({**PAYLOAD, field: "x" * limit}), field) == "x" * limit
    if field != "source":
        with pytest.raises(ValidationError):
            JobCreateRequest.model_validate({k: v for k, v in PAYLOAD.items() if k != field})

@pytest.mark.parametrize("url", ["ftp://example.invalid", "https://", "//example.invalid", "https://u:p@example.invalid", "https://u@example.invalid", "https://example.invalid/a b", "https://example.invalid/\n", "https://example.invalid/\x00", "https://example.invalid/\x7f", "https://example.invalid/\x80", "https://example.invalid/" + "x" * 2048, 3, []])
def test_invalid_source_url(url):
    with pytest.raises(ValidationError):
        JobCreateRequest.model_validate({**PAYLOAD, "url": url})

@pytest.mark.parametrize("url", [None, "", " \t"])
def test_blank_url_normalizes_to_none(url):
    assert JobCreateRequest.model_validate({**PAYLOAD, "url": url}).url is None

@pytest.mark.parametrize("url", ["http://example.invalid/jobs?id=1", "https://example.invalid/招聘", "https://[::1]:8443/jobs"])
def test_valid_url_is_informational(url):
    assert JobCreateRequest.model_validate({**PAYLOAD, "url": url}).url == url

def test_normalization_preserves_raw_text_and_forbids_extra():
    request = JobCreateRequest.model_validate({**PAYLOAD, "title": " Java ", "company": " 公司 ", "source": " manual ", "raw_text": " \nJD\t "})
    assert (request.title, request.company, request.source, request.raw_text) == ("Java", "公司", "manual", " \nJD\t ")
    assert JobCreateRequest.model_validate(PAYLOAD).source == "paste"
    with pytest.raises(ValidationError):
        JobCreateRequest.model_validate({**PAYLOAD, "parsed": {}})

@pytest.fixture(scope="module")
def database():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            db.init_schema(conn)
        yield dsn

@pytest.fixture
def client(database, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with db.connect(database) as conn:
        conn.execute("TRUNCATE jobs RESTART IDENTITY CASCADE")
    with TestClient(create_app(dsn=database, checkpointer=MemorySaver())) as client:
        yield client

@pytest.mark.integration
def test_save_without_model_and_persist_across_new_app(client, database):
    raw = Path("tests/fixtures/java_backend_jd.txt").read_text(encoding="utf-8")
    response = client.post("/api/jobs", json={**PAYLOAD, "raw_text": raw, "url": "https://example.invalid/job"})
    assert response.status_code == 201, response.text
    job = response.json()
    assert job == {"id": 1, "title": "Java 后端", "company": "模拟公司", "source": "paste", "url": "https://example.invalid/job", "raw_text": raw, "parsed": None, "created_at": job["created_at"]}
    with TestClient(create_app(dsn=database, checkpointer=MemorySaver())) as fresh:
        assert fresh.get("/api/jobs/1").json() == job
    assert client.post("/api/jobs/1/parse").status_code == 503
    assert client.get("/api/jobs/1").json() == job
    with db.connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM workflow_runs").fetchone()["n"] == 0

@pytest.mark.integration
@pytest.mark.parametrize("changes", [{"title": " "}, {"company": None}, {"source": ""}, {"raw_text": ""}, {"raw_text": "a\x00b"}, {"url": "javascript:alert(1)"}, {"parsed": {}}])
def test_api_rejects_invalid_without_inserting(client, changes):
    assert client.post("/api/jobs", json={**PAYLOAD, **changes}).status_code == 422
    assert client.get("/api/jobs").json()["total"] == 0

@pytest.mark.integration
def test_history_bounded_stable_and_legacy_readable(client, database):
    ids = [client.post("/api/jobs", json={**PAYLOAD, "raw_text": Path("tests/fixtures/ai_application_jd.txt").read_text(encoding="utf-8")}).json()["id"] for _ in range(5)]
    page = client.get("/api/jobs?limit=2&offset=0").json()
    assert page["total"] == 5 and page["limit"] == 2 and page["offset"] == 0
    assert [j["id"] for j in page["items"]] == ids[::-1][:2]
    assert client.get("/api/jobs?limit=2").json() == page
    assert [j["id"] for j in client.get("/api/jobs?limit=2&offset=2").json()["items"]] == ids[::-1][2:4]
    assert client.get("/api/jobs?offset=99").json()["items"] == []
    assert client.get("/api/jobs").json()["limit"] == 20
    assert client.get("/api/jobs?limit=100").status_code == 200
    for query in ("limit=0", "limit=101", "offset=-1", "limit=x"):
        assert client.get("/api/jobs?" + query).status_code == 422
    with db.connect(database) as conn:
        legacy = conn.execute("INSERT INTO jobs(raw_text) VALUES ('legacy JD') RETURNING id").fetchone()["id"]
    assert client.get(f"/api/jobs/{legacy}").json()["title"] == ""
    assert client.get("/api/jobs/999999").status_code == 404
    assert client.post("/api/jobs/999999/parse").status_code == 404

@pytest.mark.integration
def test_parse_preserves_user_title_and_failed_retry_retains_saved_data(client, database):
    job = client.post("/api/jobs", json=PAYLOAD).json()
    class Adapter:
        fail = False
        invalid = False
        def complete(self, system, user):
            if self.fail:
                raise ModelError("secret-provider-key")
            return "invalid secret-provider-key" if self.invalid else json.dumps({"job_title": "model title", "required": ["Java"]})
    adapter = Adapter()
    with TestClient(create_app(dsn=database, adapter=adapter, checkpointer=MemorySaver())) as parsing:
        response = parsing.post(f"/api/jobs/{job['id']}/parse")
        assert response.status_code == 200, response.text
        parsed = response.json()
        assert parsed["title"] == "Java 后端" and parsed["parsed"]["job_title"] == "model title"
        for field in ("fail", "invalid"):
            setattr(adapter, field, True)
            response = parsing.post(f"/api/jobs/{job['id']}/parse")
            assert response.status_code in (422, 502) and "secret-provider-key" not in response.text
            assert parsing.get(f"/api/jobs/{job['id']}").json() == parsed
            setattr(adapter, field, False)

@pytest.mark.integration
def test_job_pages_and_html_injection_are_inert(client):
    assert client.get("/jobs").status_code == 200
    assert 'href="/jobs"' in client.get("/").text
    attack = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    job = client.post("/api/jobs", json={**PAYLOAD, "title": attack, "company": attack, "source": attack, "raw_text": attack}).json()
    page = client.get("/").text
    assert attack not in page and "&lt;script&gt;" in page
    assert client.get(f"/api/jobs/{job['id']}").json()["raw_text"] == attack

@pytest.mark.integration
def test_api_validation_boundaries_and_strict_types(client):
    for field, maximum in (("title", 200), ("company", 200), ("source", 100), ("raw_text", 30000)):
        for value in (" ", None, 123, [], "x" * (maximum + 1), "a\x00b"):
            assert client.post("/api/jobs", json={**PAYLOAD, field: value}).status_code == 422
        if field != "source":
            assert client.post("/api/jobs", json={key: value for key, value in PAYLOAD.items() if key != field}).status_code == 422
    assert client.get("/api/jobs").json()["total"] == 0
    prefix = "https://example.invalid/"
    url = prefix + "a" * (2048 - len(prefix))
    accepted = client.post("/api/jobs", json={"title": "t" * 200, "company": "c" * 200, "source": "s" * 100, "raw_text": "r" * 30000, "url": url})
    assert accepted.status_code == 201 and accepted.json()["url"] == url
    assert client.post("/api/jobs", json={**PAYLOAD, "url": url + "a"}).status_code == 422
