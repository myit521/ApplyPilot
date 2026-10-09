"""FastAPI 接口层。

对应 docs/design.md 第 9 节。应用通过 create_app 工厂注入
数据库连接串、模型适配器和 checkpointer，测试可用假适配器
和临时数据库替换。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
import uuid
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
import psycopg
from psycopg.rows import dict_row

from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, ValidationError as PydanticValidationError

from . import application_repo, approvals_repo, db, facts_repo, search, task_repo
from .matching import build_match_report

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
from .deepseek_adapter import DeepSeekAdapter
from .docx_export import render_docx
from .fact_import import FactImportError, extract_facts
from .jd_parser import JDParseError, parse_jd
from .model_adapter import ModelError
from .task_worker import TaskWorker
from .schemas import Fact, FactType, EvidenceType, ProfileData, JobRequirements, ResumeSections
from .workflow import WorkflowCancelled, WorkflowStatus, build_graph


class FactImportRequest(BaseModel):
    resume_text: str


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)


class FactCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_type: FactType
    source_name: str
    content: str
    school: str = ""
    degree: str = ""
    major: str = ""
    start_date: date | None = None
    end_date: date | None = None
    skills: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    evidence_type: EvidenceType = EvidenceType.SELF_REPORT
    evidence_ref: str = ""


class FactUpdateRequest(RevisionRequest):
    fact_type: FactType | None = None
    source_name: str | None = None
    content: str | None = None
    school: str | None = None
    degree: str | None = None
    major: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    skills: list[str] | None = None
    metrics: list[str] | None = None
    evidence_type: EvidenceType | None = None
    evidence_ref: str | None = None
    enabled: bool | None = None


class ProfileUpdateRequest(RevisionRequest):
    profile: ProfileData


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str
    company: str
    raw_text: str
    source: str = "paste"
    url: str | None = None

    @field_validator("title", "company", "source", "raw_text")
    @classmethod
    def validate_text(cls, value: str, info):
        if info.field_name != "raw_text":
            value = value.strip()
        limit = {"title": 200, "company": 200, "source": 100, "raw_text": 30000}[info.field_name]
        if not value.strip() or len(value) > limit or "\x00" in value:
            raise ValueError(f"{info.field_name} must be nonblank, NUL-free and at most {limit} characters")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None):
        if value is None or not value.strip():
            return None
        if len(value) > 2048 or any(c.isspace() or unicodedata.category(c) == "Cc" for c in value):
            raise ValueError("URL must be at most 2048 characters without whitespace or controls")
        try:
            parts = urlsplit(value)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None:
                raise ValueError("URL must be HTTP(S) with a hostname and without credentials")
        except ValueError as exc:
            raise ValueError("Invalid source URL") from exc
        return value


class WorkflowCreateRequest(BaseModel):
    job_id: int


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    expected_revision: int = Field(ge=1)
    feedback: str = ""


class ResumeTextEdits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    education: list[str]
    skills: list[str]
    experience: list[str]


class DraftEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    sections: ResumeTextEdits


class ApplicationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: StrictInt = Field(gt=0)
    version_id: StrictInt = Field(gt=0)
    channel: str
    status: Literal["unknown", "submitted", "failed"]
    occurred_at: datetime | None = None
    result: str = ""

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 100 or any(ord(char) < 32 for char in value):
            raise ValueError("channel must be 1-100 printable characters")
        return value

    @field_validator("result")
    @classmethod
    def validate_result(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 2000 or "\x00" in value:
            raise ValueError("result must be at most 2000 characters and NUL-free")
        return value

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("occurred_at must include a time zone")
        return value


class ApplicationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(gt=0)
    status: Literal["unknown", "submitted", "failed"] | None = None
    occurred_at: datetime | None = None
    result: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("status cannot be null")
        return value

    @field_validator("result")
    @classmethod
    def validate_result(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("result cannot be null; use an empty string to clear")
        return ApplicationCreateRequest.validate_result(value)

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime | None) -> datetime | None:
        return ApplicationCreateRequest.validate_occurred_at(value)


def create_app(
    dsn: str | None = None,
    adapter=None,
    checkpointer=None,
) -> FastAPI:
    dsn = dsn or db.default_dsn()
    def get_conn():
        return db.connect(dsn)

    def get_adapter():
        nonlocal adapter
        if adapter is None:
            try:
                adapter = DeepSeekAdapter()
            except ModelError as e:
                raise HTTPException(503, str(e)) from e
        return adapter

    owned_connection = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal checkpointer, owned_connection
        owned_connection = None
        injected = checkpointer is not None
        try:
            if not injected:
                try:
                    owned_connection = psycopg.connect(
                        dsn, autocommit=True, prepare_threshold=0,
                        row_factory=dict_row, connect_timeout=3,
                        options="-c statement_timeout=5000",
                    )
                    candidate = PostgresSaver(owned_connection)
                    candidate.setup()
                    checkpointer = candidate
                except psycopg.Error as exc:
                    # Do not log the exception text: it can contain DSN credentials.
                    logging.getLogger(__name__).error(
                        "Checkpoint initialization failed (%s); restart after fixing database",
                        type(exc).__name__,
                    )
                    if owned_connection is not None:
                        owned_connection.close()
                        owned_connection = None
            if checkpointer is not None and not injected:
                try:
                    with get_conn() as conn:
                        if db.schema_ready(conn):
                            worker.start()
                except psycopg.Error:
                    pass
            yield
        finally:
            worker.stop()
            if owned_connection is not None:
                owned_connection.close()
            if not injected:
                checkpointer = None

    app = FastAPI(title="ApplyPilot", lifespan=lifespan)

    @app.middleware("http")
    async def require_initialized_checkpoint(request: Request, call_next):
        path = request.url.path
        if checkpointer is None and (
            path in ("/", "/jobs", "/applications") or path.startswith(("/api/", "/review/"))
        ):
            return JSONResponse({"detail": "database_not_ready"}, status_code=503)
        return await call_next(request)

    @app.get("/health/live")
    def liveness():
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness():
        if checkpointer is None:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        try:
            if owned_connection is not None:
                owned_connection.execute("SELECT 1 FROM checkpoints LIMIT 0")
            with db.connect(dsn, connect_timeout=3, options="-c statement_timeout=2000") as conn:
                row = conn.execute(
                    "SELECT bool_and(to_regclass(name) IS NOT NULL) AS ready "
                    "FROM unnest(ARRAY['facts','jobs','resume_versions','resume_claims',"
                    "'workflow_runs','applications','audit_events']) AS tables(name)"
                ).fetchone()
                if not row["ready"] or not db.schema_ready(conn):
                    return JSONResponse({"status": "not_ready"}, status_code=503)
        except psycopg.Error:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return {"status": "ready"}

    def get_checkpointer():
        if checkpointer is None:
            raise HTTPException(503, "database_not_ready")
        return checkpointer

    def get_graph():
        conn = get_conn()
        retriever = search.PostgresFactRetriever(conn, embedding_provider=get_embeddings())
        return build_graph(get_adapter(), retriever, checkpointer=get_checkpointer())

    _embedding_provider = None

    def get_embeddings():
        """本地嵌入模型；未安装 sentence-transformers 时退化为仅全文检索。"""
        nonlocal _embedding_provider
        if _embedding_provider is None:
            from .embeddings import LocalEmbeddingProvider

            _embedding_provider = LocalEmbeddingProvider()
        return _embedding_provider

    def _execute_task(task: dict) -> str:
        run_id = task["run_id"]
        config = {"configurable": {"thread_id": run_id}}

        with get_conn() as conn:
            if approvals_repo.get_approval(conn, run_id) is not None:
                return "completed"

        def cancelled() -> bool:
            if worker.stopping:
                return True
            with get_conn() as conn:
                row = task_repo.get_task(conn, run_id)
                return row is None or row["cancel_requested"]

        if cancelled():
            raise WorkflowCancelled()
        with get_conn() as conn:
            job = conn.execute(
                "SELECT raw_text FROM jobs WHERE id=%s", (task["job_id"],)
            ).fetchone()
            if job is None:
                return "failed"
            retriever = search.PostgresFactRetriever(
                conn, embedding_provider=get_embeddings()
            )
            graph = build_graph(
                get_adapter(), retriever, checkpointer=get_checkpointer(),
                cancel_requested=cancelled,
            )
            state = graph.get_state(config)
            if not state.values or (not state.next and not state.values.get("status")):
                graph.invoke(
                    {"jd_text": job["raw_text"], "job_id": task["job_id"],
                     "validation_retries": 0},
                    config,
                )
            elif state.next and state.next[0] != "approval":
                graph.invoke(None, config)
            state = graph.get_state(config)

        if cancelled():
            raise WorkflowCancelled()
        if state.values.get("status") == WorkflowStatus.FAILED:
            return "failed"
        if state.values.get("status") == WorkflowStatus.READY_TO_APPLY:
            return "completed"
        if state.next and state.next[0] == "approval":
            return "waiting_approval"
        raise RuntimeError("Workflow ended without a terminal or approval state")

    worker = TaskWorker(dsn, _execute_task)

    # ---------- 事实库 ----------

    @app.post("/api/facts/import")
    def import_facts(req: FactImportRequest) -> dict:
        try:
            facts, skipped = extract_facts(req.resume_text, get_adapter())
        except FactImportError as e:
            raise HTTPException(422, str(e)) from e
        conn = get_conn()
        for fact in facts:
            facts_repo.create_fact(conn, fact)
        return {
            "created": len(facts),
            "skipped": skipped,
            "facts": [f.model_dump(mode="json") for f in facts],
        }

    @app.get("/api/facts")
    def list_facts(fact_type: FactType | None = None, enabled: bool = True) -> list[dict]:
        facts = facts_repo.list_facts(get_conn(), fact_type=fact_type, enabled_only=enabled)
        return [f.model_dump(mode="json") for f in facts]

    @app.exception_handler(facts_repo.RevisionConflict)
    async def revision_conflict(request, exc):
        return JSONResponse({"detail": "Revision changed; reload and review before saving or confirming."}, status_code=409)

    @app.exception_handler(PydanticValidationError)
    async def invalid_fact(request, exc):
        return JSONResponse({"detail": "Invalid or blank fact fields"}, status_code=422)

    @app.post("/api/facts", status_code=201)
    def create_fact(req: FactCreateRequest):
        fact = Fact(id="fact_"+uuid.uuid4().hex, **req.model_dump())
        with get_conn() as conn:
            facts_repo.create_fact(conn, fact)
        return fact.model_dump(mode="json")

    @app.put("/api/facts/{fact_id}")
    def update_fact(fact_id: str, req: FactUpdateRequest):
        changes = req.model_dump(exclude_unset=True, exclude={"expected_revision"})
        with get_conn() as conn:
            try:
                fact = facts_repo.update_fact(conn, fact_id, req.expected_revision, changes)
            except KeyError:
                raise HTTPException(404, "Fact not found")
        return fact.model_dump(mode="json")

    @app.post("/api/facts/{fact_id}/confirm")
    def confirm_fact(fact_id: str, req: RevisionRequest):
        with get_conn() as conn:
            try:
                fact = facts_repo.update_fact(conn, fact_id, req.expected_revision, confirm=True)
            except KeyError:
                raise HTTPException(404, "Fact not found")
        return fact.model_dump(mode="json")

    @app.get("/api/facts/{fact_id}/revisions")
    def fact_revisions(fact_id: str):
        with get_conn() as conn:
            return facts_repo.fact_history(conn, fact_id)

    @app.get("/api/profile")
    def profile():
        with get_conn() as conn:
            return facts_repo.get_profile(conn)

    @app.put("/api/profile")
    def update_profile(req: ProfileUpdateRequest):
        with get_conn() as conn:
            return facts_repo.update_profile(conn, req.expected_revision, req.profile)

    @app.post("/api/profile/confirm")
    def confirm_profile(req: RevisionRequest):
        with get_conn() as conn:
            try:
                return facts_repo.update_profile(conn, req.expected_revision, confirm=True)
            except KeyError:
                raise HTTPException(404, "Save profile before confirming")

    @app.get("/api/profile/revisions")
    def profile_revisions():
        with get_conn() as conn:
            return facts_repo.profile_history(conn)

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request):
        return TEMPLATES.TemplateResponse(request, "profile.html", {})

    # ---------- 职位 ----------

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        return TEMPLATES.TemplateResponse(request, "jobs.html", {})

    @app.post("/api/jobs", status_code=201)
    def create_job(req: JobCreateRequest) -> dict:
        with get_conn() as conn:
            return conn.execute(
                "INSERT INTO jobs (title, company, source, url, raw_text) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (req.title, req.company, req.source, req.url, req.raw_text),
            ).fetchone()

    @app.get("/api/jobs")
    def list_jobs(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)) -> dict:
        with get_conn() as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                total = conn.execute("SELECT count(*) AS total FROM jobs").fetchone()["total"]
                items = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT %s OFFSET %s", (limit, offset)).fetchall()
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int) -> dict:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "职位不存在")
        return row

    @app.post("/api/jobs/{job_id}/parse")
    def parse_job(job_id: int) -> dict:
        row = get_job(job_id)
        try:
            model = get_adapter()
        except HTTPException as exc:
            raise HTTPException(503, "模型尚未配置，职位已保留") from exc
        try:
            requirements = parse_jd(row["raw_text"], model)
        except ModelError as exc:
            raise HTTPException(502, "模型调用失败，职位及已有解析已保留") from exc
        except JDParseError as exc:
            raise HTTPException(422, "模型输出无法解析，职位及已有解析已保留") from exc
        with get_conn() as conn:
            return conn.execute(
                "UPDATE jobs SET parsed=%s WHERE id=%s RETURNING *",
                (requirements.model_dump_json(), job_id),
            ).fetchone()

    @app.get("/api/jobs/{job_id}/match")
    def match_job(job_id: int, include_semantic: bool = False) -> dict:
        conn = get_conn()
        row = conn.execute("SELECT parsed FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"职位 {job_id} 不存在")
        if not row["parsed"]:
            raise HTTPException(409, "该职位尚未成功解析，无法匹配")
        requirements = JobRequirements.model_validate(row["parsed"])

        terms = search.extract_terms(requirements)
        fts = search.fulltext_hits(conn, terms)
        vec: dict[str, float] = {}
        if include_semantic:
            embeddings = get_embeddings()
            embedding = embeddings.embed(search._query_text(requirements))
            if embedding is not None:
                search.backfill_embeddings(conn, embeddings)
                vec = search.vector_hits(conn, embedding)
        hit_ids = set(fts) | set(vec)
        candidates = []
        scored = []
        if hit_ids:
            placeholders = ",".join(["%s"] * len(hit_ids))
            rows = conn.execute(
                f"SELECT * FROM facts WHERE enabled AND status='confirmed' AND id IN ({placeholders})", list(hit_ids)
            ).fetchall()
            facts = [Fact(**{k: r[k] for k in Fact.model_fields if k in r}) for r in rows]
            scored = search.merge_and_score(
                facts,
                required_skills=search.required_skill_terms(requirements),
                fulltext_hits=fts,
                vector_hits=vec,
            )
            candidates = [s.model_dump(mode="json") for s in scored]
        columns = ", ".join(Fact.model_fields)
        with get_conn() as conn:
            fact_rows = conn.execute(
                f"SELECT {columns} FROM facts WHERE enabled AND status='confirmed' ORDER BY id"
            ).fetchall()
        confirmed_facts = [Fact(**{k: r[k] for k in Fact.model_fields if k in r}) for r in fact_rows]
        report = build_match_report(requirements, confirmed_facts)
        evidence_ids = {
            evidence["fact_id"]
            for item in report["requirements"]
            for evidence in item["evidence"]
        }
        semantic_candidates = [
            s.model_dump(mode="json") for s in scored
            if s.fact.id in vec and s.fact.id not in evidence_ids
        ]
        return {
            "terms": terms,
            "candidates": candidates,
            **report,
            "semantic_candidates": semantic_candidates,
        }

    # ---------- 工作流 ----------

    def _approval_summary(approval: dict) -> dict:
        pending = not approval["graph_reconciled"]
        return {
            "run_id": approval["run_id"],
            "status": (WorkflowStatus.APPROVAL_RECONCILIATION_PENDING
                      if pending else WorkflowStatus.READY_TO_APPLY),
            "waiting": False,
            "validation_retries": 0,
            "draft_revision": approval["draft_revision"],
            "error": "",
            "sections": approval["package"]["sections"],
            "previous_sections": None,
            "validation_errors": [],
            "resume_version_id": approval["version_id"],
            "content_sha256": approval["content_sha256"],
            "approval_reconciliation_pending": pending,
        }

    def _sync_approval_run_status(conn, approval: dict) -> None:
        pending = not approval["graph_reconciled"]
        status = (WorkflowStatus.APPROVAL_RECONCILIATION_PENDING
                  if pending else WorkflowStatus.READY_TO_APPLY)
        conn.execute(
            "UPDATE workflow_runs SET current_node=%s, status=%s, error='', updated_at=now() "
            "WHERE id=%s",
            ("approval_reconciliation" if pending else "", status, approval["run_id"]),
        )
        conn.execute(
            "UPDATE workflow_tasks SET status='completed', error='', updated_at=now() "
            "WHERE run_id=%s AND status NOT IN ('completed','cancelled')",
            (approval["run_id"],),
        )

    def _summarize_state(run_id: str) -> dict:
        with get_conn() as conn:
            with approvals_repo.workflow_lock(conn, run_id):
                approval = approvals_repo.get_approval(conn, run_id)
                if approval is not None:
                    _sync_approval_run_status(conn, approval)
                    return _approval_summary(approval)
                task = task_repo.get_task(conn, run_id)

        def task_only_summary() -> dict:
            return {
                "run_id": run_id,
                "status": task["status"].upper(),
                "task_status": task["status"],
                "waiting": False,
                "validation_retries": 0,
                "draft_revision": 0,
                "error": task["error"],
                "sections": None,
                "previous_sections": None,
                "validation_errors": [],
            }

        if task is not None and task["status"] in (
            "queued", "running", "retry_wait", "failed", "cancelled"
        ):
            return task_only_summary()

        graph = get_graph()
        config = {"configurable": {"thread_id": run_id}}
        state = graph.get_state(config)
        if not state.values:
            if task is not None:
                return task_only_summary()
            raise HTTPException(404, f"工作流 {run_id} 不存在")
        values: dict[str, Any] = state.values
        resume = values.get("resume")
        previous_resume = values.get("previous_resume")
        summary = {
            "run_id": run_id,
            "status": values.get("status"),
            "waiting": bool(state.next),
            "validation_retries": values.get("validation_retries", 0),
            "draft_revision": values.get("draft_revision", 1 if resume else 0),
            "error": (task["error"] if task is not None and task["error"]
                      else values.get("error", "")),
            "sections": resume.model_dump(mode="json") if resume else None,
            "previous_sections": previous_resume.model_dump(mode="json") if previous_resume else None,
            "validation_errors": [
                e.model_dump(mode="json") for e in values.get("validation_errors", [])
            ],
        }
        if task is not None:
            summary["task_status"] = task["status"]
            if task["status"] in ("queued", "running", "retry_wait", "failed"):
                summary["status"] = task["status"].upper()
        with get_conn() as conn:
            with approvals_repo.workflow_lock(conn, run_id):
                approval = approvals_repo.get_approval(conn, run_id)
                if approval is not None:
                    _sync_approval_run_status(conn, approval)
                    return _approval_summary(approval)
                conn.execute(
                    "INSERT INTO workflow_runs (id, current_node, status, retry_count, error) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET current_node = EXCLUDED.current_node, "
                    "status = EXCLUDED.status, retry_count = EXCLUDED.retry_count, "
                    "error = EXCLUDED.error, updated_at = now()",
                    (
                        run_id,
                        state.next[0] if state.next else "",
                        str(summary["status"]),
                        summary["validation_retries"],
                        summary["error"],
                    ),
                )
        return summary

    @app.post("/api/workflows", status_code=202)
    def create_workflow(
        req: WorkflowCreateRequest,
        idempotency_key: str | None = Header(default=None),
    ) -> dict:
        if idempotency_key:
            if (len(idempotency_key) > 128 or not idempotency_key.strip()
                    or any(ord(c) < 32 for c in idempotency_key)):
                raise HTTPException(422, "Idempotency-Key must be 1-128 printable characters")
        try:
            with get_conn() as conn:
                reservation = task_repo.reserve_workflow_task(
                    conn, job_id=req.job_id, idempotency_key=idempotency_key
                )
        except task_repo.WorkflowJobNotFound as exc:
            raise HTTPException(404, f"职位 {req.job_id} 不存在") from exc
        except task_repo.WorkflowTaskConflict as exc:
            raise HTTPException(409, str(exc)) from exc

        worker.start()
        worker.wake()
        if reservation["created"]:
            return {"run_id": reservation["run_id"], "status": "QUEUED",
                    "task_status": "queued"}
        return _summarize_state(reservation["run_id"])

    @app.get("/api/workflows/{run_id}")
    def get_workflow(run_id: str) -> dict:
        return _summarize_state(run_id)

    @app.post("/api/workflows/{run_id}/cancel")
    def cancel_workflow(run_id: str) -> dict:
        with get_conn() as conn:
            with approvals_repo.workflow_lock(conn, run_id):
                task = task_repo.get_task(conn, run_id)
                if task is None:
                    raise HTTPException(404, f"工作流 {run_id} 不存在")
                if approvals_repo.get_approval(conn, run_id) is not None:
                    raise HTTPException(409, "已批准的简历版本不能取消")
                if task["status"] in ("completed", "failed"):
                    raise HTTPException(409, "任务已结束，不能取消")
                task_repo.request_cancel(conn, run_id)
        worker.wake()
        return _summarize_state(run_id)

    def _pending_approval_response(conn, approval: dict) -> JSONResponse:
        _sync_approval_run_status(conn, approval)
        return JSONResponse(status_code=202, content=_approval_summary(approval))

    def _reconcile_approval(graph, conn, config: dict, approval: dict) -> dict | JSONResponse:
        try:
            state = graph.get_state(config)
            if not state.values:
                return _pending_approval_response(conn, approval)
            if state.values.get("status") == WorkflowStatus.READY_TO_APPLY:
                approvals_repo.set_graph_reconciled(conn, approval["run_id"])
                approval["graph_reconciled"] = True
                _sync_approval_run_status(conn, approval)
                return _approval_summary(approval)
            current_revision = state.values.get("draft_revision", 1)
            if (not state.next or state.next[0] != "approval"
                    or current_revision != approval["draft_revision"]):
                return _pending_approval_response(conn, approval)

            graph.invoke(Command(resume={"approved": True, "feedback": ""}), config)
            state = graph.get_state(config)
            if state.values and state.values.get("status") == WorkflowStatus.READY_TO_APPLY:
                approvals_repo.set_graph_reconciled(conn, approval["run_id"])
                approval["graph_reconciled"] = True
                _sync_approval_run_status(conn, approval)
                return _approval_summary(approval)
        except Exception:
            return _pending_approval_response(conn, approval)
        return _pending_approval_response(conn, approval)

    def _validate_retrieved_facts(conn, state) -> None:
        for snapshot in state.values.get("retrieved_facts", []):
            fact = facts_repo.get_fact(conn, snapshot.id)
            if (fact is None or not fact.enabled or fact.status != "confirmed"
                    or fact.revision != snapshot.revision):
                raise HTTPException(
                    409,
                    "Facts changed; create a new workflow to retrieve current confirmed facts",
                )

    @app.post("/api/workflows/{run_id}/approve", response_model=None)
    def approve_workflow(run_id: str, req: ApprovalRequest) -> dict | JSONResponse:
        config = {"configurable": {"thread_id": run_id}}
        rejected = False
        with get_conn() as conn:
            with approvals_repo.workflow_lock(conn, run_id):
                approval = approvals_repo.get_approval(conn, run_id)
                if approval is not None:
                    if not req.approved or req.expected_revision != approval["draft_revision"]:
                        raise HTTPException(409, "该工作流已批准，不能修改审批决定")
                    if approval["graph_reconciled"]:
                        _sync_approval_run_status(conn, approval)
                        return _approval_summary(approval)
                    try:
                        graph = get_graph()
                    except Exception:
                        return _pending_approval_response(conn, approval)
                    return _reconcile_approval(graph, conn, config, approval)

                task = task_repo.get_task(conn, run_id)
                if task is not None and task["cancel_requested"]:
                    raise HTTPException(409, "该工作流已取消")

                graph = get_graph()
                state = graph.get_state(config)
                if not state.values:
                    raise HTTPException(404, f"工作流 {run_id} 不存在")
                if not state.next or state.next[0] != "approval":
                    raise HTTPException(409, "工作流当前不在等待审批状态")
                current_revision = state.values.get("draft_revision", 1)
                if req.expected_revision != current_revision:
                    raise HTTPException(409, "简历草稿已更新，请刷新后重新审核")

                if not req.approved:
                    _validate_retrieved_facts(conn, state)
                    graph.invoke(
                        Command(resume={"approved": False, "feedback": req.feedback}),
                        config,
                    )
                    rejected = True
                else:
                    try:
                        approval = approvals_repo.persist_approval(
                            conn,
                            run_id=run_id,
                            draft_revision=req.expected_revision,
                            job_id=state.values["job_id"],
                            sections=state.values["resume"].model_dump(mode="json"),
                            retrieved_facts=state.values["retrieved_facts"],
                        )
                    except approvals_repo.ApprovalConflict as exc:
                        raise HTTPException(409, str(exc)) from exc
                    return _reconcile_approval(graph, conn, config, approval)

        if rejected:
            return _summarize_state(run_id)
        raise HTTPException(500, "审批流程未完成")

    @app.post("/api/workflows/{run_id}/edit")
    def edit_workflow_draft(run_id: str, req: DraftEditRequest) -> dict:
        config = {"configurable": {"thread_id": run_id}}
        with get_conn() as conn:
            with approvals_repo.workflow_lock(conn, run_id):
                if approvals_repo.get_approval(conn, run_id) is not None:
                    raise HTTPException(409, "该工作流已批准，不能再编辑")
                task = task_repo.get_task(conn, run_id)
                if task is not None and task["cancel_requested"]:
                    raise HTTPException(409, "该工作流已取消")
                graph = get_graph()
                state = graph.get_state(config)
                if not state.values:
                    raise HTTPException(404, f"工作流 {run_id} 不存在")
                if not state.next or state.next[0] != "approval":
                    raise HTTPException(409, "工作流当前不在等待审核状态")
                current_revision = state.values.get("draft_revision", 1)
                if req.expected_revision != current_revision:
                    raise HTTPException(409, "简历草稿已更新，请刷新后重新编辑")

                current = state.values["resume"]
                edited_sections = {}
                for section in ("education", "skills", "experience"):
                    claims = getattr(current, section)
                    texts = getattr(req.sections, section)
                    if len(texts) != len(claims):
                        raise HTTPException(422, f"{section} 分区的主张数量不能变更")
                    edited_sections[section] = [
                        claim.model_copy(update={"text": text})
                        for claim, text in zip(claims, texts, strict=True)
                    ]
                edited = ResumeSections(**edited_sections)
                _validate_retrieved_facts(conn, state)
                graph.invoke(
                    Command(resume={"edited_sections": edited.model_dump(mode="json")}),
                    config,
                )
        return _summarize_state(run_id)

    # ---------- 简历版本 ----------

    @app.get("/api/resume-versions/{version_id}/docx")
    def export_docx(version_id: int) -> Response:
        with get_conn() as conn:
            version = conn.execute(
                "SELECT rv.content, j.title FROM resume_versions rv "
                "JOIN jobs j ON j.id = rv.job_id "
                "WHERE rv.id = %s AND rv.status = 'approved'",
                (version_id,),
            ).fetchone()
        if version is None:
            raise HTTPException(404, f"简历版本 {version_id} 不存在")
        saved = version["content"]
        job_title = (saved.get("job_snapshot") or {}).get("title") or version["title"] or "未命名职位"
        sections = ResumeSections.model_validate(saved["sections"])
        frozen_profile = saved.get("profile_snapshot")
        data = render_docx(job_title, sections,
                           profile=frozen_profile["data"] if frozen_profile else None)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="resume-{version_id}.docx"'},
        )

    # ---------- 审核页面 ----------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        conn = get_conn()
        runs = conn.execute(
            "SELECT wr.id, CASE WHEN a.run_id IS NOT NULL THEN wr.status "
            "WHEN t.run_id IS NOT NULL THEN upper(t.status) ELSE wr.status END AS status, "
            "wr.retry_count, GREATEST(wr.updated_at, COALESCE(t.updated_at, wr.updated_at)) "
            "AS updated_at FROM workflow_runs wr "
            "LEFT JOIN workflow_tasks t ON t.run_id=wr.id "
            "LEFT JOIN workflow_approvals a ON a.run_id=wr.id "
            "ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
        jobs = conn.execute(
            "SELECT id, title, parsed IS NOT NULL AS parsed, created_at "
            "FROM jobs ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return TEMPLATES.TemplateResponse(
            request, "index.html", {"runs": runs, "jobs": jobs}
        )

    @app.get("/review/{run_id}", response_class=HTMLResponse)
    def review(request: Request, run_id: str) -> HTMLResponse:
        summary = _summarize_state(run_id)
        with get_conn() as conn:
            with approvals_repo.workflow_lock(conn, run_id):
                approval = approvals_repo.get_approval(conn, run_id)
                if approval is not None:
                    _sync_approval_run_status(conn, approval)
                    summary = _approval_summary(approval)
            frozen_facts = ({fact["id"]: fact["snapshot"]
                             for fact in approval["package"]["facts"]}
                            if approval is not None else {})

            def with_facts(claims: list[dict]) -> list[dict]:
                result = []
                for claim in claims:
                    cited = []
                    for fid in claim["fact_ids"]:
                        if approval is not None:
                            fact = frozen_facts.get(fid)
                            if fact is not None:
                                cited.append(fact)
                        else:
                            fact = facts_repo.get_fact(conn, fid)
                            if fact:
                                cited.append(fact.model_dump(mode="json"))
                    result.append({**claim, "facts": cited})
                return result

            sections = summary["sections"] or {}
            previous_sections = summary["previous_sections"] or {}
            section_titles = {"education": "教育背景", "skills": "专业技能", "experience": "工作与项目经历"}
            changes = []
            for name, title in section_titles.items():
                before = previous_sections.get(name, [])
                after = sections.get(name, [])
                for index in range(max(len(before), len(after))):
                    old_text = before[index]["text"] if index < len(before) else ""
                    new_text = after[index]["text"] if index < len(after) else ""
                    if old_text != new_text:
                        changes.append({"section": title, "before": old_text, "after": new_text})
            context = {
                name: with_facts(sections.get(name, []))
                for name in ("education", "skills", "experience")
            }
            profile_row = conn.execute("SELECT status FROM profile WHERE id=1").fetchone()
            total = sum(len(v) for v in context.values())
            return TEMPLATES.TemplateResponse(
                request, "review.html", {
                    "run_id": run_id, "sections": context, "total": total,
                    "draft_revision": summary["draft_revision"], "changes": changes,
                    "validation_errors": summary["validation_errors"],
                    "status": summary["status"],
                    "can_review": summary["status"] == WorkflowStatus.WAITING_APPROVAL and summary["waiting"],
                    "resume_version_id": summary.get("resume_version_id"),
                    "approval_reconciliation_pending": summary.get("approval_reconciliation_pending", False),
                    "profile_confirmed": bool(profile_row and profile_row["status"] == "confirmed"),
                    "profile_in_version": bool(approval and approval["package"].get("profile_snapshot")),
                }
            )

    # ---------- 投递记录 ----------

    @app.get("/applications", response_class=HTMLResponse)
    def applications_page(request: Request, version_id: int | None = Query(default=None, gt=0)) -> HTMLResponse:
        with get_conn() as conn:
            approved_versions = conn.execute(
                "SELECT wa.version_id, rv.job_id, j.company, j.title "
                "FROM workflow_approvals wa "
                "JOIN resume_versions rv ON rv.id=wa.version_id "
                "JOIN jobs j ON j.id=rv.job_id "
                "ORDER BY wa.approved_at DESC LIMIT 100"
            ).fetchall()
        return TEMPLATES.TemplateResponse(request, "applications.html", {
            "approved_versions": approved_versions, "selected_version_id": version_id,
        })

    @app.get("/api/applications")
    def list_applications() -> list[dict]:
        with get_conn() as conn:
            return application_repo.list_applications(conn)

    @app.get("/api/applications/{application_id}")
    def get_application(application_id: int) -> dict:
        with get_conn() as conn:
            record = application_repo.get_application(conn, application_id)
        if record is None:
            raise HTTPException(404, "Application record not found")
        return record

    @app.post("/api/applications", status_code=201)
    def create_application(req: ApplicationCreateRequest,
                           idempotency_key: str = Header()) -> dict:
        if (len(idempotency_key) > 128 or not idempotency_key.strip()
                or any(ord(char) < 32 for char in idempotency_key)):
            raise HTTPException(422, "Idempotency-Key must be 1-128 printable characters")
        try:
            with get_conn() as conn:
                return application_repo.create_application(
                    conn, job_id=req.job_id, version_id=req.version_id,
                    channel=req.channel, status=req.status,
                    occurred_at=req.occurred_at, result=req.result,
                    idempotency_key=idempotency_key,
                )
        except application_repo.ApplicationConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.patch("/api/applications/{application_id}")
    def update_application(application_id: int, req: ApplicationUpdateRequest) -> dict:
        if not req.model_fields_set.intersection({"status", "occurred_at", "result"}):
            raise HTTPException(422, "At least one result field is required")
        try:
            with get_conn() as conn:
                current = application_repo.get_application(conn, application_id)
                if current is None:
                    raise application_repo.ApplicationNotFound(
                        f"application {application_id} does not exist"
                    )
                return application_repo.update_application(
                    conn, application_id, expected_revision=req.expected_revision,
                    status=req.status if req.status is not None else current["status"],
                    occurred_at=(req.occurred_at if "occurred_at" in req.model_fields_set
                                 else current["occurred_at"]),
                    result=req.result if req.result is not None else current["result"],
                )
        except application_repo.ApplicationNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except application_repo.ApplicationConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    return app


app = create_app()
