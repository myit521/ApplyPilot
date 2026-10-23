"""A stated choice between skills must not become an all-skills requirement."""

import json
from pathlib import Path

import pytest

from applypilot.matching import build_match_report
from applypilot.schemas import Fact, JobRequirements, KeywordRequirement


REPORT = Path(__file__).resolve().parents[1] / "docs/evaluation/t18-live.json"


def _fact(skill: str) -> Fact:
    return Fact(id=skill, status="confirmed", fact_type="project",
                source_name="合成项目", content=f"使用 {skill} 完成服务", skills=[skill])


@pytest.mark.parametrize("case_id,sufficient,other", [
    ("h10", "Java", "Kotlin"),
    ("h16", "PostgreSQL", "MySQL"),
])
def test_t18_one_of_clause_is_supported_by_either_skill(case_id, sufficient, other):
    rows = json.loads(REPORT.read_text(encoding="utf-8"))["cases"]
    requirements = JobRequirements.model_validate(next(row["observed"] for row in rows
                                                       if row["id"] == case_id))
    item = build_match_report(requirements, [_fact(sufficient)])["requirements"][0]
    assert item["status"] == "supported"
    assert item["matched_terms"] == [sufficient]
    assert other not in item["matched_terms"]
    assert "任一" in item["reason"]


def test_one_of_clause_without_either_skill_has_no_evidence():
    requirements = JobRequirements(
        required=["Java 或 Kotlin 任一即可"],
        keywords=[KeywordRequirement(term="Java", importance="required"),
                  KeywordRequirement(term="Kotlin", importance="required")],
    )
    item = build_match_report(requirements, [])["requirements"][0]
    assert item["status"] == "no_evidence"


def test_and_clause_still_requires_both_skills():
    requirements = JobRequirements(
        required=["Java 和 Kotlin 都必须掌握"],
        keywords=[KeywordRequirement(term="Java", importance="required"),
                  KeywordRequirement(term="Kotlin", importance="required")],
    )
    item = build_match_report(requirements, [_fact("Java")])["requirements"][0]
    assert item["status"] == "partial"


def test_or_without_explicit_one_of_marker_is_not_auto_supported():
    requirements = JobRequirements(
        required=["Java 或 Kotlin，具体要求待确认"],
        keywords=[KeywordRequirement(term="Java", importance="required"),
                  KeywordRequirement(term="Kotlin", importance="required")],
    )
    item = build_match_report(requirements, [_fact("Java")])["requirements"][0]
    assert item["status"] != "supported"


def test_unrelated_one_of_marker_before_or_does_not_change_requirement():
    requirements = JobRequirements(
        required=["至少一种数据库经验，Java 或 Kotlin 都必须掌握"],
        keywords=[KeywordRequirement(term="Java", importance="required"),
                  KeywordRequirement(term="Kotlin", importance="required")],
    )
    item = build_match_report(requirements, [_fact("Java")])["requirements"][0]
    assert item["status"] == "partial"
