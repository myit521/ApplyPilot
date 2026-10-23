"""The narrow keyword check must reject fixture drift and misleading rows."""

import copy
import hashlib
import json

import pytest

from scripts.evaluate_t16_keywords import GOLD, JD_FIXTURE, REPORT, evaluate


def _inputs():
    return (JD_FIXTURE.read_bytes(), json.loads(GOLD.read_text(encoding="utf-8")),
            REPORT.read_bytes())


def test_frozen_report_and_annotations_cover_all_ten_jds():
    result = evaluate(*_inputs())
    assert len(result["cases"]) == 10
    assert result["named_terms"] == 22
    assert result["found"] == 22
    assert result["correct_importance"] == 22
    assert result["cases"][8]["named_technologies"] == []
    assert "后端" in result["cases"][8]["other_extracted_terms_for_manual_review"]


def test_fixture_drift_is_rejected():
    jd_bytes, gold, report = _inputs()
    with pytest.raises(ValueError, match="fixture changed"):
        evaluate(jd_bytes + b" ", gold, report)


def test_unfounded_annotation_is_rejected():
    jd_bytes, gold, report = _inputs()
    gold = copy.deepcopy(gold)
    gold["cases"][0]["terms"][0]["source_span"] = "not in JD"
    with pytest.raises(ValueError, match="invalid named-technology annotation"):
        evaluate(jd_bytes, gold, report)


def test_wrong_importance_is_reported_without_changing_gold():
    jd_bytes, gold, report_bytes = _inputs()
    report = json.loads(report_bytes)
    first = next(row for row in report["cases"] if row["id"] == "jd01")
    first["observed"]["keywords"][0]["importance"] = "preferred"
    gold = copy.deepcopy(gold)
    modified_report = json.dumps(report, ensure_ascii=False).encode("utf-8")
    gold["source_report_sha256"] = hashlib.sha256(modified_report).hexdigest()
    result = evaluate(jd_bytes, gold, modified_report)
    assert result["found"] == 22
    assert result["correct_importance"] == 21
    assert result["cases"][0]["named_technologies"][0]["observed_importance"] == "preferred"


def test_report_drift_is_rejected():
    jd_bytes, gold, report_bytes = _inputs()
    with pytest.raises(ValueError, match="report changed"):
        evaluate(jd_bytes, gold, report_bytes + b" ")
