"""The T18 probe stays capped, resumable by inspection, and fail closed."""

import json

import pytest

from scripts import evaluate_t18_live as evaluation


def _ok_response():
    return {"http_status": 200, "model": "synthetic-stub", "finish_reason": "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "content": '{"job_title":"synthetic"}'}


def test_fixed_twenty_calls_and_complete_report(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "_source_commit", lambda: "fixed-commit")
    calls = []

    def fake_request(key, system, user, *, max_tokens):
        calls.append((user, max_tokens))
        return _ok_response()

    monkeypatch.setattr(evaluation, "request_once", fake_request)
    output = tmp_path / "report.json"
    report = evaluation.run("test-key", output)
    assert len(calls) == 20
    assert {limit for _, limit in calls} == {1000}
    assert report["run"]["status"] == "complete"
    assert report["run"]["attempted_requests"] == 20
    assert report["run"]["source_commit"] == "fixed-commit"
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_existing_report_refuses_new_calls(monkeypatch, tmp_path):
    output = tmp_path / "report.json"
    output.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(evaluation, "request_once",
                        lambda *args, **kwargs: pytest.fail("unexpected model call"))
    with pytest.raises(FileExistsError):
        evaluation.run("test-key", output)


@pytest.mark.parametrize("bad_response", [
    {"http_status": 429, "error": "http_error"},
    {**_ok_response(), "content": "not JSON"},
])
def test_transport_or_parse_error_stops_with_partial_report(monkeypatch, tmp_path, bad_response):
    monkeypatch.setattr(evaluation, "_source_commit", lambda: "fixed-commit")
    calls = []

    def fake_request(*args, **kwargs):
        calls.append(1)
        return bad_response

    monkeypatch.setattr(evaluation, "request_once", fake_request)
    output = tmp_path / "partial.json"
    report = evaluation.run("test-key", output)
    assert len(calls) == 1
    assert report["run"]["status"] == "partial"
    assert report["run"]["attempted_requests"] == 1
    assert "error" in report["cases"][0]
    assert json.loads(output.read_text(encoding="utf-8")) == report
