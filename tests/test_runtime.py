"""Runtime tests: no Docker, model downloads or real network required."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock

import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver


def test_import_and_factory_do_not_connect():
    script = """
import socket
import psycopg
def forbidden(*args, **kwargs):
    raise AssertionError("import/factory attempted a network connection")
socket.socket.connect = forbidden
psycopg.connect = forbidden
from applypilot.api import create_app
create_app()
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    env.pop("DATABASE_URL", None)
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_database_unavailable_is_live_but_not_ready(monkeypatch):
    from applypilot.api import create_app

    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("secret DSN must not be exposed")
    monkeypatch.setattr(psycopg, "connect", unavailable)
    with TestClient(create_app()) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert "secret" not in response.text
        assert client.get("/api/facts").status_code == 503


def test_owned_checkpoint_connection_closed(monkeypatch):
    from applypilot import api
    connection = MagicMock()
    saver = MagicMock()
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: connection)
    monkeypatch.setattr(api, "PostgresSaver", lambda conn: saver)
    with TestClient(api.create_app()):
        saver.setup.assert_called_once()
        connection.close.assert_not_called()
    connection.close.assert_called_once()


def test_failed_checkpoint_setup_closes_connection(monkeypatch):
    from applypilot import api
    connection = MagicMock()
    saver = MagicMock()
    saver.setup.side_effect = psycopg.OperationalError("private")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: connection)
    monkeypatch.setattr(api, "PostgresSaver", lambda conn: saver)
    with TestClient(api.create_app()) as client:
        assert client.get("/health/ready").status_code == 503
        connection.close.assert_called_once()
    connection.close.assert_called_once()


def test_injected_checkpoint_is_not_initialized_or_closed(monkeypatch):
    from applypilot.api import create_app
    saver = MemorySaver()
    def forbidden(*a, **k):
        raise AssertionError("injected checkpoint should not open a connection")
    monkeypatch.setattr(psycopg, "connect", forbidden)
    with TestClient(create_app(checkpointer=saver)) as client:
        assert client.get("/health/live").status_code == 200


def test_readiness_checks_current_database_and_schema(monkeypatch):
    from applypilot import api
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = {"ready": True, "name": "schema_migrations"}
    monkeypatch.setattr(api.db, "connect", lambda *a, **k: connection)
    with TestClient(api.create_app(checkpointer=MemorySaver())) as client:
        assert client.get("/health/ready").status_code == 200
        connection.execute.return_value.fetchone.return_value = {"ready": False}
        assert client.get("/health/ready").status_code == 503
        connection.execute.side_effect = psycopg.OperationalError("credentials")
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert "credentials" not in response.text
        assert client.get("/health/live").status_code == 200


def test_readiness_detects_lost_checkpoint_connection(monkeypatch):
    from applypilot import api
    checkpoint_conn = MagicMock()
    probe_conn = MagicMock()
    probe_conn.__enter__.return_value = probe_conn
    probe_conn.execute.return_value.fetchone.return_value = {"ready": True, "name": "schema_migrations"}
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: checkpoint_conn)
    monkeypatch.setattr(api.db, "connect", lambda *a, **k: probe_conn)
    monkeypatch.setattr(api, "PostgresSaver", lambda conn: MagicMock())
    with TestClient(api.create_app()) as client:
        assert client.get("/health/ready").status_code == 200
        checkpoint_conn.execute.side_effect = psycopg.OperationalError("connection lost")
        assert client.get("/health/ready").status_code == 503
        assert client.get("/health/live").status_code == 200


def test_schema_ready_requires_t7_tables_and_migration():
    from types import SimpleNamespace

    from applypilot import db

    queries = []
    migration_present = False

    def execute(query, params=None):
        queries.append(query)
        if "to_regclass('schema_migrations')" in query:
            row = {"name": "schema_migrations"}
        elif "bool_and" in query:
            row = {"ready": True}
        elif "003_atomic_approval_snapshot.sql" in query:
            row = {"version": "003_atomic_approval_snapshot.sql"} if migration_present else None
        else:
            row = {"version": "002_fact_confirmation.sql"}
        return SimpleNamespace(fetchone=lambda: row)

    connection = MagicMock()
    connection.execute.side_effect = execute
    assert db.schema_ready(connection) is False
    assert "workflow_approvals" in queries[1]
    assert "resume_version_facts" in queries[1]
    migration_present = True
    assert db.schema_ready(connection) is True
