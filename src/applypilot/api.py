"""FastAPI 接口层。

对应 docs/design.md 第 9 节。应用通过 create_app 工厂注入
数据库连接串、模型适配器和 checkpointer，测试可用假适配器
和临时数据库替换。
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from pydantic import BaseModel

from . import db, facts_repo, search
from .deepseek_adapter import DeepSeekAdapter
from .docx_export import render_docx
from .fact_import import FactImportError, extract_facts
from .jd_parser import JDParseError, parse_jd
from .model_adapter import ModelError
from .schemas import Fact, FactType, JobRequirements, ResumeClaim
from .workflow import WorkflowStatus, build_graph


class FactImportRequest(BaseModel):
    resume_text: str


class FactUpdateRequest(BaseModel):
    source_name: str | None = None
    content: str | None = None
    skills: list[str] | None = None
    metrics: list[str] | None = None
    evidence_type: str | None = None
    evidence_ref: str | None = None
    enabled: bool | None = None


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
    app = FastAPI(title="ApplyPilot")
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

    # checkpointer 在应用创建时初始化一次，避免多线程惰性创建
    # 并发执行 setup() 导致重复建表。
    if checkpointer is None:
        import psycopg
        from psycopg.rows import dict_row

        _cp_conn = psycopg.connect(
            dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row
        )
        checkpointer = PostgresSaver(_cp_conn)
        checkpointer.setup()

    def get_checkpointer():
        return checkpointer

    def get_graph():
        conn = get_conn()
        retriever = search.PostgresFactRetriever(conn)
        return build_graph(get_adapter(), retriever, checkpointer=get_checkpointer())

    # ---------- 事实库 ----------

    @app.post("/api/facts/import")
    def import_facts(req: FactImportRequest) -> dict:
        try:
            facts = extract_facts(req.resume_text, get_adapter())
        except FactImportError as e:
            raise HTTPException(422, str(e)) from e
        conn = get_conn()
        for fact in facts:
            facts_repo.upsert_fact(conn, fact)
        return {"created": len(facts), "facts": [f.model_dump(mode="json") for f in facts]}

    @app.get("/api/facts")
    def list_facts(fact_type: FactType | None = None, enabled: bool = True) -> list[dict]:
        facts = facts_repo.list_facts(get_conn(), fact_type=fact_type, enabled_only=enabled)
        return [f.model_dump(mode="json") for f in facts]

    @app.put("/api/facts/{fact_id}")
    def update_fact(fact_id: str, req: FactUpdateRequest) -> dict:
        conn = get_conn()
        fact = facts_repo.get_fact(conn, fact_id)
        if fact is None:
            raise HTTPException(404, f"事实 {fact_id} 不存在")
        for field, value in req.model_dump(exclude_none=True).items():
            setattr(fact, field, value)
        facts_repo.upsert_fact(conn, fact)
        return fact.model_dump(mode="json")

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
        hit_ids = set(fts)
        if not hit_ids:
            return {"terms": terms, "candidates": []}
        placeholders = ",".join(["%s"] * len(hit_ids))
        rows = conn.execute(
            f"SELECT * FROM facts WHERE id IN ({placeholders})", list(hit_ids)
        ).fetchall()
        facts = [Fact(**{k: r[k] for k in Fact.model_fields if k in r}) for r in rows]
        scored = search.merge_and_score(
            facts,
            required_skills=search.required_skill_terms(requirements),
            fulltext_hits=fts,
            vector_hits={},
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
        summary = {
            "run_id": run_id,
            "status": values.get("status"),
            "waiting": bool(state.next),
            "validation_retries": values.get("validation_retries", 0),
            "error": values.get("error") or run_errors.get(run_id, ""),
            "claims": [c.model_dump(mode="json") for c in values.get("claims", [])],
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

        graph.invoke(
            Command(resume={"approved": req.approved, "feedback": req.feedback}),
            config,
        )
        summary = _summarize_state(run_id)

        if req.approved and summary["status"] == WorkflowStatus.READY_TO_APPLY:
            job_id = graph.get_state(config).values.get("job_id")
            version_id = _freeze_version(job_id, summary["claims"])
            summary["resume_version_id"] = version_id
        return summary

    def _freeze_version(job_id: int | None, claims: list[dict]) -> int:
        """批准后将 claims 冻结为不可变简历版本（第 8.5 节）。"""
        conn = get_conn()
        content = {"claims": claims}
        row = conn.execute(
            "INSERT INTO resume_versions (job_id, content, status) "
            "VALUES (%s, %s, 'approved') RETURNING id",
            (job_id, json.dumps(content)),
        ).fetchone()
        for claim in claims:
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
        claims = [ResumeClaim.model_validate(c) for c in version["content"]["claims"]]
        data = render_docx(version["title"] or "未命名职位", claims)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="resume-{version_id}.docx"'},
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
