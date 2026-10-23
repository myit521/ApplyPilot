"""Best-effort request logs, not a durable usage ledger or spending limit."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import re
import uuid

log = logging.getLogger(__name__)
_context: ContextVar[dict | None] = ContextVar("model_call_context", default=None)
_STEPS = {"parse_jd", "generate_resume", "semantic_review"}


@contextmanager
def model_call_scope(*, run_id=None, job_id=None, attempt_count=None, step=None):
    context = dict(_context.get() or {})
    if run_id is not None:
        try:
            # Task admission uses wf_ + UUID hex; never allow arbitrary prefixed text.
            candidate = run_id
            if isinstance(run_id, str) and re.fullmatch(r"wf_[0-9a-f]{32}", run_id):
                candidate = run_id[3:]
            uuid.UUID(candidate)
            context["run_id"] = run_id
        except (ValueError, TypeError, AttributeError):
            context["run_id"] = None
    for name, value in (("job_id", job_id), ("attempt_count", attempt_count)):
        if value is not None:
            context[name] = value if type(value) is int and value > 0 else None
    if step is not None:
        context["step"] = step if isinstance(step, str) and step in _STEPS else None
    token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(token)


def current_model_context() -> dict:
    context = _context.get() or {}
    return {key: context.get(key) for key in ("run_id", "job_id", "attempt_count", "step")}


def usage_summary(payload: object) -> dict:
    result = {"usage_status": "unavailable", "prompt_tokens": None,
              "completion_tokens": None, "total_tokens": None}
    if not isinstance(payload, dict):
        return result
    if "usage" not in payload:
        return {**result, "usage_status": "missing"}
    usage = payload["usage"]
    if not isinstance(usage, dict):
        return {**result, "usage_status": "invalid"}
    values = [usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")]
    if (any(type(value) is not int or value < 0 for value in values)
            or values[0] + values[1] != values[2]):
        return {**result, "usage_status": "invalid"}
    return {"usage_status": "reported", "prompt_tokens": values[0],
            "completion_tokens": values[1], "total_tokens": values[2]}


def emit_model_event(event: dict) -> None:
    try:
        log.info(json.dumps(event, separators=(",", ":")))
    except Exception:
        # Logging failures must not cause a retry of an already paid request.
        pass
