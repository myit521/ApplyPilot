import json

import pytest

from scripts import evaluate_t24_retrieval as evaluation


def test_frozen_fixture_has_only_eligible_relevance_labels():
    facts, cases = evaluation.validate(json.loads(evaluation.FIXTURE.read_text(encoding="utf-8")))
    assert len(facts) == 25
    assert len(cases) == 9
    assert cases[-1]["relevant"] == []


def test_draft_fact_cannot_be_labeled_as_relevant():
    data = json.loads(evaluation.FIXTURE.read_text(encoding="utf-8"))
    data["cases"][0]["relevant"][0]["fact_id"] = "f23"
    with pytest.raises(ValueError, match="ineligible"):
        evaluation.validate(data)


def test_recall_counts_unique_hits_and_reports_empty_gold_separately():
    ranked = [{"id": "a", "score": 1}, {"id": "b", "score": 0.5}]
    result = evaluation.metrics(ranked, {"a", "c"}, k=1)
    assert result["recall_at_5"] == 0.5
    assert evaluation.metrics(ranked, set(), k=1)["recall_at_5"] is None


def test_tied_boundary_reports_possible_recall_range():
    ranked = [{"id": "a", "score": 1}, {"id": "b", "score": 0.5},
              {"id": "c", "score": 0.5}]
    result = evaluation.metrics(ranked, {"c"}, k=2)
    assert result["tie_at_boundary"] is True
    assert result["recall_tie_lower"] == 0
    assert result["recall_tie_upper"] == 1


def test_precision_at_five_uses_five_slots_even_with_sparse_results():
    result = evaluation.metrics([{"id": "a", "score": 1}], {"a"})
    assert result["precision_at_5"] == 0.2


def test_existing_report_refuses_model_and_database_work(tmp_path):
    output = tmp_path / "report.json"
    output.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        evaluation.run(tmp_path / "nonexistent-model", output)
