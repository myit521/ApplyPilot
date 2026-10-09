"""PostgreSQL integration coverage for manually recorded applications (T9)."""

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from psycopg import errors
from testcontainers.postgres import PostgresContainer

from applypilot import db
from applypilot.application_repo import (
    ApplicationDuplicate,
    ApplicationIdempotencyConflict,
    ApplicationJobVersionMismatch,
    ApplicationNotFound,
    ApplicationRevisionConflict,
    ApplicationSourceLocked,
    ApplicationVersionNotApproved,
    InvalidApplicationStatus,
    create_application,
    get_application,
    list_applications,
    update_application,
)

pytestmark = pytest.mark.integration

OCCURRED_AT = datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc)

CLEAN_TABLES = ("applications", "workflow_approvals", "resume_version_facts",
                "resume_claims", "resume_versions", "workflow_runs", "jobs",
                "audit_events", "fact_revisions", "facts")

LEGACY_INSERT = (
    "INSERT INTO applications (job_id, version_id, channel, idempotency_key, "
    "status, result) VALUES (%s, %s, 'web', %s, 'submitted', 'legacy row') RETURNING id"
)


def _dsn(pg: PostgresContainer) -> str:
    return pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def _events(conn, application_id: int) -> list[str]:
    return [row["event_type"] for row in conn.execute(
        "SELECT event_type FROM audit_events WHERE payload->>'application_id'=%s ORDER BY id",
        (str(application_id),),
    ).fetchall()]


def _job(conn, company: str) -> int:
    return conn.execute(
        "INSERT INTO jobs (source, company, title, raw_text) "
        "VALUES ('paste', %s, 'Java 后端', 'JD text') RETURNING id", (company,)
    ).fetchone()["id"]


def _version(conn, job_id: int, *, approved: bool) -> int:
    version_id = conn.execute(
        "INSERT INTO resume_versions (job_id, content, status) "
        "VALUES (%s, '{}'::jsonb, 'approved') RETURNING id", (job_id,)
    ).fetchone()["id"]
    if approved:
        run_id = f"wf_t9_{version_id}"
        conn.execute(
            "INSERT INTO workflow_runs (id, current_node, status) "
            "VALUES (%s, 'approval', 'READY_TO_APPLY')", (run_id,)
        )
        conn.execute(
            "INSERT INTO workflow_approvals (run_id, draft_revision, content_sha256, version_id) "
            "VALUES (%s, 1, %s, %s)", (run_id, "a" * 64, version_id)
        )
    return version_id


def _create(conn, job_id, version_id, *, key="t9-key", status="unknown",
            occurred_at=None, result="", channel="官网"):
    return create_application(
        conn, job_id=job_id, version_id=version_id, channel=channel, status=status,
        occurred_at=occurred_at, result=result, idempotency_key=key,
    )


def test_t9_migration_preserves_legacy_rows_and_is_idempotent():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        with db.connect(_dsn(pg)) as conn:
            conn.execute(db.SCHEMA_PATH.read_text(encoding="utf-8"))
            job_id = _job(conn, "T9 legacy")
            version_id = _version(conn, job_id, approved=False)
            legacy = conn.execute(LEGACY_INSERT, (job_id, version_id, "legacy-key")).fetchone()
            db.migrate(conn)
            db.migrate(conn)

            row = conn.execute(
                "SELECT * FROM applications WHERE id=%s", (legacy["id"],)
            ).fetchone()
            assert row["source"] == "legacy_unverified"
            assert row["status"] == "submitted"
            assert row["result"] == "legacy row"
            assert row["occurred_at"] is None
            assert row["revision"] == 1
            assert row["updated_at"]
            assert conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version='005_manual_applications.sql'"
            ).fetchone()
            assert db.schema_ready(conn)
            conn.execute(
                "DELETE FROM schema_migrations WHERE version='005_manual_applications.sql'"
            )
            assert not db.schema_ready(conn)

            # unknown joins the vocabulary, old statuses survive, and no status
            # can claim a site confirmation.
            conn.execute(
                "INSERT INTO applications (job_id, version_id, channel, idempotency_key, status) "
                "VALUES (%s, %s, 'email', 'unknown-key', 'unknown')", (job_id, version_id)
            )
            assert conn.execute(
                "SELECT count(*) AS n FROM applications WHERE status='unknown'"
            ).fetchone()["n"] == 1
            with pytest.raises(errors.UniqueViolation):
                conn.execute(LEGACY_INSERT, (job_id, version_id, "legacy-key"))
            with pytest.raises(errors.CheckViolation):
                conn.execute(
                    "INSERT INTO applications (job_id, version_id, channel, idempotency_key, status) "
                    "VALUES (%s, %s, 'sms', 'bogus-key', 'site_confirmed')", (job_id, version_id)
                )


@pytest.fixture(scope="module")
def application_dsn():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = _dsn(pg)
        with db.connect(dsn) as conn:
            db.init_schema(conn)
        yield dsn


@pytest.fixture
def seeded(application_dsn):
    with db.connect(application_dsn) as conn:
        conn.execute(f"TRUNCATE {', '.join(CLEAN_TABLES)} CASCADE")
        jobs = [_job(conn, f"T9 fixture {name}") for name in ("A", "B")]
        versions = {
            "approved": _version(conn, jobs[0], approved=True),
            "unapproved": _version(conn, jobs[0], approved=False),
            "other_job": _version(conn, jobs[1], approved=True),
        }
        yield conn, jobs, versions


def test_create_rejects_unapproved_and_mismatched_versions(seeded):
    conn, jobs, versions = seeded
    with pytest.raises(ApplicationVersionNotApproved):
        _create(conn, jobs[0], versions["unapproved"])
    with pytest.raises(ApplicationJobVersionMismatch):
        _create(conn, jobs[0], versions["other_job"])
    assert _count(conn, "applications") == 0
    assert _count(conn, "audit_events") == 0


def test_create_records_user_reported_source_and_one_event(seeded):
    conn, jobs, versions = seeded
    record = _create(conn, jobs[0], versions["approved"], key="t9-create", result="尚无回执")
    assert record["source"] == "user_reported"
    assert record["status"] == "unknown"
    assert record["occurred_at"] is None
    assert record["result"] == "尚无回执"
    assert record["revision"] == 1
    assert record["idempotency_key"] == "t9-create"
    assert get_application(conn, record["id"]) == record
    assert _events(conn, record["id"]) == ["application.created"]
    assert [row["id"] for row in list_applications(conn)] == [record["id"]]


def test_same_key_same_payload_replays_without_second_event(seeded):
    conn, jobs, versions = seeded
    record = _create(conn, jobs[0], versions["approved"], key="t9-retry", result="尚无回执")
    again = _create(conn, jobs[0], versions["approved"], key="t9-retry", result="尚无回执")
    assert again == record
    assert _count(conn, "applications") == 1
    assert _events(conn, record["id"]) == ["application.created"]


def test_same_create_request_replays_after_result_correction(seeded):
    conn, jobs, versions = seeded
    record = _create(conn, jobs[0], versions["approved"], key="t9-late-retry")
    update_application(conn, record["id"], expected_revision=1, status="submitted",
                       occurred_at=OCCURRED_AT, result="用户自述已提交")
    replay = _create(conn, jobs[0], versions["approved"], key="t9-late-retry")
    assert replay["id"] == record["id"]
    assert replay["status"] == "submitted"
    assert _events(conn, record["id"]) == ["application.created", "application.updated"]


def test_same_key_different_payload_conflicts(seeded):
    conn, jobs, versions = seeded
    _create(conn, jobs[0], versions["approved"], key="t9-reuse", result="尚无回执")
    with pytest.raises(ApplicationIdempotencyConflict):
        _create(conn, jobs[0], versions["approved"], key="t9-reuse", result="改口了")
    with pytest.raises(ApplicationIdempotencyConflict):
        _create(conn, jobs[0], versions["approved"], key="t9-reuse",
                occurred_at=OCCURRED_AT, result="尚无回执")
    assert _count(conn, "applications") == 1
    assert _count(conn, "audit_events") == 1


def test_same_job_version_channel_with_another_key_conflicts(seeded):
    conn, jobs, versions = seeded
    _create(conn, jobs[0], versions["approved"], key="t9-first")
    with pytest.raises(ApplicationDuplicate):
        _create(conn, jobs[0], versions["approved"], key="t9-second")
    assert _count(conn, "applications") == 1
    # A different channel for the same version is a different record.
    second = _create(conn, jobs[0], versions["approved"], key="t9-second", channel="邮箱")
    assert second["channel"] == "邮箱"
    assert _count(conn, "applications") == 2


def test_concurrent_same_job_version_channel_has_one_winner(seeded, application_dsn):
    conn, jobs, versions = seeded
    barrier = Barrier(2)

    def attempt(key):
        with db.connect(application_dsn) as writer:
            barrier.wait(timeout=10)
            try:
                return _create(writer, jobs[0], versions["approved"], key=key)
            except ApplicationDuplicate:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(attempt, ("t9-race-a", "t9-race-b")))
    assert sum(record is not None for record in records) == 1
    assert _count(conn, "applications") == 1
    assert _count(conn, "audit_events") == 1


def test_status_outside_the_manual_vocabulary_is_refused(seeded):
    conn, jobs, versions = seeded
    with pytest.raises(InvalidApplicationStatus):
        _create(conn, jobs[0], versions["approved"], key="t9-bad", status="site_confirmed")
    with pytest.raises(InvalidApplicationStatus):
        _create(conn, jobs[0], versions["approved"], key="t9-bad", status="created")
    assert _count(conn, "applications") == 0
    assert _count(conn, "audit_events") == 0


def test_naive_occurred_at_is_refused_before_insert(seeded):
    conn, jobs, versions = seeded
    with pytest.raises(ValueError, match="time zone"):
        _create(conn, jobs[0], versions["approved"], key="t9-naive",
                occurred_at=datetime(2026, 10, 9, 2, 0))
    assert _count(conn, "applications") == 0
    record = _create(conn, jobs[0], versions["approved"], key="t9-aware")
    with pytest.raises(ValueError, match="time zone"):
        update_application(conn, record["id"], expected_revision=1, status="submitted",
                           occurred_at=datetime(2026, 10, 9, 2, 0), result="")
    assert get_application(conn, record["id"])["revision"] == 1


def test_update_bumps_revision_and_appends_one_event(seeded):
    conn, jobs, versions = seeded
    record = _create(conn, jobs[0], versions["approved"], key="t9-update")
    updated = update_application(
        conn, record["id"], expected_revision=record["revision"], status="submitted",
        occurred_at=OCCURRED_AT, result="用户自述已提交",
    )
    assert updated["revision"] == record["revision"] + 1
    assert updated["status"] == "submitted"
    assert updated["occurred_at"] == OCCURRED_AT
    assert updated["result"] == "用户自述已提交"
    assert updated["source"] == "user_reported"
    assert updated["idempotency_key"] == "t9-update"
    assert _events(conn, record["id"]) == ["application.created", "application.updated"]
    assert get_application(conn, record["id"]) == updated


def test_update_rejects_stale_revision_unknown_id_and_old_status(seeded):
    conn, jobs, versions = seeded
    record = _create(conn, jobs[0], versions["approved"], key="t9-stale")
    with pytest.raises(ApplicationRevisionConflict):
        update_application(conn, record["id"], expected_revision=99, status="failed",
                           occurred_at=None, result="")
    with pytest.raises(ApplicationNotFound):
        update_application(conn, record["id"] + 10_000, expected_revision=1,
                           status="failed", occurred_at=None, result="")
    with pytest.raises(InvalidApplicationStatus):
        update_application(conn, record["id"], expected_revision=record["revision"],
                           status="created", occurred_at=None, result="")
    assert get_application(conn, record["id"]) == record
    assert _events(conn, record["id"]) == ["application.created"]


def test_legacy_record_stays_read_only(seeded):
    conn, jobs, versions = seeded
    legacy_id = conn.execute(LEGACY_INSERT, (jobs[0], versions["approved"], "legacy-record")).fetchone()["id"]
    with pytest.raises(ApplicationSourceLocked):
        update_application(conn, legacy_id, expected_revision=1, status="failed",
                           occurred_at=None, result="用户补记")
    assert conn.execute(
        "SELECT status, revision FROM applications WHERE id=%s", (legacy_id,)
    ).fetchone() == {"status": "submitted", "revision": 1}
    assert _events(conn, legacy_id) == []


def test_record_and_event_survive_reconnect(seeded, application_dsn):
    conn, jobs, versions = seeded
    record = _create(conn, jobs[0], versions["approved"], key="t9-reconnect")
    update_application(conn, record["id"], expected_revision=1, status="submitted",
                       occurred_at=OCCURRED_AT, result="用户自述已提交")
    with db.connect(application_dsn) as reopened:
        stored = get_application(reopened, record["id"])
        assert stored["status"] == "submitted"
        assert stored["revision"] == 2
        assert stored["occurred_at"] == OCCURRED_AT
        assert stored["result"] == "用户自述已提交"
        assert [row["id"] for row in list_applications(reopened)] == [record["id"]]
        assert _events(reopened, record["id"]) == ["application.created", "application.updated"]
