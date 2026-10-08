import pytest

from applypilot.approval_snapshots import build_approval_package, hash_approval_package


def test_approval_package_hash_is_stable_and_binds_approved_content():
    package = {
        "schema_version": 1,
        "draft_revision": 4,
        "job_snapshot": {"id": 7, "title": "Java 后端", "raw_text": "JD 原文"},
        "sections": {
            "education": [],
            "skills": [],
            "experience": [
                {"text": "交付批处理模块", "fact_ids": ["f1"], "matched_requirements": ["Java"]},
            ],
        },
        "facts": [{"id": "f1", "revision": 2, "snapshot": {"content": "批处理"}}],
    }
    reordered = {
        "facts": package["facts"],
        "sections": package["sections"],
        "job_snapshot": package["job_snapshot"],
        "draft_revision": 4,
        "schema_version": 1,
    }
    digest = hash_approval_package(package)
    assert digest == hash_approval_package(reordered)
    assert len(digest) == 64 and digest == digest.lower()

    changed_revision = {**package, "facts": [
        {"id": "f1", "revision": 3, "snapshot": {"content": "批处理"}},
    ]}
    changed_source = {**package, "facts": [
        {"id": "f1", "revision": 2, "snapshot": {"content": "不同来源正文"}},
    ]}
    changed_claim = {**package, "sections": {**package["sections"], "experience": [
        {**package["sections"]["experience"][0], "text": "另一条简历文案"},
    ]}}
    changed_job = {**package, "job_snapshot": {**package["job_snapshot"], "raw_text": "不同 JD"}}
    for changed in (changed_revision, changed_source, changed_claim, changed_job):
        assert hash_approval_package(package) != hash_approval_package(changed)


def test_package_contains_job_and_only_cited_revisions_in_stable_order():
    job = {
        "id": 7,
        "source": "paste",
        "url": "https://example.test/job/7",
        "company": "T7 fixture",
        "title": "Java 后端",
        "raw_text": "职位描述原文",
        "parsed": {"job_title": "Java 后端工程师"},
    }
    sections = {
        "experience": [
            {"text": "交付两项能力", "fact_ids": ["f2"], "matched_requirements": ["Java"]},
            {"text": "维护服务", "fact_ids": ["f1"], "matched_requirements": []},
        ],
        "skills": [],
        "education": [],
    }
    facts = [
        {"id": "f3", "revision": 1, "snapshot": {"content": "未引用"}},
        {"id": "f2", "revision": 4, "snapshot": {"content": "批量接口"}},
        {"id": "f1", "revision": 2, "snapshot": {"content": "Java 服务"}},
    ]

    package = build_approval_package(
        job=job, sections=sections, fact_snapshots=facts, draft_revision=5,
    )

    assert package["job_snapshot"]["raw_text"] == "职位描述原文"
    assert list(package["sections"]) == ["education", "skills", "experience"]
    assert [fact["id"] for fact in package["facts"]] == ["f1", "f2"]
    assert package["facts"][0]["revision"] == 2
    assert hash_approval_package(package) == hash_approval_package(build_approval_package(
        job=job, sections=sections, fact_snapshots=list(reversed(facts)), draft_revision=5,
    ))

    missing = {**sections, "experience": [
        {"text": "未知来源", "fact_ids": ["f4"], "matched_requirements": []},
    ]}
    with pytest.raises(ValueError, match="f4"):
        build_approval_package(job=job, sections=missing, fact_snapshots=facts, draft_revision=5)
