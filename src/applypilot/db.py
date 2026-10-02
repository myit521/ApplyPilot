"""数据库连接与初始化。

连接串通过环境变量 DATABASE_URL 提供，默认指向 docker-compose
启动的本地实例。
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "postgresql://applypilot:applypilot@localhost:5432/applypilot"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def default_dsn() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DSN)


def connect(dsn: str | None = None, **kwargs) -> psycopg.Connection:
    return psycopg.connect(dsn or default_dsn(), row_factory=dict_row, autocommit=True, **kwargs)


def init_schema(conn: psycopg.Connection) -> None:
    """执行 schema.sql 建表（幂等，全部 IF NOT EXISTS）。"""
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    migrate(conn)


def migrate(conn: psycopg.Connection) -> None:
    """Apply versioned migrations once, preserving existing rows."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(7182042)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)")
        for path in sorted((SCHEMA_PATH.parent / "migrations").glob("*.sql")):
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=%s", (path.name,)).fetchone():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations VALUES (%s)", (path.name,))


def schema_ready(conn: psycopg.Connection) -> bool:
    if not conn.execute("SELECT to_regclass('schema_migrations') AS name").fetchone()["name"]:
        return False
    tables = conn.execute(
        "SELECT bool_and(to_regclass(name) IS NOT NULL) AS ready "
        "FROM unnest(ARRAY['fact_revisions','profile','profile_revisions']) AS tables(name)"
    ).fetchone()
    if not tables["ready"]:
        return False
    return bool(conn.execute("SELECT 1 FROM schema_migrations WHERE version='002_fact_confirmation.sql'").fetchone())


if __name__ == "__main__":
    with connect() as connection:
        init_schema(connection)
    print("Database schema is current")
