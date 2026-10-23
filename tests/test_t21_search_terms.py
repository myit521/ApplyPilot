"""Retrieval should use stated requirements and responsibilities, not waived skills."""

import json
from pathlib import Path

import pytest

from applypilot import search
from applypilot.schemas import JobRequirements


REPORT = Path(__file__).resolve().parents[1] / "docs/evaluation/t18-live.json"
CASES = {row["id"]: JobRequirements.model_validate(row["observed"])
         for row in json.loads(REPORT.read_text(encoding="utf-8"))["cases"]}


@pytest.mark.parametrize("case_id,excluded", [
    ("h07", "MySQL"), ("h08", "RabbitMQ"),
    ("h18", "Redis"), ("h19", "LangGraph"),
    ("h20", "GraphQL"),
])
def test_t18_non_requirement_term_is_not_a_retrieval_term(case_id, excluded):
    requirements = CASES[case_id]
    assert excluded not in search.extract_terms(requirements)
    assert excluded not in search.required_skill_terms(requirements)
    assert excluded not in search._query_text(requirements)


def test_responsibility_mentioned_term_remains_available_for_retrieval():
    requirements = CASES["h07"]
    assert "备份与恢复" in search.extract_terms(requirements)
    assert "备份与恢复" in search._query_text(requirements)


def test_legacy_negative_required_text_is_not_in_vector_query():
    assert "不限制实习经历" not in search._query_text(CASES["h12"])


def test_positive_keyword_survives_unrelated_negative_clause():
    requirements = JobRequirements(
        required=["Java 必备", "不要求 Redis"],
        keywords=[{"term": "Java", "importance": "required"},
                  {"term": "Redis", "importance": "required"}],
    )
    assert search.extract_terms(requirements) == ["Java"]
    assert search.required_skill_terms(requirements) == ["Java"]
    assert "Redis" not in search._query_text(requirements)


def test_only_unrelated_mentioned_keyword_produces_no_retrieval_term():
    requirements = JobRequirements(
        keywords=[{"term": "RabbitMQ", "importance": "mentioned"}],
    )
    assert search.extract_terms(requirements) == []
    assert search.required_skill_terms(requirements) == []


def test_preferred_and_responsibility_terms_do_not_become_required_skills():
    requirements = JobRequirements(
        preferred=["Kafka 经验加分"], responsibilities=["负责接口联调"],
        keywords=[{"term": "Kafka", "importance": "preferred"},
                  {"term": "接口联调", "importance": "mentioned"}],
    )
    assert search.extract_terms(requirements) == ["Kafka", "接口联调"]
    assert search.required_skill_terms(requirements) == []


def test_negative_parenthetical_in_responsibility_does_not_retrieve_waived_skill():
    requirements = JobRequirements(
        responsibilities=["负责消息链路（不需要 RabbitMQ）"],
        keywords=[{"term": "RabbitMQ", "importance": "mentioned"}],
    )
    assert search.extract_terms(requirements) == []
    assert "RabbitMQ" not in search._query_text(requirements)
