"""API 集成测试：TestClient + 真实 PostgreSQL 容器 + 假模型适配器。

覆盖设计文档第 9 节主链路：事实导入 -> 职位解析 -> 匹配 ->
工作流 -> 审批 -> 冻结版本 -> DOCX 导出，以及幂等键。
"""

import json
import time

import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

from applypilot import db
from applypilot.api import create_app

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
    "claims": [{
        "text": "承担批量执行模块开发，交付 6 个批量接口",
        "fact_ids": ["__FACT_ID__"],
        "matched_requirements": ["Java"],
    }]
})


class FakeAdapter:
    def __init__(self):
        self.fact_id = None

    def complete(self, system: str, user: str) -> str:
        if "职位描述解析器" in system:
            return JD_JSON
        if "事实提取器" in system:
            return FACTS_JSON
        if "事实一致性复核员" in system:
            return '{"violations": []}'
        return CLAIMS_JSON.replace("__FACT_ID__", self.fact_id)


@pytest.fixture(scope="module")
def client():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        db.init_schema(db.connect(dsn))
        adapter = FakeAdapter()
        app = create_app(dsn=dsn, adapter=adapter)
        with TestClient(app) as c:
            c.adapter = adapter
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
    updated = client.put(f"/api/facts/{fact['id']}", json={"evidence_ref": "6671a45"})
    assert updated.json()["evidence_ref"] == "6671a45"

    # 3. 保存并解析 JD
    resp = client.post("/api/jobs", json={"raw_text": "招聘 Java 后端工程师"})
    assert resp.status_code == 201
    job = resp.json()
    assert job["parsed"]["job_title"] == "Java 后端开发"
    assert job["parse_error"] is None

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
    assert state["claims"][0]["fact_ids"] == [fact["id"]]
    assert state["validation_errors"] == []

    # 6. 批准 -> 冻结版本
    resp = client.post(f"/api/workflows/{run_id}/approve", json={"approved": True})
    result = resp.json()
    assert result["status"] == "READY_TO_APPLY"
    version_id = result["resume_version_id"]

    # 7. 导出 DOCX
    resp = client.get(f"/api/resume-versions/{version_id}/docx")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )
    assert len(resp.content) > 1000

    # 8. 已结束后不能再审批
    resp = client.post(f"/api/workflows/{run_id}/approve", json={"approved": True})
    assert resp.status_code == 409

    # 9. 审核页面
    resp = client.get("/")
    assert resp.status_code == 200 and "审核台" in resp.text
    resp = client.get(f"/review/{run_id}")
    assert resp.status_code == 200
    assert "承担批量执行模块开发" in resp.text


def test_workflow_idempotency(client: TestClient):
    resp = client.post("/api/jobs", json={"raw_text": "另一条 JD"})
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
    assert client.put("/api/facts/f_missing", json={"enabled": False}).status_code == 404
    assert client.get("/api/resume-versions/999/docx").status_code == 404
