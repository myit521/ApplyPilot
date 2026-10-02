"""FastAPI 接口层。

对应 docs/design.md 第 9 节。应用通过 create_app 工厂注入
数据库连接串、模型适配器和 checkpointer，测试可用假适配器
和临时数据库替换。
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
import psycopg
from psycopg.rows import dict_row

from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from datetime import date
from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError

from . import db, facts_repo, search

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
from .deepseek_adapter import DeepSeekAdapter
from .docx_export import render_docx
from .fact_import import FactImportError, extract_facts
from .jd_parser import JDParseError, parse_jd
from .model_adapter import ModelError
from .schemas import Fact, FactType, EvidenceType, ProfileData, JobRequirements, ResumeSections
from .workflow import WorkflowStatus, build_graph


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
    raw_text: str
    source: str = "paste"
    company: str = ""
    url: str | None = None


class WorkflowCreateRequest(BaseModel):
    job_id: int


class ApprovalRequest(BaseModel):
    approved: bool
    feedback: str = ""


def create_app(
    dsn: str | None = None,
    adapter=None,
    checkpointer=None,
) -> FastAPI:
    dsn = dsn or db.default_dsn()
    # 工作流线程的异常登记表：run_id -> 错误信息
    run_errors: dict[str, str] = {}

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
            yield
        finally:
            if owned_connection is not None:
                owned_connection.close()
            if not injected:
                checkpointer = None

    app = FastAPI(title="ApplyPilot", lifespan=lifespan)

    @app.middleware("http")
    async def require_initialized_checkpoint(request: Request, call_next):
        path = request.url.path
        if checkpointer is None and (
            path == "/" or path.startswith(("/api/", "/review/"))
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

    @app.post("/api/jobs", status_code=201)
    def create_job(req: JobCreateRequest) -> dict:
        conn = get_conn()
        row = conn.execute(
            "INSERT INTO jobs (source, url, company, raw_text) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (req.source, req.url, req.company, req.raw_text),
        ).fetchone()
        job_id = row["id"]

        try:
            requirements = parse_jd(req.raw_text, get_adapter())
        except JDParseError as e:
            # 解析失败不阻断保存，允许用户修正原文后重试（第 10 节）
            return {"id": job_id, "parsed": None, "parse_error": str(e)}
        conn.execute(
            "UPDATE jobs SET title = %s, parsed = %s WHERE id = %s",
            (requirements.job_title, requirements.model_dump_json(), job_id),
        )
        return {"id": job_id, "parsed": requirements.model_dump(mode="json"), "parse_error": None}

    @app.get("/api/jobs/{job_id}/match")
    def match_job(job_id: int) -> dict:
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
        embedding = get_embeddings().embed(search._query_text(requirements))
        if embedding is not None:
            search.backfill_embeddings(conn, get_embeddings())
            vec = search.vector_hits(conn, embedding)
        hit_ids = set(fts) | set(vec)
        if not hit_ids:
            return {"terms": terms, "candidates": []}
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
        return {
            "terms": terms,
            "candidates": [s.model_dump(mode="json") for s in scored],
        }

    # ---------- 工作流 ----------

    def _summarize_state(run_id: str) -> dict:
        graph = get_graph()
        config = {"configurable": {"thread_id": run_id}}
        state = graph.get_state(config)
        if not state.values:
            raise HTTPException(404, f"工作流 {run_id} 不存在")
        values: dict[str, Any] = state.values
        resume = values.get("resume")
        summary = {
            "run_id": run_id,
            "status": values.get("status"),
            "waiting": bool(state.next),
            "validation_retries": values.get("validation_retries", 0),
            "error": values.get("error") or run_errors.get(run_id, ""),
            "sections": resume.model_dump(mode="json") if resume else None,
            "validation_errors": [
                e.model_dump(mode="json") for e in values.get("validation_errors", [])
            ],
        }
        conn = get_conn()
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

    def _run_workflow(run_id: str, jd_text: str, job_id: int) -> None:
        try:
            graph = get_graph()
            config = {"configurable": {"thread_id": run_id}}
            graph.invoke(
                {"jd_text": jd_text, "job_id": job_id, "validation_retries": 0},
                config,
            )
        except Exception as e:  # 线程内异常登记，GET 时可见
            run_errors[run_id] = str(e)

    @app.post("/api/workflows", status_code=202)
    def create_workflow(
        req: WorkflowCreateRequest,
        idempotency_key: str | None = Header(default=None),
    ) -> dict:
        conn = get_conn()
        row = conn.execute("SELECT raw_text FROM jobs WHERE id = %s", (req.job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"职位 {req.job_id} 不存在")

        run_id = f"wf_{idempotency_key}" if idempotency_key else f"wf_{uuid.uuid4().hex[:12]}"
        if idempotency_key:
            existing = conn.execute(
                "SELECT id FROM workflow_runs WHERE id = %s", (run_id,)
            ).fetchone()
            if existing:
                return _summarize_state(run_id)

        thread = threading.Thread(
            target=_run_workflow, args=(run_id, row["raw_text"], req.job_id), daemon=True
        )
        thread.start()
        conn.execute(
            "INSERT INTO workflow_runs (id, current_node, status, input_summary) "
            "VALUES (%s, 'parse_jd', %s, %s) ON CONFLICT (id) DO NOTHING",
            (run_id, WorkflowStatus.PARSING_JD, row["raw_text"][:200]),
        )
        return {"run_id": run_id, "status": WorkflowStatus.PARSING_JD}

    @app.get("/api/workflows/{run_id}")
    def get_workflow(run_id: str) -> dict:
        return _summarize_state(run_id)

    @app.post("/api/workflows/{run_id}/approve")
    def approve_workflow(run_id: str, req: ApprovalRequest) -> dict:
        graph = get_graph()
        config = {"configurable": {"thread_id": run_id}}
        state = graph.get_state(config)
        if not state.values:
            raise HTTPException(404, f"工作流 {run_id} 不存在")
        if not state.next or state.next[0] != "approval":
            raise HTTPException(409, "工作流当前不在等待审批状态")

        # A paused draft is bound to the retrieved fact revisions, including rejection/regeneration.
        with get_conn() as conn:
            for snapshot in state.values.get("retrieved_facts", []):
                current = facts_repo.get_fact(conn, snapshot.id)
                if current is None or not current.enabled or current.status != "confirmed" or current.revision != snapshot.revision:
                    raise HTTPException(409, "Facts changed; create a new workflow to retrieve current confirmed facts")

        graph.invoke(
            Command(resume={"approved": req.approved, "feedback": req.feedback}),
            config,
        )
        summary = _summarize_state(run_id)

        if req.approved and summary["status"] == WorkflowStatus.READY_TO_APPLY:
            job_id = graph.get_state(config).values.get("job_id")
            version_id = _freeze_version(job_id, summary["sections"])
            summary["resume_version_id"] = version_id
        return summary

    def _freeze_version(job_id: int | None, sections: dict) -> int:
        """批准后将分区简历冻结为不可变版本（第 8.3、8.5 节）。"""
        conn = get_conn()
        row = conn.execute(
            "INSERT INTO resume_versions (job_id, content, status) "
            "VALUES (%s, %s, 'approved') RETURNING id",
            (job_id, json.dumps({"sections": sections})),
        ).fetchone()
        for section_claims in sections.values():
            for claim in section_claims:
                conn.execute(
                    "INSERT INTO resume_claims (version_id, text, fact_ids, matched_requirements) "
                    "VALUES (%s, %s, %s, %s)",
                    (
                        row["id"],
                        claim["text"],
                        claim["fact_ids"],
                        claim.get("matched_requirements", []),
                    ),
                )
        return row["id"]

    # ---------- 简历版本 ----------

    @app.get("/api/resume-versions/{version_id}/docx")
    def export_docx(version_id: int) -> Response:
        conn = get_conn()
        version = conn.execute(
            "SELECT rv.content, j.title FROM resume_versions rv "
            "JOIN jobs j ON j.id = rv.job_id WHERE rv.id = %s",
            (version_id,),
        ).fetchone()
        if version is None:
            raise HTTPException(404, f"简历版本 {version_id} 不存在")
        sections = ResumeSections.model_validate(version["content"]["sections"])
        data = render_docx(version["title"] or "未命名职位", sections)
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
            "SELECT id, status, retry_count, updated_at FROM workflow_runs "
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
        conn = get_conn()

        def with_facts(claims: list[dict]) -> list[dict]:
            result = []
            for claim in claims:
                cited = []
                for fid in claim["fact_ids"]:
                    fact = facts_repo.get_fact(conn, fid)
                    if fact:
                        cited.append(fact.model_dump(mode="json"))
                result.append({**claim, "facts": cited})
            return result

        sections = summary["sections"] or {}
        context = {
            name: with_facts(sections.get(name, []))
            for name in ("education", "skills", "experience")
        }
        total = sum(len(v) for v in context.values())
        return TEMPLATES.TemplateResponse(
            request, "review.html", {"run_id": run_id, "sections": context, "total": total}
        )

    # ---------- 投递记录 ----------

    @app.get("/api/applications")
    def list_applications() -> list[dict]:
        rows = get_conn().execute(
            "SELECT * FROM applications ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        return [{k: str(v) for k, v in r.items()} for r in rows]

    return app


app = create_app()
