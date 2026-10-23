"""Explicit non-requirements must not become evidence requirements."""

import json
from pathlib import Path

import pytest

from applypilot.jd_parser import JDParseError, parse_jd
from applypilot.matching import build_match_report
from applypilot.schemas import Fact, JobRequirements


REPORT = Path(__file__).resolve().parents[1] / "docs/evaluation/t18-live.json"
CASES = {row["id"]: row for row in json.loads(REPORT.read_text(encoding="utf-8"))["cases"]}


class FixedAdapter:
    def __init__(self, *outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def complete(self, system, user):
        self.calls.append(user)
        return next(self.outputs)


def _response(case_id):
    return json.dumps(CASES[case_id]["observed"], ensure_ascii=False)


@pytest.mark.parametrize("case_id,wrong_text", [
    ("h12", "不限制实习经历"),
    ("h20", "GraphQL 可选"),
])
def test_explicit_negative_or_optional_clause_gets_one_repair(case_id, wrong_text):
    repaired = dict(CASES[case_id]["observed"])
    repaired["required"] = [text for text in repaired["required"] if wrong_text not in text]
    repaired["preferred"] = [text for text in repaired["preferred"] if wrong_text not in text]
    repaired["experience_requirement"] = ""
    if case_id == "h20":
        repaired["keywords"] = [
            {**keyword, "importance": "mentioned"} if keyword["term"] == "GraphQL" else keyword
            for keyword in repaired["keywords"]
        ]
    adapter = FixedAdapter(_response(case_id), json.dumps(repaired, ensure_ascii=False))
    result = parse_jd(CASES[case_id]["jd_text"], adapter)
    assert len(adapter.calls) == 2
    assert "非要求" in adapter.calls[1]
    assert wrong_text not in result.required + result.preferred


def test_optional_keyword_still_preferred_after_clause_repair_fails_closed():
    bad = dict(CASES["h20"]["observed"])
    bad["preferred"] = []
    output = json.dumps(bad, ensure_ascii=False)
    adapter = FixedAdapter(output, output)
    with pytest.raises(JDParseError):
        parse_jd(CASES["h20"]["jd_text"], adapter)
    assert len(adapter.calls) == 2


@pytest.mark.parametrize("case_id,term", [
    ("h07", "MySQL"), ("h08", "RabbitMQ"),
    ("h18", "Redis"), ("h19", "LangGraph"),
])
def test_promoted_non_requirement_keyword_gets_repaired(case_id, term):
    bad = dict(CASES[case_id]["observed"])
    bad["keywords"] = [
        {**keyword, "importance": "required"} if keyword["term"] == term else keyword
        for keyword in bad["keywords"]
    ]
    adapter = FixedAdapter(json.dumps(bad, ensure_ascii=False), _response(case_id))
    result = parse_jd(CASES[case_id]["jd_text"], adapter)
    assert len(adapter.calls) == 2
    assert next(keyword.importance for keyword in result.keywords if keyword.term == term) == "mentioned"


def test_repeat_invalid_polarity_fails_closed():
    adapter = FixedAdapter(_response("h12"), _response("h12"))
    with pytest.raises(JDParseError):
        parse_jd(CASES["h12"]["jd_text"], adapter)
    assert len(adapter.calls) == 2


def test_existing_bad_parse_is_not_shown_as_supported_requirement():
    for case_id, phrase in (("h12", "不限制实习经历"), ("h20", "GraphQL 可选")):
        requirements = JobRequirements.model_validate(CASES[case_id]["observed"])
        report = build_match_report(requirements, [
            Fact(id="synthetic", status="confirmed", fact_type="project",
                 source_name="合成事实", content="GraphQL 实习经历", skills=["GraphQL"]),
        ])
        assert all(phrase not in item["text"] for item in report["requirements"])
        assert any(phrase in item["text"] for item in report["unknowns"])


@pytest.mark.parametrize("case_id,term", [
    ("h07", "MySQL"), ("h08", "RabbitMQ"),
    ("h18", "Redis"), ("h19", "LangGraph"),
])
def test_mentioned_only_technology_is_not_a_requirement(case_id, term):
    requirements = JobRequirements.model_validate(CASES[case_id]["observed"])
    report = build_match_report(requirements, [
        Fact(id="synthetic", status="confirmed", fact_type="project",
             source_name="合成事实", content=f"用过 {term}", skills=[term]),
    ])
    assert all(item["text"] != term for item in report["requirements"])
    assert any(item["text"] == term for item in report["unknowns"])


def test_positive_requirement_with_negation_in_unrelated_clause_still_matches():
    requirements = JobRequirements(
        required=["必须掌握 Java"],
        keywords=[{"term": "Java", "importance": "required"},
                  {"term": "Redis", "importance": "mentioned"}],
    )
    report = build_match_report(requirements, [
        Fact(id="java", status="confirmed", fact_type="project",
             source_name="合成事实", content="使用 Java", skills=["Java"]),
    ])
    assert report["requirements"][0]["status"] == "supported"
    assert any(item["text"] == "Redis" for item in report["unknowns"])


def test_positive_keyword_before_comma_is_not_rejected_by_negative_after_comma():
    output = json.dumps({
        "job_title": "后端开发", "required": ["Java 必备"],
        "keywords": [{"term": "Java", "importance": "required"},
                     {"term": "Redis", "importance": "mentioned"}],
    }, ensure_ascii=False)
    adapter = FixedAdapter(output)
    result = parse_jd("后端开发：Java 必备，不要求 Redis。", adapter)
    assert result.required == ["Java 必备"]
    assert len(adapter.calls) == 1


def test_optional_parameter_is_not_an_optional_job_requirement():
    output = json.dumps({"required": ["熟悉接口可选参数"]}, ensure_ascii=False)
    adapter = FixedAdapter(output)
    assert parse_jd("后端开发：要求熟悉接口可选参数。", adapter).required == ["熟悉接口可选参数"]
    assert len(adapter.calls) == 1


def test_experience_unlimited_is_not_a_required_condition():
    output = json.dumps({"required": ["经验不限"]}, ensure_ascii=False)
    adapter = FixedAdapter(output, output)
    with pytest.raises(JDParseError):
        parse_jd("校招后端：经验不限；Java 必备。", adapter)
    item = build_match_report(JobRequirements(required=["经验不限"]), [])["unknowns"]
    assert any(row["text"] == "经验不限" for row in item)


def test_second_repair_error_names_remaining_keyword_conflict():
    first = _response("h20")
    second = dict(CASES["h20"]["observed"])
    second["preferred"] = []
    adapter = FixedAdapter(first, json.dumps(second, ensure_ascii=False))
    with pytest.raises(JDParseError) as error:
        parse_jd(CASES["h20"]["jd_text"], adapter)
    assert "技术词" in str(error.value)
