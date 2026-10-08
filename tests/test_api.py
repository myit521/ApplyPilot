"""API 集成测试：TestClient + 真实 PostgreSQL 容器 + 假模型适配器。

覆盖设计文档第 9 节主链路：事实导入 -> 职位解析 -> 匹配 ->
工作流 -> 审批 -> 冻结版本 -> DOCX 导出，以及幂等键。
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor

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
        self.generation_prompts = []

    def complete(self, system: str, user: str) -> str:
        if "职位描述解析器" in system:
            return JD_JSON
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
    fact = client.post("/api/facts", json={
        "fact_type": "project", "source_name": "T7 fixture",
        "content": "Built batch API", "skills": ["Java"],
        "metrics": ["6 个批量接口"],
    }).json()
    client.adapter.fact_id = fact["id"]
    confirmed = client.post(f"/api/facts/{fact['id']}/confirm",
                            json={"expected_revision": fact["revision"]})
    assert confirmed.status_code == 200
    job = client.post("/api/jobs", json={
        "title": "Java 后端", "company": "T7 fixture", "raw_text": "Java 后端工程师",
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
