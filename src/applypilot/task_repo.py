"""Durable workflow task repository (T8 slice 1 admission + slice 2a lifecycle).

``reserve_workflow_task`` writes ``workflow_runs`` and ``workflow_tasks`` in a
single transaction. The lifecycle helpers below are the store operations a
single worker needs: they never start execution and never touch job applications
or approvals.

``attempt_count`` here counts worker claims for one task. It is unrelated to
``workflow_runs.retry_count``, which is the graph's per-node validation counter
owned elsewhere; nothing in this module reads or writes that column.
"""

from __future__ import annotations

import uuid

import psycopg

LEGACY_RUN_PREFIX = "wf_"
INITIAL_NODE = "queued"
INITIAL_STATUS = "queued"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_RETRY_WAIT = "retry_wait"
STATUS_WAITING_APPROVAL = "waiting_approval"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)
CLAIMABLE_STATUSES = (STATUS_QUEUED, STATUS_RETRY_WAIT)
CANCEL_NOW_STATUSES = (STATUS_QUEUED, STATUS_RETRY_WAIT, STATUS_WAITING_APPROVAL)
SETTLE_STATUSES = (STATUS_WAITING_APPROVAL, STATUS_COMPLETED, STATUS_FAILED,
                   STATUS_RETRY_WAIT, STATUS_CANCELLED)

TASK_COLUMNS = ("run_id", "job_id", "idempotency_key", "status", "attempt_count",
                "next_attempt_at", "cancel_requested", "error", "created_at", "updated_at")
_SELECT_TASK = "SELECT " + ", ".join(TASK_COLUMNS) + " FROM workflow_tasks"
_RETURNING = " RETURNING " + ", ".join(TASK_COLUMNS)


class WorkflowTaskConflict(RuntimeError):
    """The idempotency key cannot be reused for this request."""


class WorkflowJobNotFound(LookupError):
    """The job targeted by the reservation does not exist."""


class InvalidTaskTransition(ValueError):
    """A worker asked for a lifecycle step that the current status forbids."""


def reserve_workflow_task(
    conn: psycopg.Connection,
    *,
    job_id: int,
    idempotency_key: str | None = None,
) -> dict:
    """Create-or-reuse one workflow task.

    Returns ``{"created": bool, "run_id": str, "job_id": int, "status": str,
    "idempotency_key": str | None}``. Same key and same job returns the
    original run whatever its state; same key with another job raises
    :class:`WorkflowTaskConflict`; no key always creates a new run.
    """
    with conn.transaction():
        if idempotency_key is not None:
            # Serializes concurrent reservations for the same key so the unique
            # constraint is never the thing that has to arbitrate them.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"t8.task:{idempotency_key}",),
            )
        if conn.execute("SELECT id FROM jobs WHERE id=%s FOR SHARE", (job_id,)).fetchone() is None:
            raise WorkflowJobNotFound(f"job {job_id} does not exist")

        if idempotency_key is not None:
            existing = conn.execute(
                "SELECT run_id, job_id, status FROM workflow_tasks WHERE idempotency_key=%s",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["job_id"] != job_id:
                    raise WorkflowTaskConflict(
                        f"idempotency key already reserved for job {existing['job_id']}"
                    )
                return {
                    "created": False,
                    "run_id": existing["run_id"],
                    "job_id": existing["job_id"],
                    "status": existing["status"],
                    "idempotency_key": idempotency_key,
                }
            _reject_legacy_run(conn, idempotency_key)

        # Never derive the id from the raw key: a legacy row would silently
        # adopt jobs it never belonged to.
        run_id = f"{LEGACY_RUN_PREFIX}{uuid.uuid4().hex}"
        conn.execute(
            "INSERT INTO workflow_runs (id, current_node, status) VALUES (%s, %s, %s)",
            (run_id, INITIAL_NODE, INITIAL_STATUS),
        )
        conn.execute(
            "INSERT INTO workflow_tasks (run_id, job_id, idempotency_key, status) "
            "VALUES (%s, %s, %s, %s)",
            (run_id, job_id, idempotency_key, INITIAL_STATUS),
        )
        return {
            "created": True,
            "run_id": run_id,
            "job_id": job_id,
            "status": INITIAL_STATUS,
            "idempotency_key": idempotency_key,
        }


def _reject_legacy_run(conn: psycopg.Connection, idempotency_key: str) -> None:
    """Old ``wf_<key>`` runs predate this table and carry no job_id."""
    legacy_id = f"{LEGACY_RUN_PREFIX}{idempotency_key}"
    if conn.execute("SELECT 1 FROM workflow_runs WHERE id=%s", (legacy_id,)).fetchone():
        raise WorkflowTaskConflict(
            "idempotency key collides with a legacy workflow run that predates task tracking"
        )


def get_task(conn: psycopg.Connection, run_id: str) -> dict | None:
    """Return one task row, or ``None`` when the run has no task."""
    return conn.execute(f"{_SELECT_TASK} WHERE run_id=%s", (run_id,)).fetchone()


def claim_due_task(conn: psycopg.Connection) -> dict | None:
    """Claim the oldest due ``queued``/``retry_wait`` task for this worker.

    Marking ``running`` and incrementing ``attempt_count`` happen in one
    statement: ``FOR UPDATE SKIP LOCKED`` lets a second connection skip this row
    instead of waiting, so two workers never claim the same task. Rows whose
    cancellation was requested are skipped (they are already ``cancelled``).
    Returns ``None`` when nothing is due.
    """
    with conn.transaction():
        return conn.execute(
            "UPDATE workflow_tasks AS t "
            "SET status=%s, attempt_count=t.attempt_count + 1, updated_at=now() "
            "WHERE t.run_id = ("
            "    SELECT c.run_id FROM workflow_tasks c "
            "    WHERE c.status = ANY(%s) AND c.next_attempt_at <= now() "
            "      AND NOT c.cancel_requested "
            "    ORDER BY c.next_attempt_at, c.created_at, c.run_id "
            "    FOR UPDATE SKIP LOCKED LIMIT 1"
            f"){_RETURNING}",
            (STATUS_RUNNING, list(CLAIMABLE_STATUSES)),
        ).fetchone()


def recover_running_tasks(conn: psycopg.Connection) -> int:
    """Requeue ``running`` tasks left behind by a previous process.

    Runs once at worker startup. A task already flagged for cancellation
    becomes ``cancelled``; ``waiting_approval`` and terminal tasks are untouched
    so approvals and settled outcomes survive a restart.
    """
    with conn.transaction():
        cursor = conn.execute(
            "UPDATE workflow_tasks SET "
            "status = CASE WHEN cancel_requested THEN %s ELSE %s END, "
            "next_attempt_at = now(), updated_at = now() "
            "WHERE status = %s",
            (STATUS_CANCELLED, STATUS_QUEUED, STATUS_RUNNING),
        )
        return cursor.rowcount


def request_cancel(conn: psycopg.Connection, run_id: str) -> dict | None:
    """Ask for cancellation without deleting anything.

    ``queued``/``retry_wait``/``waiting_approval`` become ``cancelled`` at once;
    ``running`` is only flagged and stays ``running`` until the worker settles
    it. Terminal tasks are returned unchanged.
    """
    with conn.transaction():
        row = conn.execute(f"{_SELECT_TASK} WHERE run_id=%s FOR UPDATE", (run_id,)).fetchone()
        if row is None:
            return None
        if row["status"] in TERMINAL_STATUSES:
            return row
        return conn.execute(
            "UPDATE workflow_tasks SET cancel_requested=TRUE, "
            "status = CASE WHEN status = ANY(%s) THEN %s ELSE status END, "
            f"updated_at=now() WHERE run_id=%s{_RETURNING}",
            (list(CANCEL_NOW_STATUSES), STATUS_CANCELLED, run_id),
        ).fetchone()


def settle_task(
    conn: psycopg.Connection,
    run_id: str,
    status: str,
    error: str = "",
    retry_delay_seconds: float | None = None,
) -> dict | None:
    """Record a worker outcome for a task it claimed.

    Only a ``running`` task may be settled, and only to
    ``waiting_approval``/``completed``/``failed``/``retry_wait``/``cancelled``.
    A cancellation requested meanwhile wins over any other target. ``error`` is
    stored verbatim: callers must pass an already sanitized message (``""``
    clears a previous one). ``retry_wait`` needs ``retry_delay_seconds >= 0``
    and is not due again until that delay passes.
    """
    if status not in SETTLE_STATUSES:
        raise InvalidTaskTransition(f"a worker may not settle a task to {status!r}")
    if status == STATUS_RETRY_WAIT and (
        retry_delay_seconds is None or retry_delay_seconds < 0
    ):
        raise InvalidTaskTransition("retry_wait requires retry_delay_seconds >= 0")

    delay_seconds = float(retry_delay_seconds or 0)
    with conn.transaction():
        row = conn.execute(f"{_SELECT_TASK} WHERE run_id=%s FOR UPDATE", (run_id,)).fetchone()
        if row is None:
            return None
        if row["status"] != STATUS_RUNNING:
            raise InvalidTaskTransition(
                f"task is {row['status']}, only a running task can be settled"
            )
        target = STATUS_CANCELLED if row["cancel_requested"] else status
        # Only an actual retry_wait reschedules: a cancellation that overrode it
        # leaves the previous due time alone.
        reschedule = target == STATUS_RETRY_WAIT
        return conn.execute(
            "UPDATE workflow_tasks SET status=%s, error=%s, "
            "next_attempt_at = CASE WHEN %s THEN now() + make_interval(secs => %s) "
            "ELSE next_attempt_at END, updated_at=now() "
            f"WHERE run_id=%s{_RETURNING}",
            (target, error, reschedule, delay_seconds, run_id),
        ).fetchone()
