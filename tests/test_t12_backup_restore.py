"""A synthetic backup must restore into a separate PostgreSQL instance."""

import subprocess

import pytest
from testcontainers.postgres import PostgresContainer

from applypilot import db, facts_repo
from applypilot.approvals_repo import get_approval, persist_approval
from applypilot.schemas import Fact, FactType

pytestmark = pytest.mark.integration


def _dsn(container):
    return container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


def _docker(*args):
    subprocess.run(["docker", *args], check=True, capture_output=True, text=True)


def _snapshot(conn):
    approval = get_approval(conn, "wf_backup_synthetic")
    application = conn.execute(
        "SELECT version_id, channel, status, source FROM applications "
        "WHERE idempotency_key='backup-synthetic'"
    ).fetchone()
    return {
        "schema_ready": db.schema_ready(conn),
        "migrations": [row["version"] for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()],
        "approval_hash": approval["content_sha256"],
        "approval_package": approval["package"],
        "version_id": approval["version_id"],
        "fact_snapshot_count": conn.execute(
            "SELECT count(*) AS n FROM resume_version_facts WHERE version_id=%s",
            (approval["version_id"],),
        ).fetchone()["n"],
        "application": application,
    }


def test_pg_dump_restores_approved_version_fact_snapshot_and_manual_record(tmp_path):
    with PostgresContainer("pgvector/pgvector:pg16") as source, \
            PostgresContainer("pgvector/pgvector:pg16") as destination:
        fact = Fact(
            id="backup-fact", status="confirmed", fact_type=FactType.PROJECT,
            source_name="合成测试", content="使用 Java 实现 6 个接口",
            skills=["Java"], metrics=["6 个接口"],
        )
        with db.connect(_dsn(source)) as conn:
            db.init_schema(conn)
            facts_repo.create_fact(conn, fact)
            job_id = conn.execute(
                "INSERT INTO jobs (title, raw_text) VALUES ('Java 后端', '合成 JD') RETURNING id"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO workflow_runs (id, current_node, status) "
                "VALUES ('wf_backup_synthetic', 'approval', 'WAITING_APPROVAL')"
            )
            saved = persist_approval(
                conn, run_id="wf_backup_synthetic", draft_revision=1, job_id=job_id,
                sections={"education": [], "skills": [], "experience": [{
                    "text": "使用 Java 实现 6 个接口", "fact_ids": [fact.id],
                    "matched_requirements": ["Java"],
                }]}, retrieved_facts=[fact],
            )
            conn.execute(
                "INSERT INTO applications (job_id, version_id, channel, idempotency_key, "
                "status, source) VALUES (%s, %s, '官网', 'backup-synthetic', "
                "'unknown', 'user_reported')",
                (job_id, saved["version_id"]),
            )
            before = _snapshot(conn)

        source_id = source.get_wrapped_container().id
        destination_id = destination.get_wrapped_container().id
        backup = tmp_path / "synthetic.dump"
        _docker("exec", source_id, "pg_dump", "-U", source.username, "-d", source.dbname,
                "--format=custom", "--file=/tmp/synthetic.dump")
        _docker("cp", f"{source_id}:/tmp/synthetic.dump", str(backup))
        assert backup.stat().st_size > 0
        _docker("cp", str(backup), f"{destination_id}:/tmp/synthetic.dump")
        _docker("exec", destination_id, "pg_restore", "-U", destination.username,
                "-d", destination.dbname, "--no-owner", "--no-acl", "/tmp/synthetic.dump")

        with db.connect(_dsn(destination)) as conn:
            assert _snapshot(conn) == before
            db.init_schema(conn)
            assert _snapshot(conn) == before
