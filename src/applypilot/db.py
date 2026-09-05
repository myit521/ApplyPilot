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


def connect(dsn: str | None = None) -> psycopg.Connection:
    return psycopg.connect(dsn or default_dsn(), row_factory=dict_row, autocommit=True)


def init_schema(conn: psycopg.Connection) -> None:
    """执行 schema.sql 建表（幂等，全部 IF NOT EXISTS）。"""
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
