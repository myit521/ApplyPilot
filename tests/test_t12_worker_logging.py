"""Worker event logs must be traceable without copying private exception text."""

import json
import logging
from contextlib import nullcontext
from unittest.mock import MagicMock

from applypilot.model_adapter import RetryableModelError
from applypilot import task_worker
from applypilot.task_worker import TaskWorker


def _run_with_error(error, caplog):
    def fail(_task):
        raise error

    worker = TaskWorker("postgresql://user:secret@localhost/db", fail)
    settled = []
    worker._settle = lambda *args, **kwargs: settled.append((args, kwargs))
    task = {"run_id": "wf_log_1", "job_id": 42, "attempt_count": 2}
    with caplog.at_level(logging.INFO, logger="applypilot.task_worker"):
        worker._process(task)
    return settled, [json.loads(record.message) for record in caplog.records]


def test_failure_log_has_ids_step_duration_and_type_without_private_text(caplog):
    settled, events = _run_with_error(RuntimeError("candidate@example.test token-secret"), caplog)
    assert settled[0][0] == ("wf_log_1", "failed")
    event = events[-1]
    assert event["event"] == "workflow_task_finished"
    assert event["step"] == "execute_workflow" and event["task_id"] == "wf_log_1"
    assert event["run_id"] == "wf_log_1" and event["job_id"] == 42
    assert event["attempt_count"] == 2 and event["retry_count"] == 1
    assert event["status"] == "failed" and event["error_type"] == "RuntimeError"
    assert event["duration_ms"] >= 0
    assert "candidate@example.test" not in caplog.text
    assert "token-secret" not in caplog.text
    assert "user:secret" not in caplog.text


def test_retry_log_records_safe_status_and_error_type(caplog):
    settled, events = _run_with_error(RetryableModelError("phone 13800000000"), caplog)
    assert settled[0][0] == ("wf_log_1", "retry_wait")
    assert events[-1]["status"] == "retry_wait"
    assert events[-1]["error_type"] == "RetryableModelError"
    assert "13800000000" not in caplog.text


def test_cycle_error_log_does_not_crash_on_incomplete_task(monkeypatch, caplog):
    worker = TaskWorker("postgresql://user:secret@localhost/db", lambda task: "failed")
    monkeypatch.setattr(task_worker.db, "connect", lambda _: nullcontext(None))
    monkeypatch.setattr(task_worker.task_repo, "claim_due_task", lambda _: {"job_id": 42})
    monkeypatch.setattr(worker, "_process", lambda _: (_ for _ in ()).throw(RuntimeError("private")))
    monkeypatch.setattr(worker, "_wait", worker._stop.set)
    with caplog.at_level(logging.ERROR, logger="applypilot.task_worker"):
        worker._loop()
    event = json.loads(caplog.records[-1].message)
    assert event["event"] == "workflow_worker_cycle_failed"
    assert event["job_id"] == 42 and event["run_id"] is None
    assert "private" not in caplog.text


def test_cycle_error_log_ignores_mock_identifiers(monkeypatch, caplog):
    worker = TaskWorker("postgresql://user:secret@localhost/db", lambda task: "failed")
    monkeypatch.setattr(task_worker.db, "connect", lambda _: nullcontext(None))
    monkeypatch.setattr(task_worker.task_repo, "claim_due_task", lambda _: MagicMock())
    monkeypatch.setattr(worker, "_process", lambda _: (_ for _ in ()).throw(RuntimeError("private")))
    monkeypatch.setattr(worker, "_wait", worker._stop.set)
    with caplog.at_level(logging.ERROR, logger="applypilot.task_worker"):
        worker._loop()
    event = json.loads(caplog.records[-1].message)
    assert event["run_id"] is None and event["job_id"] is None
    assert "private" not in caplog.text
