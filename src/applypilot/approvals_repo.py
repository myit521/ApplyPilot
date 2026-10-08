"""Transactional persistence for immutable resume approvals."""

from __future__ import annotations

from collections.abc import Iterable

import psycopg
from psycopg.types.json import Jsonb

from .approval_snapshots import build_approval_package, hash_approval_package


class ApprovalConflict(Exception):
    """The draft no longer matches the evidence available for approval."""


class ApprovalIntegrityError(RuntimeError):
    """A stored approval snapshot no longer matches its persisted hash."""


def _cited_fact_ids(sections: dict) -> list[str]:
    return sorted({
        fact_id
        for section in ("education", "skills", "experience")
        for claim in sections.get(section, [])
        for fact_id in claim["fact_ids"]
    })


def _retrieved_revisions(retrieved_facts: Iterable) -> dict[str, int]:
    return {fact.id: fact.revision for fact in retrieved_facts}


def _lock_job(conn: psycopg.Connection, job_id: int) -> dict:
    job = conn.execute(
        "SELECT id, source, url, company, title, raw_text, parsed "
        "FROM jobs WHERE id=%s FOR SHARE",
        (job_id,),
    ).fetchone()
    if job is None:
        raise ApprovalConflict("The job no longer exists")
    return job


def _lock_and_validate_facts(
    conn: psycopg.Connection,
    cited_ids: list[str],
    retrieved_revisions: dict[str, int],
) -> list[dict]:
    if not cited_ids:
        return []
    missing = sorted(set(cited_ids) - retrieved_revisions.keys())
    if missing:
        raise ApprovalConflict(f"Claim references facts not retrieved: {', '.join(missing)}")

    rows = conn.execute(
        "SELECT id, enabled, status, revision FROM facts "
        "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
        (cited_ids,),
    ).fetchall()
    if len(rows) != len(cited_ids):
        raise ApprovalConflict("A cited fact no longer exists")
    for row in rows:
        if (not row["enabled"] or row["status"] != "confirmed"
                or row["revision"] != retrieved_revisions[row["id"]]):
            raise ApprovalConflict("A cited fact changed after retrieval")
    return rows


def _load_cited_fact_revisions(
    conn: psycopg.Connection,
    cited_ids: list[str],
    locked_facts: list[dict],
) -> list[dict]:
    if not cited_ids:
        return []
    rows = conn.execute(
        "SELECT f.id, f.revision, fr.snapshot FROM facts f "
        "JOIN fact_revisions fr ON fr.fact_id=f.id AND fr.revision=f.revision "
        "WHERE f.id = ANY(%s) ORDER BY f.id",
        (cited_ids,),
    ).fetchall()
    if len(rows) != len(locked_facts):
        raise ApprovalConflict("A cited fact revision snapshot is missing")
    return rows


def _insert_approved_version(
    conn: psycopg.Connection, job_id: int, content: dict,
) -> int:
    row = conn.execute(
        "INSERT INTO resume_versions (job_id, content, status) "
        "VALUES (%s, %s, 'approved') RETURNING id",
        (job_id, Jsonb(content)),
    ).fetchone()
    return row["id"]


def _insert_claim_rows(conn: psycopg.Connection, version_id: int, sections: dict) -> None:
    for section in ("education", "skills", "experience"):
        for claim in sections[section]:
            conn.execute(
                "INSERT INTO resume_claims (version_id, text, fact_ids, matched_requirements) "
                "VALUES (%s, %s, %s, %s)",
                (version_id, claim["text"], claim["fact_ids"], claim["matched_requirements"]),
            )


def _insert_fact_snapshot_rows(
    conn: psycopg.Connection, version_id: int, fact_snapshots: list[dict],
) -> None:
    for fact in fact_snapshots:
        conn.execute(
            "INSERT INTO resume_version_facts (version_id, fact_id, fact_revision, snapshot) "
            "VALUES (%s, %s, %s, %s)",
            (version_id, fact["id"], fact["revision"], Jsonb(fact["snapshot"])),
        )


def get_approval(conn: psycopg.Connection, run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT wa.run_id, wa.draft_revision, wa.content_sha256, wa.version_id, "
        "wa.graph_reconciled, wa.approved_at, rv.job_id, rv.content "
        "FROM workflow_approvals wa JOIN resume_versions rv ON rv.id=wa.version_id "
        "WHERE wa.run_id=%s",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    facts = conn.execute(
        "SELECT fact_id AS id, fact_revision AS revision, snapshot "
        "FROM resume_version_facts WHERE version_id=%s ORDER BY fact_id",
        (row["version_id"],),
    ).fetchall()
    package = {**row["content"], "facts": facts}
    if hash_approval_package(package) != row["content_sha256"]:
        raise ApprovalIntegrityError(f"Approval snapshot hash mismatch for run {run_id}")
    return {
        "run_id": row["run_id"],
        "draft_revision": row["draft_revision"],
        "content_sha256": row["content_sha256"],
        "version_id": row["version_id"],
        "graph_reconciled": row["graph_reconciled"],
        "approved_at": row["approved_at"],
        "job_id": row["job_id"],
        "package": package,
    }


def persist_approval(
    conn: psycopg.Connection,
    *,
    run_id: str,
    draft_revision: int,
    job_id: int,
    sections: dict,
    retrieved_facts: list,
) -> dict:
    cited_ids = _cited_fact_ids(sections)
    retrieved_revisions = _retrieved_revisions(retrieved_facts)

    with conn.transaction():
        job = _lock_job(conn, job_id)
        locked_facts = _lock_and_validate_facts(conn, cited_ids, retrieved_revisions)
        fact_snapshots = _load_cited_fact_revisions(conn, cited_ids, locked_facts)
        package = build_approval_package(
            job=job,
            sections=sections,
            fact_snapshots=fact_snapshots,
            draft_revision=draft_revision,
        )
        content_sha256 = hash_approval_package(package)
        version_content = {key: value for key, value in package.items() if key != "facts"}
        version_id = _insert_approved_version(conn, job_id, version_content)
        _insert_claim_rows(conn, version_id, package["sections"])
        _insert_fact_snapshot_rows(conn, version_id, package["facts"])
        conn.execute(
            "INSERT INTO workflow_approvals (run_id, draft_revision, content_sha256, version_id) "
            "VALUES (%s, %s, %s, %s)",
            (run_id, draft_revision, content_sha256, version_id),
        )
        conn.execute(
            "INSERT INTO audit_events (event_type, payload) VALUES (%s, %s)",
            ("resume.approved", Jsonb({
                "run_id": run_id,
                "draft_revision": draft_revision,
                "version_id": version_id,
                "content_sha256": content_sha256,
                "fact_count": len(package["facts"]),
            })),
        )

    approval = get_approval(conn, run_id)
    return approval


def set_graph_reconciled(conn: psycopg.Connection, run_id: str) -> None:
    updated = conn.execute(
        "UPDATE workflow_approvals SET graph_reconciled=TRUE WHERE run_id=%s",
        (run_id,),
    )
    if updated.rowcount != 1:
        raise KeyError(f"Approval not found for run {run_id}")
