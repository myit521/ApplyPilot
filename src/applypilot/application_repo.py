"""Transactional persistence for manually recorded job applications (T9).

Only ``user_reported`` rows are writable. Rows carried over from earlier slices
keep ``legacy_unverified`` and stay read-only, because nothing ever confirmed
them against a site: a stored record is a human note, never proof of delivery.
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

SOURCE_USER_REPORTED = "user_reported"

# The vocabulary a human may record. Anything else (including a status that
# would imply a site confirmation) is rejected here and at the API edge.
STATUSES = ("unknown", "submitted", "failed")

RECORD_COLUMNS = ("id", "job_id", "version_id", "channel", "source", "status",
                  "occurred_at", "result", "idempotency_key", "revision",
                  "created_at", "updated_at")
_SELECT = "SELECT " + ", ".join(RECORD_COLUMNS) + " FROM applications"
_RETURNING = " RETURNING " + ", ".join(RECORD_COLUMNS)


class ApplicationConflict(RuntimeError):
    """The requested record cannot be written as asked."""


class ApplicationVersionNotApproved(ApplicationConflict):
    """The resume version has no stored approval snapshot."""


class ApplicationJobVersionMismatch(ApplicationConflict):
    """The resume version belongs to another job."""


class ApplicationIdempotencyConflict(ApplicationConflict):
    """The idempotency key was already used for a different payload."""


class ApplicationDuplicate(ApplicationConflict):
    """This job, version and channel already have a record."""


class ApplicationRevisionConflict(ApplicationConflict):
    """``expected_revision`` is stale: the record moved on."""


class ApplicationSourceLocked(ApplicationConflict):
    """Only ``user_reported`` records may be updated."""


class ApplicationNotFound(LookupError):
    """No application row carries this id."""


class InvalidApplicationStatus(ValueError):
    """The status is outside the manual recording vocabulary."""


def _check_status(status: str) -> None:
    if status not in STATUSES:
        raise InvalidApplicationStatus(
            f"status must be one of {', '.join(STATUSES)}, got {status!r}"
        )


def _as_utc(value: datetime | None) -> datetime | None:
    """Compare a caller datetime with the aware value Postgres returns."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must include a time zone")
    return value.astimezone(timezone.utc)


def _payload(row: dict) -> tuple:
    return (row["job_id"], row["version_id"], row["channel"], row["status"],
            _as_utc(row["occurred_at"]), row["result"])


def _creation_payload(conn: psycopg.Connection, row: dict) -> tuple | None:
    """Use the original event when a later correction changed the current row."""
    if row["source"] != SOURCE_USER_REPORTED:
        return None
    if row["revision"] == 1:
        return _payload(row)
    event = conn.execute(
        "SELECT payload FROM audit_events WHERE event_type='application.created' "
        "AND payload->>'application_id'=%s ORDER BY id LIMIT 1",
        (str(row["id"]),),
    ).fetchone()
    if event is None:
        return None
    payload = event["payload"]
    occurred_at = payload["occurred_at"]
    return (payload["job_id"], payload["version_id"], payload["channel"],
            payload["status"], datetime.fromisoformat(occurred_at) if occurred_at else None,
            payload["result"])


def _require_approved_version(conn: psycopg.Connection, job_id: int, version_id: int) -> None:
    """A record is only meaningful for a version approved for exactly this job."""
    version = conn.execute(
        "SELECT id, job_id FROM resume_versions WHERE id=%s FOR SHARE", (version_id,),
    ).fetchone()
    if version is None:
        raise ApplicationVersionNotApproved(f"resume version {version_id} does not exist")
    if version["job_id"] != job_id:
        raise ApplicationJobVersionMismatch(
            f"resume version {version_id} belongs to job {version['job_id']}, not {job_id}"
        )
    # resume_versions.status says nothing on its own: only a stored approval does.
    if conn.execute(
        "SELECT 1 FROM workflow_approvals WHERE version_id=%s", (version_id,)
    ).fetchone() is None:
        raise ApplicationVersionNotApproved(f"resume version {version_id} was never approved")


def _audit(conn: psycopg.Connection, event_type: str, row: dict) -> None:
    conn.execute(
        "INSERT INTO audit_events (event_type, payload) VALUES (%s, %s)",
        (event_type, Jsonb({
            "application_id": str(row["id"]),
            "job_id": row["job_id"],
            "version_id": row["version_id"],
            "channel": row["channel"],
            "status": row["status"],
            "occurred_at": row["occurred_at"].isoformat() if row["occurred_at"] else None,
            "result": row["result"],
            "revision": row["revision"],
        })),
    )


def create_application(
    conn: psycopg.Connection,
    *,
    job_id: int,
    version_id: int,
    channel: str,
    status: str,
    occurred_at: datetime | None,
    result: str,
    idempotency_key: str,
) -> dict:
    """Store one user-reported record, or replay an identical retry.

    Reusing a key with the same payload returns the original row without a
    second audit event; a different payload, or a second record for the same
    job, version and channel, is a conflict. The unique constraints are the
    backstop for concurrent writers.
    """
    _check_status(status)
    payload = (job_id, version_id, channel, status, _as_utc(occurred_at), result)
    with conn.transaction():
        # Serializes retries for one key so the unique constraint is never the
        # thing that has to arbitrate them.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"t9.application:{idempotency_key}",),
        )
        existing = conn.execute(
            f"{_SELECT} WHERE idempotency_key=%s", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            if _creation_payload(conn, existing) != payload:
                raise ApplicationIdempotencyConflict(
                    f"idempotency key {idempotency_key!r} was already used for another record"
                )
            return existing
        _require_approved_version(conn, job_id, version_id)
        if conn.execute(
            "SELECT id FROM applications WHERE job_id=%s AND version_id=%s AND channel=%s",
            (job_id, version_id, channel),
        ).fetchone() is not None:
            raise ApplicationDuplicate(
                f"job {job_id} already has a {channel} record for version {version_id}"
            )
        try:
            with conn.transaction():
                row = conn.execute(
                    "INSERT INTO applications (job_id, version_id, channel, idempotency_key, "
                    "status, occurred_at, result, source) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s){_RETURNING}",
                    (job_id, version_id, channel, idempotency_key, status, occurred_at,
                     result, SOURCE_USER_REPORTED),
                ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "applications_job_id_version_id_channel_key":
                raise ApplicationDuplicate(
                    f"job {job_id} already has a {channel} record for version {version_id}"
                ) from exc
            if exc.diag.constraint_name == "applications_idempotency_key_key":
                raise ApplicationIdempotencyConflict("Idempotency-Key was already used") from exc
            raise
        _audit(conn, "application.created", row)
        return row


def get_application(conn: psycopg.Connection, application_id: int) -> dict | None:
    """Return one record, or ``None`` when the id is unknown."""
    return conn.execute(f"{_SELECT} WHERE id=%s", (application_id,)).fetchone()


def list_applications(conn: psycopg.Connection) -> list[dict]:
    """Return every record, newest first."""
    return conn.execute(f"{_SELECT} ORDER BY created_at DESC, id DESC").fetchall()


def update_application(
    conn: psycopg.Connection,
    application_id: int,
    *,
    expected_revision: int,
    status: str,
    occurred_at: datetime | None,
    result: str,
) -> dict:
    """Correct the status, time and note of one user-reported record.

    ``expected_revision`` is the optimistic concurrency check; the stored
    revision is bumped and one audit event is appended in the same transaction.
    """
    _check_status(status)
    occurred_at = _as_utc(occurred_at)
    with conn.transaction():
        row = conn.execute(
            f"{_SELECT} WHERE id=%s FOR UPDATE", (application_id,)
        ).fetchone()
        if row is None:
            raise ApplicationNotFound(f"application {application_id} does not exist")
        if row["source"] != SOURCE_USER_REPORTED:
            raise ApplicationSourceLocked(
                f"application {application_id} is {row['source']}, not user reported"
            )
        if row["revision"] != expected_revision:
            raise ApplicationRevisionConflict(
                f"application {application_id} is at revision {row['revision']}, "
                f"not {expected_revision}"
            )
        updated = conn.execute(
            "UPDATE applications SET status=%s, occurred_at=%s, result=%s, "
            f"revision=revision + 1, updated_at=now() "
            f"WHERE id=%s AND revision=%s{_RETURNING}",
            (status, occurred_at, result, application_id, expected_revision),
        ).fetchone()
        _audit(conn, "application.updated", updated)
        return updated
