"""Build and hash immutable resume approval packages."""

from __future__ import annotations

import hashlib
import json

SECTION_ORDER = ("education", "skills", "experience")
JOB_SNAPSHOT_FIELDS = ("id", "source", "url", "company", "title", "raw_text", "parsed")

SCHEMA_VERSION = 2
PROFILE_DATA_FIELDS = ("name", "email", "phone", "location", "website")


def normalize_profile_snapshot(profile_snapshot: dict | None) -> dict | None:
    """Reduce a confirmed profile row to the frozen revision and contact fields."""
    if profile_snapshot is None:
        return None
    data = profile_snapshot["data"] or {}
    return {
        "revision": int(profile_snapshot["revision"]),
        "data": {field: data.get(field, "") for field in PROFILE_DATA_FIELDS},
    }


def build_approval_package(
    *,
    job: dict,
    sections: dict,
    fact_snapshots: list[dict],
    draft_revision: int,
    profile_snapshot: dict | None = None,
) -> dict:
    normalized_sections = {
        name: [
            {
                "text": claim["text"],
                "fact_ids": list(claim["fact_ids"]),
                "matched_requirements": list(claim.get("matched_requirements", [])),
            }
            for claim in sections.get(name, [])
        ]
        for name in SECTION_ORDER
    }
    cited_ids = {
        fact_id
        for claims in normalized_sections.values()
        for claim in claims
        for fact_id in claim["fact_ids"]
    }

    snapshots_by_id = {}
    for fact in fact_snapshots:
        fact_id = fact["id"]
        if fact_id in snapshots_by_id:
            raise ValueError(f"Duplicate retrieved fact: {fact_id}")
        snapshots_by_id[fact_id] = fact

    missing_ids = sorted(cited_ids - snapshots_by_id.keys())
    if missing_ids:
        raise ValueError(f"Claim references facts not retrieved: {', '.join(missing_ids)}")

    cited_snapshots = [
        {
            "id": fact_id,
            "revision": snapshots_by_id[fact_id]["revision"],
            "snapshot": snapshots_by_id[fact_id]["snapshot"],
        }
        for fact_id in sorted(cited_ids)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "draft_revision": draft_revision,
        "job_snapshot": {field: job[field] for field in JOB_SNAPSHOT_FIELDS},
        "sections": normalized_sections,
        "facts": cited_snapshots,
        "profile_snapshot": normalize_profile_snapshot(profile_snapshot),
    }


def hash_approval_package(package: dict) -> str:
    payload = json.dumps(
        package, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
