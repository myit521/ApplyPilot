"""T17 evidence pointers must fail closed when annotations drift."""

import copy
import json

import pytest

from scripts.review_t17_jd_atoms import ANNOTATIONS, JD_FIXTURE, REPORT, review


def _inputs():
    return (JD_FIXTURE.read_bytes(), json.loads(ANNOTATIONS.read_text(encoding="utf-8")),
            REPORT.read_bytes())


def test_frozen_atoms_are_traceable():
    result = review(*_inputs())
    assert len(result["cases"]) == 10
    assert result["adjudications"] == {"covered": 48, "needs_review": 1}
    assert result["cases"][3]["atoms"][3]["verdict"] == "needs_review"


def test_source_clause_must_exist_in_jd():
    jd_bytes, annotation, report_bytes = _inputs()
    annotation = copy.deepcopy(annotation)
    annotation["cases"][0]["atoms"][0]["source_span"] = "not present"
    with pytest.raises(ValueError, match="invalid source atom"):
        review(jd_bytes, annotation, report_bytes)


def test_wrong_output_field_cannot_be_marked_covered():
    jd_bytes, annotation, report_bytes = _inputs()
    annotation = copy.deepcopy(annotation)
    annotation["cases"][0]["atoms"][0]["observed_field"] = "preferred"
    with pytest.raises(ValueError, match="unjustified covered"):
        review(jd_bytes, annotation, report_bytes)


def test_report_change_is_rejected():
    jd_bytes, annotation, report_bytes = _inputs()
    with pytest.raises(ValueError, match="report changed"):
        review(jd_bytes, annotation, report_bytes + b" ")


def test_nonliteral_atom_needs_a_normalization_note():
    jd_bytes, annotation, report_bytes = _inputs()
    annotation = copy.deepcopy(annotation)
    annotation["cases"][0]["atoms"][0]["atom"] = "岗位：数据库运营"
    with pytest.raises(ValueError, match="missing normalization note"):
        review(jd_bytes, annotation, report_bytes)


def test_removed_atom_is_visible_as_unlinked_output():
    jd_bytes, annotation, report_bytes = _inputs()
    annotation = copy.deepcopy(annotation)
    annotation["cases"][6]["atoms"] = [
        item for item in annotation["cases"][6]["atoms"] if item["atom"] != "Spring Cloud"]
    result = review(jd_bytes, annotation, report_bytes)
    assert {"field": "preferred", "text": "Spring Cloud"} in result["cases"][6]["unlinked_output_items"]
