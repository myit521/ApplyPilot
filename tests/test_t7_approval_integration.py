"""PostgreSQL integration coverage for atomic approval snapshots."""

import pytest
import psycopg
from testcontainers.postgres import PostgresContainer

from applypilot import db, facts_repo
from applypilot.approval_snapshots import hash_approval_package
from applypilot.approvals_repo import ApprovalConflict, get_approval, persist_approval
from applypilot.schemas import Fact, FactType

pytestmark = pytest.mark.integration


def test_t7_migration_preserves_legacy_versions_and_is_idempotent():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            conn.execute(db.SCHEMA_PATH.read_text(encoding="utf-8"))
            job = conn.execute(
                "INSERT INTO jobs (raw_text) VALUES ('legacy JD') RETURNING id"
            ).fetchone()
            version = conn.execute(
                "INSERT INTO resume_versions (job_id, content) VALUES (%s, %s::jsonb) RETURNING id",
                (job["id"], '{"sections":{"experience":[]}}'),
            ).fetchone()
            db.migrate(conn)
            db.migrate(conn)
            saved = conn.execute(
                "SELECT job_id, content FROM resume_versions WHERE id=%s",
                (version["id"],),
            ).fetchone()
            assert saved == {
                "job_id": job["id"],
                "content": {"sections": {"experience": []}},
            }
            assert conn.execute(
                "SELECT version FROM schema_migrations "
                "WHERE version='003_atomic_approval_snapshot.sql'"
            ).fetchone()
            assert db.schema_ready(conn)


@pytest.fixture(scope="module")
def approval_dsn():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            db.init_schema(conn)
        yield dsn


@pytest.fixture
def approval_case(approval_dsn):
    with db.connect(approval_dsn) as conn:
        conn.execute(
            "TRUNCATE workflow_approvals, resume_version_facts, resume_claims, "
            "resume_versions, jobs, workflow_runs, audit_events, fact_revisions, facts CASCADE"
        )
        job = conn.execute(
            "INSERT INTO jobs (source, company, title, raw_text) "
            "VALUES ('paste', 'T7 fixture', 'Java 后端', 'Java 后端工程师') RETURNING id"
        ).fetchone()
        f1 = Fact(
            id="f1", fact_type=FactType.PROJECT, source_name="T7 fixture",
            content="Built batch API", skills=["Java"], metrics=["6 个批量接口"],
            status="confirmed", origin="human",
        )
        f2 = Fact(
            id="f2", fact_type=FactType.PROJECT, source_name="T7 fixture",
            content="Uncited work", skills=["Java"], status="confirmed", origin="human",
        )
        facts_repo.create_fact(conn, f1)
        facts_repo.create_fact(conn, f2)
        run_id = "wf_t7_repository"
        conn.execute(
            "INSERT INTO workflow_runs (id, current_node, status) "
            "VALUES (%s, 'approval', 'WAITING_APPROVAL')",
            (run_id,),
        )
        sections = {
            "education": [],
            "skills": [],
            "experience": [{
                "text": "Built batch API",
                "fact_ids": ["f1"],
                "matched_requirements": ["Java"],
            }],
        }
        yield conn, run_id, job["id"], sections, [f1, f2]


def test_persist_approval_writes_version_claims_facts_and_one_event(approval_case):
    conn, run_id, job_id, sections, retrieved_facts = approval_case
    record = persist_approval(
        conn, run_id=run_id, draft_revision=2, job_id=job_id,
        sections=sections, retrieved_facts=retrieved_facts,
    )
    assert record["draft_revision"] == 2
    assert len(record["content_sha256"]) == 64
    assert conn.execute("SELECT count(*) AS n FROM resume_claims WHERE version_id=%s",
                        (record["version_id"],)).fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM resume_version_facts WHERE version_id=%s",
                        (record["version_id"],)).fetchone()["n"] == 1
    saved_content = conn.execute("SELECT content FROM resume_versions WHERE id=%s",
                                  (record["version_id"],)).fetchone()["content"]
    assert "facts" not in saved_content
    saved = get_approval(conn, run_id)
    assert saved["package"] == record["package"]
    assert hash_approval_package(saved["package"]) == record["content_sha256"]
    assert conn.execute(
        "SELECT count(*) AS n FROM audit_events WHERE event_type='resume.approved' "
        "AND payload->>'run_id'=%s", (run_id,)
    ).fetchone()["n"] == 1


def test_changed_unreferenced_fact_does_not_block_approval(approval_case):
    conn, run_id, job_id, sections, retrieved_facts = approval_case
    facts_repo.update_fact(conn, "f2", 1, {"content": "Changed but not cited"})
    record = persist_approval(
        conn, run_id=run_id, draft_revision=2, job_id=job_id,
        sections=sections, retrieved_facts=retrieved_facts,
    )
    assert conn.execute("SELECT count(*) AS n FROM resume_version_facts WHERE version_id=%s",
                        (record["version_id"],)).fetchone()["n"] == 1


def test_changed_cited_fact_rejects_approval_without_writes(approval_case):
    conn, run_id, job_id, sections, retrieved_facts = approval_case
    facts_repo.update_fact(conn, "f1", 1, {"content": "Changed after retrieval"})
    with pytest.raises(ApprovalConflict):
        persist_approval(
            conn, run_id=run_id, draft_revision=2, job_id=job_id,
            sections=sections, retrieved_facts=retrieved_facts,
        )
    assert conn.execute("SELECT count(*) AS n FROM resume_versions").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM workflow_approvals").fetchone()["n"] == 0


def test_persist_approval_rolls_back_every_table_when_event_insert_fails(approval_case):
    conn, run_id, job_id, sections, retrieved_facts = approval_case
    conn.execute("CREATE FUNCTION fail_t7_event() RETURNS trigger LANGUAGE plpgsql AS "
                 "$$ BEGIN RAISE EXCEPTION 'injected event failure'; END $$")
    conn.execute("CREATE TRIGGER fail_t7_event BEFORE INSERT ON audit_events "
                 "FOR EACH ROW EXECUTE FUNCTION fail_t7_event()")
    try:
        with pytest.raises(psycopg.Error, match="injected event failure"):
            persist_approval(conn, run_id=run_id, draft_revision=2, job_id=job_id,
                              sections=sections, retrieved_facts=retrieved_facts)
    finally:
        conn.execute("DROP TRIGGER fail_t7_event ON audit_events")
        conn.execute("DROP FUNCTION fail_t7_event()")
    assert conn.execute("SELECT count(*) AS n FROM resume_versions").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM resume_claims").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM resume_version_facts").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM workflow_approvals").fetchone()["n"] == 0
