"""PostgreSQL integration coverage for the frozen contact-profile snapshot."""

import json

import pytest
from testcontainers.postgres import PostgresContainer

from applypilot import db, facts_repo
from applypilot.approval_snapshots import hash_approval_package
from applypilot.approvals_repo import get_approval, persist_approval
from applypilot.schemas import Fact, FactType, ProfileData

pytestmark = pytest.mark.integration

PROFILE_FIELDS = {"name", "email", "phone", "location", "website"}


@pytest.fixture(scope="module")
def profile_dsn():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            db.init_schema(conn)
        yield dsn


@pytest.fixture
def approval_case(profile_dsn):
    with db.connect(profile_dsn) as conn:
        conn.execute(
            "TRUNCATE workflow_approvals, resume_version_facts, resume_claims, "
            "resume_versions, jobs, workflow_runs, audit_events, profile_revisions, "
            "profile, fact_revisions, facts CASCADE"
        )
        job = conn.execute(
            "INSERT INTO jobs (source, company, title, raw_text) "
            "VALUES ('paste', 'T10 fixture', 'Java 后端', 'Java 后端工程师') RETURNING id"
        ).fetchone()
        cited = Fact(
            id="f1", fact_type=FactType.PROJECT, source_name="T10 fixture",
            content="Built batch API", skills=["Java"], status="confirmed", origin="human",
        )
        facts_repo.create_fact(conn, cited)
        run_id = "wf_t10_profile_snapshot"
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
        yield conn, run_id, job["id"], sections, [cited]


def _approve(case):
    conn, run_id, job_id, sections, retrieved_facts = case
    return persist_approval(
        conn, run_id=run_id, draft_revision=2, job_id=job_id,
        sections=sections, retrieved_facts=retrieved_facts,
    )


def _write_profile(conn, confirm, **fields):
    """Write the singleton profile (revision 0 -> 1), optionally confirming it."""
    facts_repo.update_profile(conn, 0, ProfileData(**fields))
    if not confirm:
        return facts_repo.update_profile(conn, 1)
    return facts_repo.update_profile(conn, 1, confirm=True)


def _contact():
    return {
        "name": "批准时姓名",
        "email": "frozen@example.test",
        "phone": "13800000000",
        "location": "上海",
        "website": "https://example.test/profile",
    }


def test_confirmed_profile_is_frozen_into_package_content_and_hash(approval_case):
    conn, run_id, _, _, _ = approval_case
    _write_profile(conn, True, **_contact())

    record = _approve(approval_case)

    snapshot = record["package"]["profile_snapshot"]
    assert record["package"]["schema_version"] == 2
    assert snapshot["revision"] == 2
    assert set(snapshot["data"]) == PROFILE_FIELDS
    assert snapshot["data"]["name"] == "批准时姓名"
    assert snapshot["data"]["location"] == "上海"
    saved = conn.execute(
        "SELECT content FROM resume_versions WHERE id=%s", (record["version_id"],)
    ).fetchone()["content"]
    assert saved["profile_snapshot"] == snapshot
    assert "facts" not in saved
    assert hash_approval_package(record["package"]) == record["content_sha256"]

    tampered = {
        **record["package"],
        "profile_snapshot": {**snapshot, "data": {**snapshot["data"], "email": "tampered@x"}},
    }
    assert hash_approval_package(tampered) != record["content_sha256"]
    assert get_approval(conn, run_id)["package"] == record["package"]


def test_profile_changed_after_approval_leaves_stored_snapshot_and_hash_intact(approval_case):
    conn, run_id, _, _, _ = approval_case
    _write_profile(conn, True, **_contact())

    record = _approve(approval_case)
    version_id, digest, frozen_name = (
        record["version_id"], record["content_sha256"],
        record["package"]["profile_snapshot"]["data"]["name"],
    )

    facts_repo.update_profile(conn, 2, ProfileData(name="改后姓名", email="live@example.test"))
    live = conn.execute("SELECT revision, status, data FROM profile WHERE id=1").fetchone()
    assert (live["revision"], live["status"]) == (3, "draft")
    assert live["data"]["name"] == "改后姓名"

    reread = get_approval(conn, run_id)
    assert reread["content_sha256"] == digest
    assert reread["package"]["profile_snapshot"]["revision"] == 2
    assert reread["package"]["profile_snapshot"]["data"]["name"] == frozen_name
    assert hash_approval_package(reread["package"]) == digest
    stored = conn.execute(
        "SELECT content FROM resume_versions WHERE id=%s", (version_id,)
    ).fetchone()["content"]
    assert stored["profile_snapshot"]["data"]["email"] == "frozen@example.test"


def test_draft_profile_is_omitted_from_the_package(approval_case):
    conn, run_id, _, _, _ = approval_case
    _write_profile(conn, False, **_contact())
    assert conn.execute("SELECT status FROM profile WHERE id=1").fetchone()["status"] == "draft"

    record = _approve(approval_case)

    assert record["package"]["schema_version"] == 2
    assert record["package"]["profile_snapshot"] is None
    assert conn.execute(
        "SELECT content->'profile_snapshot' AS p FROM resume_versions WHERE id=%s",
        (record["version_id"],),
    ).fetchone()["p"] is None
    assert hash_approval_package(record["package"]) == record["content_sha256"]
    assert get_approval(conn, run_id)["package"] == record["package"]


def test_missing_profile_is_omitted_and_never_blocks_approval(approval_case):
    conn, run_id, _, _, _ = approval_case
    assert conn.execute("SELECT count(*) AS n FROM profile").fetchone()["n"] == 0

    record = _approve(approval_case)

    assert record["package"]["profile_snapshot"] is None
    assert record["package"]["schema_version"] == 2
    assert hash_approval_package(record["package"]) == record["content_sha256"]
    assert get_approval(conn, run_id)["package"] == record["package"]


def test_legacy_schema_v1_package_stays_readable_and_hash_valid(approval_case):
    conn, _, job_id, _, _ = approval_case
    _write_profile(conn, True, **_contact())
    legacy = {
        "schema_version": 1,
        "draft_revision": 3,
        "job_snapshot": {
            "id": job_id, "source": "paste", "url": None, "company": "T10 legacy",
            "title": "旧版职位", "raw_text": "旧 JD 原文", "parsed": None,
        },
        "sections": {"education": [], "skills": [], "experience": [
            {"text": "旧事实表述", "fact_ids": ["f1"], "matched_requirements": []},
        ]},
        "facts": [{"id": "f1", "revision": 1, "snapshot": {"content": "Built batch API"}}],
    }
    run_id = "wf_t10_legacy_v1"
    conn.execute(
        "INSERT INTO workflow_runs (id, current_node, status) "
        "VALUES (%s, 'approval', 'WAITING_APPROVAL')", (run_id,),
    )
    content = {k: v for k, v in legacy.items() if k != "facts"}
    version_id = conn.execute(
        "INSERT INTO resume_versions (job_id, content, status) "
        "VALUES (%s, %s::jsonb, 'approved') RETURNING id",
        (job_id, json.dumps(content, ensure_ascii=False)),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO resume_version_facts (version_id, fact_id, fact_revision, snapshot) "
        "VALUES (%s, %s, %s, %s::jsonb)",
        (version_id, "f1", 1, json.dumps(legacy["facts"][0]["snapshot"])),
    )
    digest = hash_approval_package(legacy)
    conn.execute(
        "INSERT INTO workflow_approvals (run_id, draft_revision, content_sha256, version_id) "
        "VALUES (%s, %s, %s, %s)",
        (run_id, legacy["draft_revision"], digest, version_id),
    )

    stored = get_approval(conn, run_id)

    assert stored["package"] == legacy
    assert stored["content_sha256"] == digest
    assert "profile_snapshot" not in stored["package"]
    assert hash_approval_package(stored["package"]) == digest
    assert conn.execute("SELECT status FROM profile WHERE id=1").fetchone()["status"] == "confirmed"
