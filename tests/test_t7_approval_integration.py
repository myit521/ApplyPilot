"""PostgreSQL integration coverage for atomic approval snapshots."""

import pytest
from testcontainers.postgres import PostgresContainer

from applypilot import db

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
