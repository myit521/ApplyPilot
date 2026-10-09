"""PostgreSQL integration coverage for durable workflow task admission and lifecycle."""

import threading

import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from applypilot import db
from applypilot.task_repo import (
    InvalidTaskTransition,
    WorkflowJobNotFound,
    WorkflowTaskConflict,
    claim_due_task,
    get_task,
    recover_running_tasks,
    request_cancel,
    reserve_workflow_task,
    settle_task,
)

pytestmark = pytest.mark.integration

STATUSES = ("queued", "running", "retry_wait", "waiting_approval",
            "completed", "failed", "cancelled")

CLEAN_TABLES = ("workflow_tasks", "workflow_approvals", "resume_version_facts",
                "resume_claims", "resume_versions", "applications",
                "workflow_runs", "jobs", "audit_events", "fact_revisions", "facts")


def _dsn(pg: PostgresContainer) -> str:
    return pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


def test_t8_migration_preserves_preexisting_rows_and_is_idempotent():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        with db.connect(_dsn(pg)) as conn:
            conn.execute(db.SCHEMA_PATH.read_text(encoding="utf-8"))
            job = conn.execute(
                "INSERT INTO jobs (raw_text) VALUES ('legacy JD') RETURNING id"
            ).fetchone()
            conn.execute(
                "INSERT INTO workflow_runs (id, current_node, status) "
                "VALUES ('wf_legacy', 'approval', 'WAITING_APPROVAL')"
            )
            db.migrate(conn)
            db.migrate(conn)

            saved = conn.execute(
                "SELECT id, current_node, status FROM workflow_runs WHERE id='wf_legacy'"
            ).fetchone()
            assert saved == {"id": "wf_legacy", "current_node": "approval",
                             "status": "WAITING_APPROVAL"}
            assert conn.execute(
                "SELECT count(*) AS n FROM workflow_tasks"
            ).fetchone()["n"] == 0
            assert conn.execute(
                "SELECT raw_text FROM jobs WHERE id=%s", (job["id"],)
            ).fetchone()["raw_text"] == "legacy JD"
            assert conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version='004_workflow_tasks.sql'"
            ).fetchone()
            assert db.schema_ready(conn)
            conn.execute(
                "DELETE FROM schema_migrations WHERE version='004_workflow_tasks.sql'"
            )
            assert not db.schema_ready(conn)


@pytest.fixture(scope="module")
def task_dsn():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = _dsn(pg)
        with db.connect(dsn) as conn:
            db.init_schema(conn)
        yield dsn


@pytest.fixture
def seeded(task_dsn):
    with db.connect(task_dsn) as conn:
        conn.execute(f"TRUNCATE {', '.join(CLEAN_TABLES)} CASCADE")
        jobs = [
            conn.execute(
                "INSERT INTO jobs (source, company, title, raw_text) "
                "VALUES ('paste', %s, %s, 'JD text') RETURNING id",
                (f"T8 fixture {name}", f"Java 后端 {name}"),
            ).fetchone()["id"]
            for name in ("A", "B")
        ]
        yield conn, jobs


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_same_key_same_job_returns_original_run_in_every_state(seeded):
    conn, jobs = seeded
    job_id = jobs[0]
    first = reserve_workflow_task(conn, job_id=job_id, idempotency_key="key-1")
    assert first["created"] is True
    assert first["idempotency_key"] == "key-1"

    for status in STATUSES:
        conn.execute(
            "UPDATE workflow_tasks SET status=%s, attempt_count=3 WHERE run_id=%s",
            (status, first["run_id"]),
        )
        again = reserve_workflow_task(conn, job_id=job_id, idempotency_key="key-1")
        assert again == {
            "created": False,
            "run_id": first["run_id"],
            "job_id": job_id,
            "status": status,
            "idempotency_key": "key-1",
        }
    assert _count(conn, "workflow_runs") == 1
    assert _count(conn, "workflow_tasks") == 1


def test_same_key_different_job_conflicts(seeded):
    conn, jobs = seeded
    reserve_workflow_task(conn, job_id=jobs[0], idempotency_key="job-key")
    with pytest.raises(WorkflowTaskConflict):
        reserve_workflow_task(conn, job_id=jobs[1], idempotency_key="job-key")
    assert _count(conn, "workflow_tasks") == 1
    assert _count(conn, "workflow_runs") == 1
    rows = conn.execute("SELECT job_id FROM workflow_tasks").fetchall()
    assert [row["job_id"] for row in rows] == [jobs[0]]
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_tasks WHERE job_id=%s", (jobs[1],)
    ).fetchone()["n"] == 0


def test_reservation_requires_existing_job(seeded):
    conn, jobs = seeded
    missing = max(jobs) + 10_000
    with pytest.raises(WorkflowJobNotFound):
        reserve_workflow_task(conn, job_id=missing, idempotency_key="missing-job")
    assert _count(conn, "workflow_runs") == 0
    assert _count(conn, "workflow_tasks") == 0


def test_no_key_always_creates_a_new_run(seeded):
    conn, jobs = seeded
    first = reserve_workflow_task(conn, job_id=jobs[0])
    second = reserve_workflow_task(conn, job_id=jobs[0])
    assert first["created"] is True and second["created"] is True
    assert first["run_id"] != second["run_id"]
    assert first["idempotency_key"] is None
    assert _count(conn, "workflow_tasks") == 2
    keys = conn.execute(
        "SELECT count(idempotency_key) AS n FROM workflow_tasks"
    ).fetchone()["n"]
    assert keys == 0


def test_concurrent_same_key_leaves_one_run_and_one_task(task_dsn, seeded):
    _, jobs = seeded
    job_id = jobs[0]
    started = threading.Barrier(2)
    results: dict[int, dict] = {}

    def worker(index: int) -> None:
        conn = db.connect(task_dsn)
        try:
            started.wait(timeout=30)
            results[index] = reserve_workflow_task(
                conn, job_id=job_id, idempotency_key="race-key"
            )
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    for index in (0, 1):
        assert not threads[index].is_alive(), f"worker {index} did not finish"

    assert len(results) == 2, results
    created = sorted(result["created"] for result in results.values())
    assert created == [False, True]
    assert len({result["run_id"] for result in results.values()}) == 1

    with db.connect(task_dsn) as conn:
        assert _count(conn, "workflow_tasks") == 1
        assert _count(conn, "workflow_runs") == 1
        row = conn.execute(
            "SELECT run_id, job_id, status FROM workflow_tasks"
        ).fetchone()
    assert row["job_id"] == job_id
    assert row["status"] == "queued"


def test_failed_reservation_rolls_back_run_and_task(seeded):
    conn, jobs = seeded
    conn.execute("CREATE FUNCTION fail_t8_task() RETURNS trigger LANGUAGE plpgsql AS "
                 "$$ BEGIN RAISE EXCEPTION 'injected task failure'; END $$")
    conn.execute("CREATE TRIGGER fail_t8_task AFTER INSERT ON workflow_tasks "
                 "FOR EACH ROW EXECUTE FUNCTION fail_t8_task()")
    try:
        with pytest.raises(psycopg.Error, match="injected task failure"):
            reserve_workflow_task(conn, job_id=jobs[0], idempotency_key="boom")
    finally:
        conn.execute("DROP TRIGGER IF EXISTS fail_t8_task ON workflow_tasks")
        conn.execute("DROP FUNCTION IF EXISTS fail_t8_task()")
    assert _count(conn, "workflow_runs") == 0
    assert _count(conn, "workflow_tasks") == 0


def test_legacy_wf_key_run_conflicts_and_stays_unclaimed(seeded):
    conn, jobs = seeded
    key = "legacy-key"
    conn.execute(
        "INSERT INTO workflow_runs (id, current_node, status) "
        "VALUES (%s, 'approval', 'WAITING_APPROVAL')",
        (f"wf_{key}",),
    )
    with pytest.raises(WorkflowTaskConflict):
        reserve_workflow_task(conn, job_id=jobs[0], idempotency_key=key)
    with pytest.raises(WorkflowTaskConflict):
        reserve_workflow_task(conn, job_id=jobs[1], idempotency_key=key)
    assert _count(conn, "workflow_tasks") == 0
    assert _count(conn, "workflow_runs") == 1
    assert conn.execute(
        "SELECT id FROM workflow_runs"
    ).fetchone()["id"] == f"wf_{key}"


def _reserve(conn, job_id: int, key: str | None = None) -> str:
    return reserve_workflow_task(conn, job_id=job_id, idempotency_key=key)["run_id"]


def _set(conn, run_id: str, status: str | None = None, **flags) -> None:
    updates = ["updated_at=now()"]
    params: list = []
    if status is not None:
        updates.append("status=%s")
        params.append(status)
    for column, value in flags.items():
        updates.append(f"{column}=%s")
        params.append(value)
    params.append(run_id)
    conn.execute(
        f"UPDATE workflow_tasks SET {', '.join(updates)} WHERE run_id=%s", tuple(params)
    )


def _due_in_db(conn, run_id: str) -> bool:
    row = conn.execute(
        "SELECT next_attempt_at <= now() AS due FROM workflow_tasks WHERE run_id=%s",
        (run_id,),
    ).fetchone()
    return bool(row["due"])


def _run_two_workers(task_dsn, action) -> dict:
    """Call ``action`` on two separate connections released together."""
    started = threading.Barrier(2)
    results: dict[int, object] = {}

    def worker(index: int) -> None:
        conn = db.connect(task_dsn)
        try:
            started.wait(timeout=30)
            results[index] = action(conn)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    for index in (0, 1):
        assert not threads[index].is_alive(), f"worker {index} did not finish"
    return results


def test_get_task_returns_lifecycle_fields(seeded):
    conn, jobs = seeded
    run_id = _reserve(conn, jobs[0], "read-key")
    row = get_task(conn, run_id)
    assert row["run_id"] == run_id
    assert row["job_id"] == jobs[0]
    assert row["status"] == "queued"
    assert row["attempt_count"] == 0
    assert row["cancel_requested"] is False
    assert row["error"] == ""
    assert row["created_at"] and row["updated_at"]
    assert get_task(conn, "wf_missing") is None


def test_claim_marks_running_counts_attempts_and_prefers_oldest(seeded):
    conn, jobs = seeded
    older = _reserve(conn, jobs[0])
    newer = _reserve(conn, jobs[0])
    conn.execute(
        "UPDATE workflow_tasks SET next_attempt_at = now() - interval '1 hour' "
        "WHERE run_id=%s", (older,)
    )
    conn.execute(
        "UPDATE workflow_tasks SET next_attempt_at = now() WHERE run_id=%s", (newer,)
    )

    claimed = claim_due_task(conn)
    assert claimed["run_id"] == older
    assert claimed["status"] == "running"
    assert claimed["attempt_count"] == 1
    second = claim_due_task(conn)
    assert second["run_id"] == newer
    assert second["attempt_count"] == 1
    assert claim_due_task(conn) is None


def test_claim_skips_cancelled_and_not_yet_due(seeded):
    conn, jobs = seeded
    flagged = _reserve(conn, jobs[0])
    future = _reserve(conn, jobs[0])
    _set(conn, flagged, "queued", cancel_requested=True)
    conn.execute(
        "UPDATE workflow_tasks SET status='retry_wait', "
        "next_attempt_at = now() + interval '1 hour' WHERE run_id=%s", (future,)
    )
    assert claim_due_task(conn) is None

    conn.execute(
        "UPDATE workflow_tasks SET next_attempt_at = now() - interval '1 second' "
        "WHERE run_id=%s", (future,)
    )
    claimed = claim_due_task(conn)
    assert claimed["run_id"] == future
    assert get_task(conn, flagged)["status"] == "queued"


def test_concurrent_claims_are_exclusive(task_dsn, seeded):
    conn, jobs = seeded
    run_id = _reserve(conn, jobs[0])
    results = _run_two_workers(task_dsn, claim_due_task)

    claimed = [row for row in results.values() if row is not None]
    assert len(claimed) == 1, results
    assert claimed[0]["run_id"] == run_id
    assert claimed[0]["attempt_count"] == 1
    assert get_task(conn, run_id)["status"] == "running"


def test_recover_running_tasks_requeues_only_running(seeded):
    conn, jobs = seeded
    run_ids = {name: _reserve(conn, jobs[0]) for name in
               ("stale", "flagged", "waiting", "completed", "failed", "cancelled", "queued")}
    _set(conn, run_ids["stale"], "running")
    _set(conn, run_ids["flagged"], "running", cancel_requested=True)
    _set(conn, run_ids["waiting"], "waiting_approval")
    _set(conn, run_ids["completed"], "completed")
    _set(conn, run_ids["failed"], "failed", error="sanitized failure")
    _set(conn, run_ids["cancelled"], "cancelled")
    before = {name: get_task(conn, run_ids[name]) for name in
              ("waiting", "completed", "failed", "cancelled", "queued")}

    assert recover_running_tasks(conn) == 2

    stale = get_task(conn, run_ids["stale"])
    assert stale["status"] == "queued"
    assert _due_in_db(conn, run_ids["stale"]) is True
    assert get_task(conn, run_ids["flagged"])["status"] == "cancelled"
    for name, row in before.items():
        assert get_task(conn, run_ids[name]) == row, name
    assert recover_running_tasks(conn) == 0


def test_request_cancel_before_claim_marks_cancelled(seeded):
    conn, jobs = seeded
    queued = _reserve(conn, jobs[0])
    waiting = _reserve(conn, jobs[0])
    _set(conn, waiting, "waiting_approval")

    cancelled = request_cancel(conn, queued)
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] is True
    assert request_cancel(conn, waiting)["status"] == "cancelled"
    assert request_cancel(conn, "wf_missing") is None
    assert claim_due_task(conn) is None
    assert get_task(conn, queued)["status"] == "cancelled"


def test_request_cancel_while_running_only_flags(seeded):
    conn, jobs = seeded
    run_id = _reserve(conn, jobs[0])
    assert claim_due_task(conn)["run_id"] == run_id

    flagged = request_cancel(conn, run_id)
    assert flagged["status"] == "running"
    assert flagged["cancel_requested"] is True
    assert get_task(conn, run_id)["status"] == "running"
    assert claim_due_task(conn) is None


def test_request_cancel_leaves_terminal_tasks_unchanged(seeded):
    conn, jobs = seeded
    for status in ("completed", "failed", "cancelled"):
        run_id = _reserve(conn, jobs[0])
        _set(conn, run_id, status)
        before = get_task(conn, run_id)
        assert request_cancel(conn, run_id) == before
        assert get_task(conn, run_id) == before


def test_settle_records_worker_outcome(seeded):
    conn, jobs = seeded
    for target, error in (("waiting_approval", ""), ("completed", ""),
                          ("failed", "model call timed out")):
        run_id = _reserve(conn, jobs[0])
        assert claim_due_task(conn)["run_id"] == run_id
        settled = settle_task(conn, run_id, target, error=error)
        assert settled["status"] == target
        assert settled["error"] == error
        assert settled["attempt_count"] == 1
        # Terminal outcomes are monotonic: settling twice is rejected.
        with pytest.raises(InvalidTaskTransition):
            settle_task(conn, run_id, "completed")


def test_settle_cancellation_wins_over_any_outcome(seeded):
    conn, jobs = seeded
    run_id = _reserve(conn, jobs[0])
    claim_due_task(conn)
    request_cancel(conn, run_id)
    settled = settle_task(conn, run_id, "completed")
    assert settled["status"] == "cancelled"
    assert settled["cancel_requested"] is True


def test_settle_retry_wait_is_not_due_until_delay_passes(seeded):
    conn, jobs = seeded
    run_id = _reserve(conn, jobs[0])
    claim_due_task(conn)
    settled = settle_task(conn, run_id, "retry_wait", retry_delay_seconds=3600)
    assert settled["status"] == "retry_wait"
    future_due = conn.execute(
        "SELECT next_attempt_at > now() AS future FROM workflow_tasks WHERE run_id=%s",
        (run_id,),
    ).fetchone()["future"]
    assert future_due is True
    assert claim_due_task(conn) is None

    conn.execute(
        "UPDATE workflow_tasks SET next_attempt_at = now() - interval '1 second' "
        "WHERE run_id=%s", (run_id,)
    )
    reclaimed = claim_due_task(conn)
    assert reclaimed["attempt_count"] == 2
    with pytest.raises(InvalidTaskTransition):
        settle_task(conn, run_id, "retry_wait")
    with pytest.raises(InvalidTaskTransition):
        settle_task(conn, run_id, "retry_wait", retry_delay_seconds=-1)


def test_settle_rejects_invalid_transitions(seeded):
    conn, jobs = seeded
    run_id = _reserve(conn, jobs[0])
    assert settle_task(conn, "wf_missing", "completed") is None
    for target in ("queued", "running", "completed"):
        with pytest.raises(InvalidTaskTransition):
            settle_task(conn, run_id, target)
    assert get_task(conn, run_id)["status"] == "queued"


def test_attempt_count_is_independent_of_graph_retry_count(seeded):
    conn, jobs = seeded
    run_id = _reserve(conn, jobs[0])
    conn.execute("UPDATE workflow_runs SET retry_count=7 WHERE id=%s", (run_id,))
    assert claim_due_task(conn)["attempt_count"] == 1
    settle_task(conn, run_id, "retry_wait", retry_delay_seconds=0)
    conn.execute(
        "UPDATE workflow_tasks SET next_attempt_at = now() - interval '1 second' "
        "WHERE run_id=%s", (run_id,)
    )
    assert claim_due_task(conn)["attempt_count"] == 2
    assert conn.execute(
        "SELECT retry_count FROM workflow_runs WHERE id=%s", (run_id,)
    ).fetchone()["retry_count"] == 7
