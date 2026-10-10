"""The full synthetic probe stays bounded and never silently drops cases."""

import json

import pytest

from scripts import evaluate_t15_live as evaluation


def test_run_records_all_fixed_cases_with_one_call_each(monkeypatch, tmp_path):
    jd_cases = json.loads(evaluation.JD_FIXTURE.read_text(encoding="utf-8"))["cases"]
    claim_cases = json.loads(evaluation.CLAIM_FIXTURE.read_text(encoding="utf-8"))["cases"]
    jd_by_text = {case["jd_text"]: case for case in jd_cases}
    calls = []

    def fake_request(key, system, user, *, max_tokens):
        calls.append((system, max_tokens))
        if system == evaluation.JD_PROMPT:
            content = json.dumps(jd_by_text[user]["requirements"], ensure_ascii=False)
        else:
            content = '{"violations": []}'
        return {"http_status": 200, "model": "synthetic-stub", "finish_reason": "stop",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "content": content}

    monkeypatch.setattr(evaluation, "request_once", fake_request)
    output = tmp_path / "report.json"
    report = evaluation.run("test-key", output)

    assert len(jd_cases) == 10 and len(claim_cases) == 20
    assert len(calls) == 19
    assert [cap for _, cap in calls] == [1000] * 10 + [300] * 9
    assert len(report["cases"]) == 19
    assert [row["id"] for row in report["cases"][10:]] == [
        "cl01", "cl02", "cl03", "cl04", "cl05", "cl06", "cl08", "cl09", "cl10",
    ]
    assert json.loads(output.read_text(encoding="utf-8"))["cases"] == report["cases"]


def test_existing_report_refuses_new_paid_calls(monkeypatch, tmp_path):
    output = tmp_path / "report.json"
    output.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(evaluation, "request_once",
                        lambda *args, **kwargs: pytest.fail("unexpected model call"))

    with pytest.raises(FileExistsError):
        evaluation.run("test-key", output)
