"""T8 worker restart and bounded retry with a real PostgreSQL checkpointer."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
import psycopg
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

from applypilot import db, embeddings, facts_repo, task_repo
from applypilot.api import create_app
from applypilot.task_worker import TaskWorker
from applypilot.model_adapter import RetryableModelError
from applypilot.schemas import Fact, FactType

pytestmark = pytest.mark.integration

JD_RESPONSE = json.dumps({
    "job_title": "Java 后端", "required": ["Java"], "preferred": [],
    "responsibilities": [],
    "keywords": [{"term": "Java", "importance": "required"}],
    "unknowns": [],
})
RESUME_RESPONSE = json.dumps({"sections": {
    "education": [], "skills": [],
    "experience": [{"text": "参与 Java 开发", "fact_ids": ["t8_fact"],
                    "matched_requirements": ["Java"]}],
}})


class StableAdapter:
    def __init__(self):
        self.parse_calls = 0

    def complete(self, system: str, user: str) -> str:
        if "职位描述解析器" in system:
            self.parse_calls += 1
            return JD_RESPONSE
        if "事实一致性复核员" in system:
            return '{"violations": []}'
        return RESUME_RESPONSE


class FailGenerateAdapter(StableAdapter):
    def complete(self, system: str, user: str) -> str:
        if "职位描述解析器" in system:
            return super().complete(system, user)
        raise RetryableModelError("synthetic generation timeout")


class FailParseAdapter(StableAdapter):
    def complete(self, system: str, user: str) -> str:
        raise RetryableModelError("synthetic parse timeout")


@pytest.fixture
def task_database(monkeypatch):
    monkeypatch.setattr(embeddings, "LocalEmbeddingProvider", embeddings.NullEmbeddingProvider)
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            db.init_schema(conn)
            facts_repo.create_fact(conn, Fact(
                id="t8_fact", status="confirmed", fact_type=FactType.INTERNSHIP,
                source_name="合成测试", content="参与 Java 开发", skills=["Java"],
            ))
            job_id = conn.execute(
                "INSERT INTO jobs (raw_text) VALUES ('招聘 Java 后端') RETURNING id"
            ).fetchone()["id"]
        yield dsn, job_id


def wait_task(dsn: str, run_id: str, expected: str, timeout: float = 12) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db.connect(dsn) as conn:
            task = task_repo.get_task(conn, run_id)
        if task["status"] == expected:
            return task
        time.sleep(0.1)
    raise AssertionError(f"task did not reach {expected}: {task['status']}")


def test_startup_reclaims_running_task_without_checkpoint(task_database):
    dsn, job_id = task_database
    with db.connect(dsn) as conn:
        run_id = task_repo.reserve_workflow_task(conn, job_id=job_id)["run_id"]
        conn.execute(
            "UPDATE workflow_tasks SET status='running', attempt_count=1 WHERE run_id=%s",
            (run_id,),
        )
    with TestClient(create_app(dsn=dsn, adapter=StableAdapter())):
        task = wait_task(dsn, run_id, "waiting_approval")
        assert task["attempt_count"] == 2


def test_transient_failure_resumes_from_checkpoint_after_restart(task_database):
    dsn, job_id = task_database
    with db.connect(dsn) as conn:
        run_id = task_repo.reserve_workflow_task(conn, job_id=job_id)["run_id"]
    first_adapter = FailGenerateAdapter()
    with TestClient(create_app(dsn=dsn, adapter=first_adapter)):
        task = wait_task(dsn, run_id, "retry_wait")
        assert task["attempt_count"] == 1
        assert first_adapter.parse_calls == 1

    with db.connect(dsn) as conn:
        conn.execute(
            "UPDATE workflow_tasks SET next_attempt_at=now() WHERE run_id=%s", (run_id,)
        )
    second_adapter = StableAdapter()
    with TestClient(create_app(dsn=dsn, adapter=second_adapter)):
        task = wait_task(dsn, run_id, "waiting_approval")
        assert task["attempt_count"] == 2
        assert second_adapter.parse_calls == 0


def test_transient_failure_has_finite_retry_and_safe_error(task_database):
    dsn, job_id = task_database
    with db.connect(dsn) as conn:
        run_id = task_repo.reserve_workflow_task(conn, job_id=job_id)["run_id"]
    with TestClient(create_app(dsn=dsn, adapter=FailParseAdapter())):
        task = wait_task(dsn, run_id, "failed")
        assert task["attempt_count"] == 3
        assert task["error"] == "model_retry_limit_reached"
        assert "synthetic" not in task["error"]


def test_settlement_failure_recovers_without_killing_worker(task_database, monkeypatch):
    dsn, job_id = task_database
    with db.connect(dsn) as conn:
        run_id = task_repo.reserve_workflow_task(conn, job_id=job_id)["run_id"]

    original_settle = TaskWorker._settle
    failures = []

    def fail_twice(self, *args, **kwargs):
        if len(failures) < 2:
            failures.append(args[1])
            raise psycopg.OperationalError("injected settlement outage")
        return original_settle(self, *args, **kwargs)

    monkeypatch.setattr(TaskWorker, "_settle", fail_twice)
    with TestClient(create_app(dsn=dsn, adapter=StableAdapter())):
        task = wait_task(dsn, run_id, "waiting_approval")
        assert failures == ["waiting_approval", "failed"]
        assert task["attempt_count"] == 2


def test_restart_reconciles_approved_task_without_second_version(task_database):
    dsn, job_id = task_database
    with db.connect(dsn) as conn:
        run_id = task_repo.reserve_workflow_task(conn, job_id=job_id)["run_id"]
    with TestClient(create_app(dsn=dsn, adapter=StableAdapter())) as client:
        wait_task(dsn, run_id, "waiting_approval")
        approved = client.post(
            f"/api/workflows/{run_id}/approve",
            json={"approved": True, "expected_revision": 1},
        )
        assert approved.status_code == 200, approved.text
        version_id = approved.json()["resume_version_id"]

    # Simulate a crash after T7 committed but before task status was reconciled.
    with db.connect(dsn) as conn:
        conn.execute(
            "UPDATE workflow_tasks SET status='running', attempt_count=1 WHERE run_id=%s",
            (run_id,),
        )
    with TestClient(create_app(dsn=dsn, adapter=StableAdapter())) as client:
        task = wait_task(dsn, run_id, "completed")
        assert task["attempt_count"] == 1
        summary = client.get(f"/api/workflows/{run_id}")
        assert summary.json()["resume_version_id"] == version_id
    with db.connect(dsn) as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM resume_versions WHERE job_id=%s", (job_id,)
        ).fetchone()["n"] == 1


def test_killed_process_reclaims_claimed_task(task_database):
    dsn, job_id = task_database
    with db.connect(dsn) as conn:
        run_id = task_repo.reserve_workflow_task(conn, job_id=job_id)["run_id"]

    child_code = """
import sys, time
from fastapi.testclient import TestClient
from applypilot.api import create_app
class SlowAdapter:
    def complete(self, system, user):
        time.sleep(30)
        raise RuntimeError('the child should be killed before the model returns')
with TestClient(create_app(dsn=sys.argv[1], adapter=SlowAdapter())):
    time.sleep(30)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, dsn],
        cwd=Path(__file__).resolve().parents[1], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        claimed = wait_task(dsn, run_id, "running", timeout=15)
        assert claimed["attempt_count"] == 1
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    with TestClient(create_app(dsn=dsn, adapter=StableAdapter())):
        task = wait_task(dsn, run_id, "waiting_approval")
        assert task["attempt_count"] == 2
    with db.connect(dsn) as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM workflow_tasks WHERE run_id=%s", (run_id,)
        ).fetchone()["n"] == 1
