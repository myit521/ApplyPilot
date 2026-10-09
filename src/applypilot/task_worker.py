"""One bounded, database-backed workflow executor for the local app instance."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

from . import db, task_repo
from .model_adapter import RetryableModelError
from .workflow import WorkflowCancelled

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


def _safe_log_id(value: object) -> str | int | None:
    return value if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def _log_task(task: dict, status: str, started: float, error_type: str = "") -> None:
    event = {
        "event": "workflow_task_finished",
        "step": "execute_workflow",
        "task_id": task["run_id"],
        "run_id": task["run_id"],
        "job_id": task["job_id"],
        "attempt_count": task["attempt_count"],
        "retry_count": max(0, task["attempt_count"] - 1),
        "status": status,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "error_type": error_type,
    }
    log.log(logging.WARNING if error_type else logging.INFO,
            json.dumps(event, separators=(",", ":")))


class TaskWorker:
    def __init__(self, dsn: str, execute: Callable[[dict], str], poll_seconds: float = 0.5):
        self.dsn = dsn
        self.execute = execute
        self.poll_seconds = poll_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def start(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            with db.connect(self.dsn) as conn:
                conn.execute(
                    "UPDATE workflow_tasks AS t SET status='completed', error='', "
                    "updated_at=now() WHERE status NOT IN ('completed','cancelled') "
                    "AND EXISTS (SELECT 1 FROM workflow_approvals a WHERE a.run_id=t.run_id)"
                )
                task_repo.recover_running_tasks(conn)
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="applypilot-task-worker", daemon=True
            )
            self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        needs_recovery = False
        while not self._stop.is_set():
            task = None
            try:
                with db.connect(self.dsn) as conn:
                    if needs_recovery:
                        task_repo.recover_running_tasks(conn)
                        needs_recovery = False
                    task = task_repo.claim_due_task(conn)
                if task is None:
                    self._wait()
                    continue
                self._process(task)
            except Exception as exc:
                log.error(json.dumps({
                    "event": "workflow_worker_cycle_failed",
                    "step": "claim_or_settle_task",
                    "task_id": _safe_log_id(task.get("run_id")) if task else None,
                    "run_id": _safe_log_id(task.get("run_id")) if task else None,
                    "job_id": _safe_log_id(task.get("job_id")) if task else None,
                    "error_type": type(exc).__name__,
                }, separators=(",", ":")))
                needs_recovery = True
                self._wait()

    def _process(self, task: dict) -> None:
        started = time.monotonic()
        try:
            status = self.execute(task)
            if self.stopping:
                return  # A new process will reclaim this running task.
            error = "workflow_validation_failed" if status == "failed" else ""
            self._settle(task["run_id"], status, error=error)
            _log_task(task, status, started, error_type=error)
        except WorkflowCancelled:
            if self.stopping:
                return
            self._settle(task["run_id"], "cancelled")
            _log_task(task, "cancelled", started, error_type="WorkflowCancelled")
        except RetryableModelError as exc:
            if self.stopping:
                return
            if task["attempt_count"] < MAX_ATTEMPTS:
                delay = 2 ** (task["attempt_count"] - 1)
                self._settle(
                    task["run_id"], "retry_wait",
                    error="model_temporarily_unavailable",
                    retry_delay_seconds=delay,
                )
                _log_task(task, "retry_wait", started, error_type=type(exc).__name__)
            else:
                self._settle(task["run_id"], "failed", error="model_retry_limit_reached")
                _log_task(task, "failed", started, error_type=type(exc).__name__)
        except Exception as exc:
            if self.stopping:
                return
            self._settle(task["run_id"], "failed", error="workflow_execution_failed")
            _log_task(task, "failed", started, error_type=type(exc).__name__)

    def _settle(
        self, run_id: str, status: str, *, error: str = "",
        retry_delay_seconds: float | None = None,
    ) -> None:
        with db.connect(self.dsn) as conn:
            try:
                task_repo.settle_task(
                    conn, run_id, status, error=error,
                    retry_delay_seconds=retry_delay_seconds,
                )
            except task_repo.InvalidTaskTransition:
                current = task_repo.get_task(conn, run_id)
                if current is None or current["status"] not in ("completed", "cancelled"):
                    raise

    def _wait(self) -> None:
        self._wake.wait(self.poll_seconds)
        self._wake.clear()
