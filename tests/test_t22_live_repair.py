import json

import pytest

from scripts import evaluate_t22_live_repair as evaluation


def response(content):
    return {"http_status": 200, "model": "stub", "finish_reason": "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "content": json.dumps(content, ensure_ascii=False)}


def test_probe_calls_production_parser_and_caps_all_requests(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "_source_commit", lambda: "fixed")
    calls = []

    def request(key, system, user, *, max_tokens):
        calls.append(max_tokens)
        return response({"job_title": "stub"})

    monkeypatch.setattr(evaluation, "request_once", request)
    report = evaluation.run("test-key", tmp_path / "report.json")
    assert len(calls) == 10
    assert set(calls) == {1000}
    assert report["run"]["request_cap"] == 18
    assert report["run"]["status"] == "complete"
    assert len(report["cases"]) == 10
    assert report["cases"][-1]["seeded_first_response"] is True
    assert report["cases"][-1]["parser_calls"] == 2
    assert report["cases"][-1]["repair_attempted"] is True
    assert report["cases"][-1]["obligation_failures"]


def test_transport_failure_saves_partial_and_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "_source_commit", lambda: "fixed")
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        return {"http_status": 429, "error": "http_error"}

    monkeypatch.setattr(evaluation, "request_once", request)
    output = tmp_path / "report.json"
    report = evaluation.run("test-key", output)
    assert len(calls) == 1
    assert report["run"]["status"] == "partial"
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_existing_report_never_makes_paid_call(monkeypatch, tmp_path):
    output = tmp_path / "report.json"
    output.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(evaluation, "request_once", lambda *a, **k: pytest.fail("paid call"))
    with pytest.raises(FileExistsError):
        evaluation.run("test-key", output)


def test_gate_rejection_is_recorded_and_not_retried_more_than_once(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "_source_commit", lambda: "fixed")
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        return response({"required": ["不要求 Redis"]})

    monkeypatch.setattr(evaluation, "request_once", request)
    report = evaluation.run("test-key", tmp_path / "report.json")
    assert len(calls) == 18
    assert all(row["outcome"] == "rejected" for row in report["cases"])
    assert all(row["parser_calls"] == 2 for row in report["cases"])


def test_technology_obligation_does_not_accept_javascript_for_java():
    observed = {"required": ["JavaScript"], "preferred": [],
                "experience_requirement": "", "keywords": []}
    assert evaluation.obligations({"must_required": ["Java"]}, observed) == ["missing_required:Java"]


def test_unexpected_runner_error_is_not_reported_as_transport(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "_source_commit", lambda: "fixed")

    def request(*args, **kwargs):
        raise TypeError("synthetic programming failure")

    monkeypatch.setattr(evaluation, "request_once", request)
    report = evaluation.run("test-key", tmp_path / "report.json")
    assert report["run"]["status"] == "partial"
    assert report["cases"][0]["outcome"] == "runner_failed"
    assert report["run"]["attempted_requests"] == 1
