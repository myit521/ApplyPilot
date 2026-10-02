"""Revisioned facts and singleton contact profile with atomic append-only history."""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from .schemas import Fact, FactType, ProfileData

_COLUMNS = ", ".join(Fact.model_fields)


class RevisionConflict(Exception):
    """The editor is attempting to mutate an outdated revision."""


def _row_to_fact(row: dict) -> Fact:
    return Fact(**{k: row[k] for k in Fact.model_fields if k in row})


def create_fact(
    conn: psycopg.Connection, fact: Fact, embedding: list[float] | None = None,
) -> None:
    """Insert a new fact; existing facts require revision-aware update_fact."""
    values = fact.model_dump()
    placeholders = ", ".join(f"%({key})s" for key in values)
    with conn.transaction():
        conn.execute(
            f"INSERT INTO facts ({_COLUMNS}, embedding) "
            f"VALUES ({placeholders}, %(embedding)s::vector)",
            {**values, "embedding": str(embedding) if embedding is not None else None},
        )
        _record_fact(conn, fact)


def _record_fact(conn: psycopg.Connection, fact: Fact) -> None:
    conn.execute(
        "INSERT INTO fact_revisions(fact_id, revision, snapshot) VALUES (%s, %s, %s)",
        (fact.id, fact.revision, Jsonb(fact.model_dump(mode="json"))),
    )


def get_fact(conn: psycopg.Connection, fact_id: str) -> Fact | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM facts WHERE id = %s", (fact_id,),
    ).fetchone()
    return _row_to_fact(row) if row else None


def list_facts(
    conn: psycopg.Connection,
    fact_type: FactType | None = None,
    enabled_only: bool = True,
) -> list[Fact]:
    sql = f"SELECT {_COLUMNS} FROM facts WHERE TRUE"
    params = []
    if enabled_only:
        sql += " AND enabled"
    if fact_type is not None:
        sql += " AND fact_type = %s"
        params.append(fact_type.value)
    rows = conn.execute(sql + " ORDER BY created_at, id", params).fetchall()
    return [_row_to_fact(row) for row in rows]


def update_fact(
    conn: psycopg.Connection, fact_id: str, expected_revision: int,
    changes: dict | None = None, confirm: bool = False,
) -> Fact:
    with conn.transaction():
        row = conn.execute(
            "SELECT * FROM facts WHERE id = %s FOR UPDATE", (fact_id,),
        ).fetchone()
        if row is None:
            raise KeyError(fact_id)
        fact = _row_to_fact(row)
        if fact.revision != expected_revision:
            raise RevisionConflict()
        data = {
            **fact.model_dump(), **(changes or {}),
            "revision": fact.revision + 1,
            "status": "confirmed" if confirm else "draft",
        }
        fact = Fact.model_validate(data)
        values = fact.model_dump()
        assignments = ", ".join(f"{key}=%({key})s" for key in values if key != "id")
        conn.execute(
            "UPDATE facts SET " + assignments +
            ", embedding=NULL, updated_at=now() WHERE id=%(id)s", values,
        )
        _record_fact(conn, fact)
        return fact


def disable_fact(conn: psycopg.Connection, fact_id: str, expected_revision: int) -> bool:
    if get_fact(conn, fact_id) is None:
        return False
    update_fact(conn, fact_id, expected_revision, {"enabled": False})
    return True


def fact_history(conn: psycopg.Connection, fact_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT snapshot FROM fact_revisions WHERE fact_id=%s ORDER BY revision",
        (fact_id,),
    ).fetchall()
    return [row["snapshot"] for row in rows]


def get_profile(conn: psycopg.Connection) -> dict:
    row = conn.execute("SELECT revision, status, data FROM profile WHERE id=1").fetchone()
    result = {"revision": 0, "status": "draft", "profile": None}
    if row is not None:
        result = {"revision": row["revision"], "status": row["status"], "profile": row["data"]}
    result["education"] = [
        fact.model_dump(mode="json")
        for fact in list_facts(conn, FactType.EDUCATION, enabled_only=False)
    ]
    return result


def update_profile(
    conn: psycopg.Connection, expected_revision: int,
    data: ProfileData | None = None, confirm: bool = False,
) -> dict:
    with conn.transaction():
        # Serializes initial creation too, where no row exists to lock yet.
        conn.execute("SELECT pg_advisory_xact_lock(7182043)")
        current = get_profile(conn)
        if current["revision"] != expected_revision:
            raise RevisionConflict()
        if confirm and current["profile"] is None:
            raise KeyError("profile")
        profile = data.model_dump() if data else current["profile"]
        revision = expected_revision + 1
        status = "confirmed" if confirm else "draft"
        conn.execute(
            "INSERT INTO profile VALUES (1, %s, %s, %s) ON CONFLICT(id) DO UPDATE SET "
            "revision=EXCLUDED.revision, status=EXCLUDED.status, data=EXCLUDED.data",
            (revision, status, Jsonb(profile)),
        )
        snapshot = {"revision": revision, "status": status, "profile": profile}
        conn.execute(
            "INSERT INTO profile_revisions VALUES (%s, %s, now())",
            (revision, Jsonb(snapshot)),
        )
        return get_profile(conn)


def profile_history(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute("SELECT snapshot FROM profile_revisions ORDER BY revision").fetchall()
    return [row["snapshot"] for row in rows]
