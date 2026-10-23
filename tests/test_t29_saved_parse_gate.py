"""Reusing a saved parse must not bypass the current non-requirement gate."""

import pytest

from applypilot.workflow import WorkflowStatus, build_graph


def test_invalid_saved_parse_fails_locally_without_a_model_call():
    class NoModel:
        def complete(self, system, user):
            pytest.fail("invalid saved parse must stop before generation or paid repair")

    graph = build_graph(NoModel(), lambda requirements: [])
    result = graph.invoke({"jd_text": "不要求实习经验。", "job_id": 1,
        "job_snapshot": {"id": 1, "raw_text": "不要求实习经验。", "parsed": {
            "job_title": "合成岗位", "required": ["不要求实习经验"],
            "preferred": [], "responsibilities": [], "keywords": [], "unknowns": []}}})
    assert result["status"] == WorkflowStatus.FAILED
    assert "非要求" in result["error"]


@pytest.mark.parametrize("parsed", [
    [{"job_title": "合成岗位", "required": ["Java"], "keywords": []}],
    "invalid cached value", 7, {"required": "not-a-list"},
])
def test_malformed_saved_parse_does_not_trigger_paid_fallback(parsed):
    class NoModel:
        def complete(self, system, user):
            pytest.fail("malformed saved input must fail locally")

    result = build_graph(NoModel(), lambda requirements: []).invoke({
        "jd_text": "要求 Java。", "job_id": 1,
        "job_snapshot": {"id": 1, "raw_text": "要求 Java。", "parsed": parsed}})
    assert result["status"] == WorkflowStatus.FAILED
