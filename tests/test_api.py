"""API 集成测试：TestClient + 真实 PostgreSQL 容器 + 假模型适配器。

覆盖设计文档第 9 节主链路：事实导入 -> 职位解析 -> 匹配 ->
工作流 -> 审批 -> 冻结版本 -> DOCX 导出，以及幂等键。
"""

import json
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

from applypilot import db, embeddings
from applypilot.api import create_app

pytestmark = pytest.mark.integration

JD_JSON = json.dumps({
    "job_title": "Java 后端开发",
    "required": ["熟悉 Java"],
    "preferred": [],
    "responsibilities": ["参与后端开发"],
    "keywords": [{"term": "Java", "importance": "required"}],
    "unknowns": [],
})

FACTS_JSON = json.dumps({
    "facts": [{
        "fact_type": "internship",
        "source_name": "亚信实习",
        "content": "承担运维工具批量执行模块开发",
        "skills": ["Java", "Spring Boot"],
        "metrics": ["6 个批量接口"],
    }]
})

CLAIMS_JSON = json.dumps({
    "sections": {
        "education": [],
        "skills": [],
        "experience": [{
            "text": "承担批量执行模块开发，交付 6 个批量接口",
            "fact_ids": ["__FACT_ID__"],
            "matched_requirements": ["Java"],
        }],
    }
})


class FakeAdapter:
    def __init__(self):
        self.fact_id = None
        self.jd_json = JD_JSON
        self.generation_prompts = []

    def complete(self, system: str, user: str) -> str:
        if "职位描述解析器" in system:
            return self.jd_json
        if "事实提取器" in system:
            return FACTS_JSON
        if "事实一致性复核员" in system:
            return '{"violations": []}'
        self.generation_prompts.append(user)
        return CLAIMS_JSON.replace("__FACT_ID__", self.fact_id)


@pytest.fixture(scope="module")
def client():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        db.init_schema(db.connect(dsn))
        adapter = FakeAdapter()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(embeddings, "LocalEmbeddingProvider", embeddings.NullEmbeddingProvider)
            app = create_app(dsn=dsn, adapter=adapter)
            with TestClient(app) as c:
                c.adapter = adapter
                c.database_dsn = dsn
                yield c


def wait_for_status(client: TestClient, run_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        resp = client.get(f"/api/workflows/{run_id}")
        if resp.status_code == 200:
            state = resp.json()
            # 以业务状态字段为准：执行中途 state.next 同样非空
            if state["status"] in ("WAITING_APPROVAL", "FAILED", "READY_TO_APPLY"):
                return state
        # 工作流线程尚未写入首个检查点时可能短暂 404，继续轮询
        time.sleep(0.3)
    raise TimeoutError(f"工作流未在 {timeout}s 内到达等待状态: {state}")


def test_full_api_flow(client: TestClient):
    # 1. 导入事实
    resp = client.post("/api/facts/import", json={"resume_text": "某简历原文"})
    assert resp.status_code == 200
    fact = resp.json()["facts"][0]
    client.adapter.fact_id = fact["id"]

    # 2. 查询与修改事实
    facts = client.get("/api/facts").json()
    assert len(facts) == 1
    updated = client.put(f"/api/facts/{fact['id']}", json={"expected_revision": fact["revision"], "evidence_ref": "6671a45"})
    assert updated.json()["evidence_ref"] == "6671a45"

    confirmed = client.post(f"/api/facts/{fact['id']}/confirm", json={"expected_revision": updated.json()["revision"]})
    assert confirmed.json()["status"] == "confirmed"

    # 3. 保存并解析 JD
    resp = client.post("/api/jobs", json={"title": "Java 后端", "company": "模拟公司", "raw_text": "招聘 Java 后端工程师"})
    assert resp.status_code == 201
    job = resp.json()
    assert job["parsed"] is None
    job = client.post(f"/api/jobs/{job['id']}/parse").json()
    assert job["parsed"]["job_title"] == "Java 后端开发"

    # 4. 岗位匹配
    match = client.get(f"/api/jobs/{job['id']}/match").json()
    assert match["candidates"][0]["fact"]["id"] == fact["id"]
    assert match["candidates"][0]["skill_coverage"] == 1.0

    # 5. 启动工作流并等待审批
    resp = client.post("/api/workflows", json={"job_id": job["id"]})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    state = wait_for_status(client, run_id)
    assert state["status"] == "WAITING_APPROVAL"
    assert state["draft_revision"] == 1
    assert state["sections"]["experience"][0]["fact_ids"] == [fact["id"]]
    assert state["validation_errors"] == []

    # 6. 退回意见进入下一次生成请求
    resp = client.post(
        f"/api/workflows/{run_id}/approve",
        json={"approved": False, "expected_revision": 1, "feedback": "突出批量执行的结果"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["draft_revision"] == 2
    assert "突出批量执行的结果" in client.adapter.generation_prompts[1]

    # 7. 手工修改只提交主张文本，引用由服务端保留，并要求重新校验
    original_text = resp.json()["sections"]["experience"][0]["text"]
    malformed_edit = client.post(f"/api/workflows/{run_id}/edit", json={
        "expected_revision": 2,
        "sections": {"education": ["新增教育"], "skills": [], "experience": [original_text]},
    })
    assert malformed_edit.status_code == 422

    edit = client.post(f"/api/workflows/{run_id}/edit", json={
        "expected_revision": 2,
        "sections": {
            "education": [], "skills": [],
            "experience": ["参与批量执行模块开发，交付 6 个批量接口"],
        },
    })
    assert edit.status_code == 200, edit.text
    edited_state = edit.json()
    assert edited_state["status"] == "WAITING_APPROVAL"
    assert edited_state["draft_revision"] == 3
    assert edited_state["sections"]["experience"][0]["text"] == "参与批量执行模块开发，交付 6 个批量接口"
    assert edited_state["sections"]["experience"][0]["fact_ids"] == [fact["id"]]
    assert edited_state["previous_sections"]["experience"][0]["text"] == original_text

    # 过期浏览器不能批准或覆盖较新的草稿
    stale = client.post(f"/api/workflows/{run_id}/approve", json={"approved": True, "expected_revision": 2})
    assert stale.status_code == 409
    stale_edit = client.post(f"/api/workflows/{run_id}/edit", json={
        "expected_revision": 2, "sections": {"education": [], "skills": [], "experience": ["旧草稿"]},
    })
    assert stale_edit.status_code == 409

    # 8. 审核页面提供可编辑文本和上版/当前版差异
    review = client.get(f"/review/{run_id}")
    assert review.status_code == 200
    assert "textarea" in review.text
    assert "修订 3" in review.text
    assert "承担批量执行模块开发" in review.text
    assert "参与批量执行模块开发" in review.text

    # 9. 批准当前修订 -> 冻结版本
    resp = client.post(
        f"/api/workflows/{run_id}/approve", json={"approved": True, "expected_revision": 3}
    )
    result = resp.json()
    assert resp.status_code == 200, resp.text
    assert result["status"] == "READY_TO_APPLY"
    version_id = result["resume_version_id"]

    # 7. 导出 DOCX
    resp = client.get(f"/api/resume-versions/{version_id}/docx")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )
    assert len(resp.content) > 1000

    # 10. 已结束后不能再审批
    resp = client.post(
        f"/api/workflows/{run_id}/approve", json={"approved": True, "expected_revision": 3}
    )
    assert resp.status_code == 200
    assert resp.json()["resume_version_id"] == version_id
    assert resp.json()["content_sha256"] == result["content_sha256"]

    # 11. 审核页面
    resp = client.get("/")
    assert resp.status_code == 200 and "审核台" in resp.text
    resp = client.get(f"/review/{run_id}")
    assert resp.status_code == 200
    assert "参与批量执行模块开发，交付 6 个批量接口" in resp.text


def test_workflow_idempotency(client: TestClient):
    resp = client.post("/api/jobs", json={"title": "Java 后端", "company": "模拟公司", "raw_text": "另一条 JD"})
    job_id = resp.json()["id"]

    resp1 = client.post(
        "/api/workflows", json={"job_id": job_id}, headers={"Idempotency-Key": "order-1"}
    )
    run_id = resp1.json()["run_id"]
    wait_for_status(client, run_id)

    resp2 = client.post(
        "/api/workflows", json={"job_id": job_id}, headers={"Idempotency-Key": "order-1"}
    )
    # 相同幂等键返回同一个运行，而不是新建
    assert resp2.json()["run_id"] == run_id


def test_unknown_resources_404(client: TestClient):
    assert client.get("/api/jobs/999/match").status_code == 404
    assert client.get("/api/workflows/wf_missing").status_code == 404
    assert client.put("/api/facts/f_missing", json={"expected_revision": 1, "enabled": False}).status_code == 404
    assert client.get("/api/resume-versions/999/docx").status_code == 404


def test_runtime_readiness_with_real_database(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_readiness_rejects_uninitialized_business_schema():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with TestClient(create_app(dsn=dsn)) as client:
            assert client.get("/health/live").status_code == 200
            assert client.get("/health/ready").status_code == 503
            with db.connect(dsn) as conn:
                db.init_schema(conn)
            assert client.get("/health/ready").status_code == 200


def reach_waiting_approval(client: TestClient) -> tuple[str, str]:
    unique_term = f"T7Unique{uuid.uuid4().hex}"
    client.adapter.jd_json = json.dumps({
        "job_title": "Java 后端开发",
        "required": ["熟悉 Java"],
        "preferred": [],
        "responsibilities": ["参与后端开发"],
        "keywords": [
            {"term": "Java", "importance": "required"},
            {"term": unique_term, "importance": "preferred"},
        ],
        "unknowns": [],
    })
    fact = client.post("/api/facts", json={
        "fact_type": "project", "source_name": f"T7 fixture {unique_term}",
        "content": f"Built batch API {unique_term}", "skills": ["Java", unique_term],
        "metrics": ["6 个批量接口"],
    }).json()
    client.adapter.fact_id = fact["id"]
    confirmed = client.post(f"/api/facts/{fact['id']}/confirm",
                            json={"expected_revision": fact["revision"]})
    assert confirmed.status_code == 200
    job = client.post("/api/jobs", json={
        "title": "Java 后端", "company": "T7 fixture",
        "raw_text": f"Java 后端工程师 {unique_term}",
    }).json()
    run_id = client.post("/api/workflows", json={"job_id": job["id"]}).json()["run_id"]
    state = wait_for_status(client, run_id)
    assert state["status"] == "WAITING_APPROVAL", state
    return run_id, fact["id"]


def test_approval_retry_reconciles_checkpoint_without_duplicate_rows(client, monkeypatch):
    run_id, _ = reach_waiting_approval(client)
    import applypilot.api as api

    real_build_graph = api.build_graph
    failed = False

    def build_graph_failing_once(*args, **kwargs):
        nonlocal failed
        graph = real_build_graph(*args, **kwargs)

        class InvokeProxy:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

            def invoke(self, *invoke_args, **invoke_kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise RuntimeError("checkpoint unavailable")
                return self.wrapped.invoke(*invoke_args, **invoke_kwargs)

        return InvokeProxy(graph)

    monkeypatch.setattr(api, "build_graph", build_graph_failing_once)
    first = client.post(f"/api/workflows/{run_id}/approve",
                        json={"approved": True, "expected_revision": 1})
    assert first.status_code == 202
    version_id = first.json()["resume_version_id"]

    def graph_unavailable(*args, **kwargs):
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(api, "build_graph", graph_unavailable)
    pending_retry = client.post(f"/api/workflows/{run_id}/approve",
                                json={"approved": True, "expected_revision": 1})
    assert pending_retry.status_code == 202
    assert pending_retry.json()["resume_version_id"] == version_id

    monkeypatch.setattr(api, "build_graph", build_graph_failing_once)
    second = client.post(f"/api/workflows/{run_id}/approve",
                         json={"approved": True, "expected_revision": 1})
    assert second.status_code == 200
    assert second.json()["resume_version_id"] == version_id
    assert second.json()["content_sha256"] == first.json()["content_sha256"]

    monkeypatch.setattr(api, "build_graph", graph_unavailable)
    third = client.post(f"/api/workflows/{run_id}/approve",
                        json={"approved": True, "expected_revision": 1})
    assert third.status_code == 200
    assert third.json()["resume_version_id"] == version_id
    with db.connect(client.database_dsn) as conn:
        assert conn.execute("SELECT count(*) AS n FROM workflow_approvals WHERE run_id=%s",
                            (run_id,)).fetchone()["n"] == 1
        assert conn.execute(
            "SELECT count(*) AS n FROM resume_versions rv "
            "JOIN workflow_approvals wa ON wa.version_id=rv.id "
            "WHERE wa.run_id=%s AND rv.status='approved'",
            (run_id,),
        ).fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM audit_events WHERE event_type='resume.approved' "
                            "AND payload->>'run_id'=%s", (run_id,)).fetchone()["n"] == 1


def test_pending_approval_summary_survives_checkpoint_read_failure(client, monkeypatch):
    run_id, _ = reach_waiting_approval(client)
    import applypilot.api as api

    real_build_graph = api.build_graph
    failed = False

    def build_graph_failing_invoke_once(*args, **kwargs):
        nonlocal failed
        graph = real_build_graph(*args, **kwargs)

        class InvokeProxy:
            def __getattr__(self, name):
                return getattr(graph, name)

            def invoke(self, *invoke_args, **invoke_kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise RuntimeError("checkpoint unavailable")
                return graph.invoke(*invoke_args, **invoke_kwargs)

        return InvokeProxy()

    monkeypatch.setattr(api, "build_graph", build_graph_failing_invoke_once)
    approved = client.post(f"/api/workflows/{run_id}/approve",
                           json={"approved": True, "expected_revision": 1})
    assert approved.status_code == 202

    def build_graph_unreadable_state(*args, **kwargs):
        graph = real_build_graph(*args, **kwargs)

        class StateReadProxy:
            def __getattr__(self, name):
                return getattr(graph, name)

            def get_state(self, *state_args, **state_kwargs):
                raise RuntimeError("checkpoint unavailable")

        return StateReadProxy()

    monkeypatch.setattr(api, "build_graph", build_graph_unreadable_state)
    summary = client.get(f"/api/workflows/{run_id}")
    assert summary.status_code == 200
    assert summary.json()["status"] == "APPROVAL_RECONCILIATION_PENDING"
    assert summary.json()["approval_reconciliation_pending"] is True
    assert summary.json()["resume_version_id"] == approved.json()["resume_version_id"]
    assert summary.json()["waiting"] is False
    with db.connect(client.database_dsn) as conn:
        run = conn.execute("SELECT status FROM workflow_runs WHERE id=%s", (run_id,)).fetchone()
        assert run["status"] == "APPROVAL_RECONCILIATION_PENDING"


def test_pending_summary_does_not_overwrite_completed_reconciliation(client, monkeypatch):
    run_id, _ = reach_waiting_approval(client)
    import applypilot.api as api

    real_build_graph = api.build_graph
    failed = False

    def build_graph_failing_invoke_once(*args, **kwargs):
        graph = real_build_graph(*args, **kwargs)

        class InvokeProxy:
            def __getattr__(self, name):
                return getattr(graph, name)

            def invoke(self, *invoke_args, **invoke_kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise RuntimeError("checkpoint unavailable")
                return graph.invoke(*invoke_args, **invoke_kwargs)

        return InvokeProxy()

    monkeypatch.setattr(api, "build_graph", build_graph_failing_invoke_once)
    approved = client.post(f"/api/workflows/{run_id}/approve",
                           json={"approved": True, "expected_revision": 1})
    assert approved.status_code == 202
    monkeypatch.setattr(api, "build_graph", real_build_graph)

    reconciliation_locked = Event()
    finish_reconciliation = Event()
    reconciliation_done = Event()
    summary_read_approval = Event()
    summary_attempted_lock = Event()
    real_workflow_lock = api.approvals_repo.workflow_lock
    real_get_approval = api.approvals_repo.get_approval

    @contextmanager
    def track_summary_lock(conn, target_run_id):
        if target_run_id == run_id:
            summary_attempted_lock.set()
        with real_workflow_lock(conn, target_run_id):
            yield

    def pause_summary_after_approval_read(conn, target_run_id):
        approval = real_get_approval(conn, target_run_id)
        if target_run_id == run_id:
            summary_read_approval.set()
            assert reconciliation_done.wait(timeout=10)
        return approval

    monkeypatch.setattr(api.approvals_repo, "workflow_lock", track_summary_lock)
    monkeypatch.setattr(api.approvals_repo, "get_approval", pause_summary_after_approval_read)

    def reconcile_approval():
        with db.connect(client.database_dsn) as conn:
            with real_workflow_lock(conn, run_id):
                reconciliation_locked.set()
                assert finish_reconciliation.wait(timeout=10)
                api.approvals_repo.set_graph_reconciled(conn, run_id)
                conn.execute(
                    "UPDATE workflow_runs SET current_node='', status='READY_TO_APPLY', "
                    "error='', updated_at=now() WHERE id=%s",
                    (run_id,),
                )
                reconciliation_done.set()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="reconciler") as reconcile_pool:
        reconciliation = reconcile_pool.submit(reconcile_approval)
        try:
            assert reconciliation_locked.wait(timeout=5)
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="summary-request") as summary_pool:
                summary_request = summary_pool.submit(client.get, f"/api/workflows/{run_id}")
                deadline = time.monotonic() + 5
                while (not summary_read_approval.is_set() and not summary_attempted_lock.is_set()
                       and time.monotonic() < deadline):
                    time.sleep(0.01)
                assert summary_read_approval.is_set() or summary_attempted_lock.is_set()
                finish_reconciliation.set()
                reconciliation.result(timeout=5)
                summary = summary_request.result(timeout=5)
        finally:
            finish_reconciliation.set()

    assert summary.status_code == 200
    assert summary.json()["status"] == "READY_TO_APPLY"
    assert summary.json()["approval_reconciliation_pending"] is False
    with db.connect(client.database_dsn) as conn:
        status = conn.execute("SELECT status FROM workflow_runs WHERE id=%s",
                              (run_id,)).fetchone()["status"]
        assert status == "READY_TO_APPLY"


def test_workflow_summary_does_not_overwrite_concurrent_approval(client, monkeypatch):
    run_id, _ = reach_waiting_approval(client)
    import applypilot.api as api

    real_build_graph = api.build_graph
    approval = None

    def build_graph_approving_during_state_read(*args, **kwargs):
        graph = real_build_graph(*args, **kwargs)

        class StateReadProxy:
            def __getattr__(self, name):
                return getattr(graph, name)

            def get_state(self, *state_args, **state_kwargs):
                nonlocal approval
                state = graph.get_state(*state_args, **state_kwargs)
                if approval is None:
                    with db.connect(client.database_dsn) as conn:
                        approval = api.approvals_repo.persist_approval(
                            conn,
                            run_id=run_id,
                            draft_revision=state.values["draft_revision"],
                            job_id=state.values["job_id"],
                            sections=state.values["resume"].model_dump(mode="json"),
                            retrieved_facts=state.values["retrieved_facts"],
                        )
                return state

        return StateReadProxy()

    monkeypatch.setattr(api, "build_graph", build_graph_approving_during_state_read)
    summary = client.get(f"/api/workflows/{run_id}")
    assert summary.status_code == 200
    assert summary.json()["status"] == "APPROVAL_RECONCILIATION_PENDING"
    assert summary.json()["resume_version_id"] == approval["version_id"]
    with db.connect(client.database_dsn) as conn:
        status = conn.execute("SELECT status FROM workflow_runs WHERE id=%s",
                              (run_id,)).fetchone()["status"]
        assert status == "APPROVAL_RECONCILIATION_PENDING"
    review = client.get(f"/review/{run_id}")
    assert 'id="approval-pending"' in review.text
    assert 'id="approve"' not in review.text
    assert 'id="reject"' not in review.text


def test_two_concurrent_approvals_return_one_version(client):
    run_id, _ = reach_waiting_approval(client)
    body = {"approved": True, "expected_revision": 1}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda _: client.post(f"/api/workflows/{run_id}/approve", json=body), range(2)
        ))
    assert [response.status_code for response in responses] == [200, 200]
    assert len({response.json()["resume_version_id"] for response in responses}) == 1
    assert len({response.json()["content_sha256"] for response in responses}) == 1


def test_stale_approval_writes_no_version(client):
    run_id, _ = reach_waiting_approval(client)
    response = client.post(f"/api/workflows/{run_id}/approve",
                           json={"approved": True, "expected_revision": 99})
    assert response.status_code == 409
    with db.connect(client.database_dsn) as conn:
        assert conn.execute("SELECT count(*) AS n FROM workflow_approvals WHERE run_id=%s",
                            (run_id,)).fetchone()["n"] == 0


def test_changed_fact_revision_blocks_approval_without_record(client):
    run_id, fact_id = reach_waiting_approval(client)
    fact = next(item for item in client.get("/api/facts", params={"enabled": "false"}).json()
                if item["id"] == fact_id)
    changed = client.put(f"/api/facts/{fact_id}", json={
        "expected_revision": fact["revision"], "content": "Changed after retrieval",
    })
    assert changed.status_code == 200
    response = client.post(f"/api/workflows/{run_id}/approve",
                           json={"approved": True, "expected_revision": 1})
    assert response.status_code == 409
    with db.connect(client.database_dsn) as conn:
        assert conn.execute("SELECT count(*) AS n FROM workflow_approvals WHERE run_id=%s",
                            (run_id,)).fetchone()["n"] == 0


def test_edit_and_approval_are_serialized_for_one_revision(client):
    run_id, _ = reach_waiting_approval(client)
    state = client.get(f"/api/workflows/{run_id}").json()
    sections = {name: [claim["text"] for claim in state["sections"][name]]
                for name in ("education", "skills", "experience")}
    with ThreadPoolExecutor(max_workers=2) as pool:
        edit = pool.submit(client.post, f"/api/workflows/{run_id}/edit",
                           json={"expected_revision": 1, "sections": sections})
        approve = pool.submit(client.post, f"/api/workflows/{run_id}/approve",
                              json={"approved": True, "expected_revision": 1})
        responses = [edit.result(), approve.result()]
    assert sorted(response.status_code for response in responses) == [200, 409]


def test_approved_docx_and_review_use_frozen_job_and_fact_snapshots(client):
    run_id, fact_id = reach_waiting_approval(client)
    approved = client.post(f"/api/workflows/{run_id}/approve",
                           json={"approved": True, "expected_revision": 1}).json()
    version_id = approved["resume_version_id"]
    fact = next(item for item in client.get("/api/facts", params={"enabled": "false"}).json()
                if item["id"] == fact_id)
    changed_fact = client.put(f"/api/facts/{fact_id}", json={
        "expected_revision": fact["revision"], "content": "Changed after approval",
    })
    assert changed_fact.status_code == 200
    with db.connect(client.database_dsn) as conn:
        conn.execute("UPDATE jobs SET title='Changed title', raw_text='Changed JD' "
                     "WHERE id=(SELECT rv.job_id FROM workflow_approvals wa "
                     "JOIN resume_versions rv ON rv.id=wa.version_id WHERE wa.run_id=%s)",
                     (run_id,))
        frozen_fact = conn.execute(
            "SELECT snapshot FROM resume_version_facts WHERE version_id=%s AND fact_id=%s",
            (version_id, fact_id),
        ).fetchone()["snapshot"]
        assert frozen_fact["content"].startswith("Built batch API")

    response = client.get(f"/api/resume-versions/{version_id}/docx")
    assert response.status_code == 200
    from io import BytesIO
    from docx import Document

    text = "\n".join(p.text for p in Document(BytesIO(response.content)).paragraphs)
    assert "Java 后端" in text
    assert "Changed title" not in text
    review_page = client.get(f"/review/{run_id}")
    assert review_page.status_code == 200
    assert "Built batch API" in review_page.text
    assert "Changed after approval" not in review_page.text


def test_pending_approval_page_has_recovery_without_edit_or_decision_controls(client, monkeypatch):
    run_id, _ = reach_waiting_approval(client)
    import applypilot.api as api

    real_build_graph = api.build_graph
    failed = False

    def build_graph_invoke_failing_once(*args, **kwargs):
        nonlocal failed
        graph = real_build_graph(*args, **kwargs)

        class InvokeProxy:
            def __getattr__(self, name):
                return getattr(graph, name)

            def invoke(self, *invoke_args, **invoke_kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise RuntimeError("checkpoint unavailable")
                return graph.invoke(*invoke_args, **invoke_kwargs)

        return InvokeProxy()

    monkeypatch.setattr(api, "build_graph", build_graph_invoke_failing_once)
    approved = client.post(f"/api/workflows/{run_id}/approve",
                           json={"approved": True, "expected_revision": 1})
    assert approved.status_code == 202
    version_id = approved.json()["resume_version_id"]

    review_page = client.get(f"/review/{run_id}")
    assert review_page.status_code == 200
    assert "已批准、待恢复对账" in review_page.text
    assert f"/api/resume-versions/{version_id}/docx" in review_page.text
    assert 'id="retry-reconciliation"' in review_page.text
    assert 'id="save-edit"' not in review_page.text
    assert 'id="approve"' not in review_page.text
    assert 'id="reject"' not in review_page.text
    assert "<textarea" not in review_page.text

    home = client.get("/")
    assert f'href="/review/{run_id}">重试对账</a>' in home.text
    monkeypatch.setattr(api, "build_graph", real_build_graph)
    retry = client.post(f"/api/workflows/{run_id}/approve",
                        json={"approved": True, "expected_revision": 1})
    assert retry.status_code == 200
    assert retry.json()["resume_version_id"] == version_id


def test_legacy_resume_version_still_exports_with_current_job_title(client):
    job = client.post("/api/jobs", json={
        "title": "Legacy Java 后端", "company": "Legacy fixture", "raw_text": "原始职位",
    }).json()
    old_content = {"sections": {
        "education": [], "skills": [],
        "experience": [{"text": "保留旧版内容", "fact_ids": [], "matched_requirements": []}],
    }}
    with db.connect(client.database_dsn) as conn:
        version_id = conn.execute(
            "INSERT INTO resume_versions (job_id, content, status) "
            "VALUES (%s, %s::jsonb, 'approved') RETURNING id",
            (job["id"], json.dumps(old_content)),
        ).fetchone()["id"]

    response = client.get(f"/api/resume-versions/{version_id}/docx")
    assert response.status_code == 200
    from io import BytesIO
    from docx import Document

    text = "\n".join(p.text for p in Document(BytesIO(response.content)).paragraphs)
    assert "Legacy Java 后端" in text
    assert "保留旧版内容" in text
