"""The T11 report must keep rule evidence separate from semantic labels."""

import json
from pathlib import Path

import pytest

from scripts.evaluate_t11 import evaluate


def _fact():
    return {"id": "f1", "fact_type": "project", "source_name": "合成项目",
            "content": "参与 Java 服务开发", "skills": ["Java"], "status": "confirmed"}


def _jd_fixture():
    return {"schema_version": 1, "facts": [_fact()], "cases": [{
        "id": "jd1", "jd_text": "要求熟悉 Java",
        "requirements": {"required": ["熟悉 Java"],
                         "keywords": [{"term": "Java", "importance": "required"}]},
        "expected_requirements": [{"category": "required", "text": "熟悉 Java",
                                   "expected_status": "supported", "reason": "事实含 Java",
                                   "supporting_fact_ids": ["f1"]}],
    }]}


def _claim_fixture():
    return {"schema_version": 1, "cases": [
        {"id": "supported", "facts": [_fact()],
         "claim": {"text": "参与 Java 服务开发", "fact_ids": ["f1"]},
         "label": "supported", "reason": "原文支持", "expected_error_codes": []},
        {"id": "semantic", "facts": [_fact()],
         "claim": {"text": "独立设计 Java 架构", "fact_ids": ["f1"]},
         "label": "overclaim", "reason": "参与不等于独立设计", "expected_error_codes": []},
        {"id": "rule", "facts": [_fact()],
         "claim": {"text": "使用 Kubernetes", "fact_ids": ["f1"]},
         "label": "unsupported", "reason": "未有 Kubernetes", "expected_error_codes": ["UNSUPPORTED_SKILL"]},
    ]}


def test_report_separates_keyword_status_rule_gate_and_semantic_gap():
    report = evaluate(_jd_fixture(), _claim_fixture())
    assert report["matching"]["status_accuracy"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert report["matching"]["evidence_recall"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert report["matching"]["evidence_precision"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert report["claims"]["rule_detectable_violation_recall"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert report["claims"]["expected_code_recall"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert report["claims"]["semantic_overclaim_cases"] == ["semantic"]
    assert report["claims"]["supported_false_blocks"] == {"numerator": 0, "denominator": 1, "value": 0.0}
    assert report["claims"]["cases"][1]["observed_error_codes"] == []


def test_zero_denominator_is_not_reported_as_perfect_score():
    jd = _jd_fixture()
    jd["cases"][0]["requirements"] = {"required": ["沟通能力"]}
    jd["cases"][0]["expected_requirements"] = [
        {"category": "required", "text": "沟通能力", "expected_status": "unknown",
         "reason": "无可核关键词", "supporting_fact_ids": []}
    ]
    claims = {"schema_version": 1, "cases": [{
        "id": "uncertain", "facts": [_fact()],
        "claim": {"text": "改进协作效率", "fact_ids": ["f1"]},
        "label": "uncertain", "reason": "需人工核对", "expected_error_codes": [],
    }]}
    report = evaluate(jd, claims)
    assert report["matching"]["evidence_recall"]["value"] is None
    assert report["claims"]["rule_detectable_violation_recall"]["value"] is None
    assert report["claims"]["supported_false_blocks"]["value"] is None
    assert report["claims"]["manual_review_cases"] == ["uncertain"]


def test_wrong_blocking_reason_does_not_count_as_expected_rule_detected():
    claims = _claim_fixture()
    claims["cases"][2]["claim"]["fact_ids"] = []
    report = evaluate(_jd_fixture(), claims)
    assert report["claims"]["rule_detectable_violation_recall"]["value"] == 1.0
    assert report["claims"]["expected_code_recall"] == {"numerator": 0, "denominator": 1, "value": 0.0}


def test_extra_keyword_fact_reduces_evidence_precision():
    jd = _jd_fixture()
    jd["facts"].append({**_fact(), "id": "f2", "content": "只读 Java 文档"})
    report = evaluate(jd, _claim_fixture())
    assert report["matching"]["evidence_recall"]["value"] == 1.0
    assert report["matching"]["evidence_precision"] == {"numerator": 1, "denominator": 2, "value": 0.5}


def test_checked_in_gold_set_is_complete_and_evaluable():
    root = Path(__file__).resolve().parent / "fixtures"
    jd = json.loads((root / "t11_jds.json").read_text(encoding="utf-8"))
    claims = json.loads((root / "t11_claims.json").read_text(encoding="utf-8"))
    report = evaluate(jd, claims)
    assert report["dataset"]["jd_cases"] >= 10
    assert report["dataset"]["claim_cases"] >= 20
    assert {case["label"] for case in report["claims"]["cases"]} == {
        "supported", "overclaim", "unsupported", "uncertain"
    }


@pytest.mark.parametrize("change", [
    lambda jd: jd["cases"].append(dict(jd["cases"][0])),
    lambda jd: jd["cases"][0]["expected_requirements"][0].pop("reason"),
    lambda jd: jd["cases"][0]["expected_requirements"][0].update(supporting_fact_ids=["missing"]),
])
def test_malformed_gold_fails_closed(change):
    jd = _jd_fixture()
    change(jd)
    with pytest.raises(ValueError):
        evaluate(jd, _claim_fixture())
