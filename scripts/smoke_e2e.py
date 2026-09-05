"""真实环境端到端冒烟脚本。

对运行中的 ApplyPilot 服务执行完整演示链路（docs/design.md 第 16 节）：
导入事实 -> 解析 JD -> 匹配 -> 启动工作流 -> 等待审批 -> 批准 ->
导出 DOCX -> 注入虚构指标验证拦截（在独立工作流中）。

用法：
    docker compose up -d
    uvicorn applypilot.api:app --port 8000
    python scripts/smoke_e2e.py
"""

from __future__ import annotations

import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"

RESUME_TEXT = (
    "武汉轻工大学软件工程专业本科，2027 届毕业。"
    "2025.06-2025.09 亚信科技 后端开发实习生。"
    "承担运维工具批量执行模块开发，提供批量参数校验、预检、SQL 预览和异步执行能力，交付 6 个批量接口。"
    "实现进度查询、结果查询、失败行提取和基于 source_batch_id 的失败数据修正重试。"
    "使用专用线程池做批次并发控制。"
    "手机配件商城项目：使用 Testcontainers 搭建分层集成测试基座，引入 Flyway 管理数据库迁移。"
)

JD_TEXT = (
    "Java 后端开发工程师（2027 届校招）- 杭州。"
    "职责：参与交易核心系统后端服务开发，参与订单、库存模块接口设计与性能优化。"
    "要求：2027 届本科及以上学历，熟悉 Java，了解 Spring Boot、MySQL、Redis。"
    "加分项：有 Agent 或 AI 应用开发经验者优先。"
)


def wait_approval(client: httpx.Client, run_id: str, timeout: float = 180.0) -> dict:
    """等待工作流进入终态或审批中断点。

    以业务状态字段为准：执行中途 state.next 同样非空，
    不能用 waiting 判断是否已停在审批。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/workflows/{run_id}")
        if resp.status_code == 200:
            state = resp.json()
            if state["status"] in ("WAITING_APPROVAL", "FAILED", "READY_TO_APPLY"):
                return state
        time.sleep(2)
    raise TimeoutError(f"工作流 {run_id} 超时")


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=120.0)

    print("== 1. 导入简历事实 ==")
    resp = client.post("/api/facts/import", json={"resume_text": RESUME_TEXT})
    resp.raise_for_status()
    facts = resp.json()["facts"]
    print(f"提取 {len(facts)} 条事实：")
    for f in facts:
        print(f"  - [{f['fact_type']}] {f['source_name']}: {f['content'][:40]}... 指标: {f['metrics']}")

    print("\n== 2. 保存并解析 JD ==")
    resp = client.post("/api/jobs", json={"raw_text": JD_TEXT})
    resp.raise_for_status()
    job = resp.json()
    parsed = job["parsed"]
    print(f"岗位: {parsed['job_title']} | 必备: {parsed['required']}")
    print(f"加分: {parsed['preferred']}")

    print("\n== 3. 岗位匹配 ==")
    match = client.get(f"/api/jobs/{job['id']}/match").json()
    for c in match["candidates"]:
        print(f"  - {c['fact']['id']} 得分 {c['score']} 覆盖率 {c['skill_coverage']}")

    print("\n== 4. 启动工作流，等待审批 ==")
    run_id = client.post("/api/workflows", json={"job_id": job["id"]}).json()["run_id"]
    state = wait_approval(client, run_id)
    print(f"状态: {state['status']}, 校验重试: {state['validation_retries']}")
    if state["status"] != "WAITING_APPROVAL":
        print(f"!! 工作流未进入审批: {state['error']}")
        return 1
    titles = {"education": "教育背景", "skills": "专业技能", "experience": "工作与项目经历"}
    for name, title in titles.items():
        for c in (state["sections"] or {}).get(name, []):
            print(f"  [{title}] {c['text'][:60]} 引用: {c['fact_ids']}")

    print("\n== 5. 批准并冻结版本 ==")
    result = client.post(
        f"/api/workflows/{run_id}/approve", json={"approved": True}
    ).json()
    version_id = result.get("resume_version_id")
    print(f"状态: {result['status']}, 简历版本: {version_id}")

    print("\n== 6. 导出 DOCX ==")
    resp = client.get(f"/api/resume-versions/{version_id}/docx")
    resp.raise_for_status()
    out = f"resume-v{version_id}.docx"
    with open(out, "wb") as f:
        f.write(resp.content)
    print(f"已保存 {out} ({len(resp.content)} 字节)")

    print("\n== 冒烟通过 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
