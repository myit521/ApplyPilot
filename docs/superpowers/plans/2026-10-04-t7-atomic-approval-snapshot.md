# T7 原子批准与不可变快照实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将批准版本、主张、事实修订快照、幂等记录和审计事件作为一个 PostgreSQL 事务提交，并允许安全重试 LangGraph checkpoint 对账。

**Architecture:** `approval_snapshots.py` 构建规范版本包并计算 SHA-256；`approvals_repo.py` 负责 advisory lock、事务写入、幂等查询和对账标记；`api.py` 在锁内核对 checkpoint 和修订，再先提交业务包、后推进图。DOCX 对新版本读取被冻结的职位快照，旧版本保持兼容。

**Tech Stack:** Python、FastAPI、Pydantic、psycopg 3、PostgreSQL、LangGraph、pytest、testcontainers。

**Spec:** [2026-10-04-t7-atomic-approval-snapshot-design.md](../specs/2026-10-04-t7-atomic-approval-snapshot-design.md)

## Global Constraints

- PostgreSQL 是批准结果、版本内容、引用快照与批准审计事件的权威来源。
- LangGraph checkpoint 不与业务事务假装原子；数据库提交后才推进 LangGraph。
- 同 run 的编辑与批准操作使用 PostgreSQL advisory lock 串行化。
- 新增版本化 SQL migration，不重写旧 migration；既有版本不回填伪造 run ID 或快照。
- 哈希使用有显式版本号的规范 JSON：UTF-8 编码、对象键排序、紧凑分隔符、非 ASCII 字符原样编码；随后计算 SHA-256 十六进制摘要。
- 不添加消息队列、worker、微服务或新运行时依赖；持久化 worker 和进程重启自动扫描属于 T8。
- 日志及审计事件不写 API 密钥；审计事件不重复保存完整职位正文。
- 被引用事实的正文只保存在 `resume_version_facts.snapshot`；`resume_versions.content` 保存职位和简历快照，不重复内嵌事实正文。规范哈希在内存中对两部分组成的完整批准包计算。
- 全部失败路径必须失败关闭；旧批准数据包不能被事实或职位后续修改重写。

## Review Focus

1. 数据库批准包提交成功后 LangGraph 推进失败：相同审批重试必须复用版本并继续对账，不能要求用户编辑或再次创建版本。
2. 同一 run 的两个审批请求或编辑与审批并发：必须只形成一个批准版本；过期草稿请求返回 409。
3. 已检索事实在审批期间更新、停用或撤回确认：事务必须阻止不一致批准；已完成批准的历史快照则保持不变。
4. 旧数据库含有简历版本和投递关系但没有 T7 migration：迁移必须保留旧行与 ID，重复迁移不改变内容。
5. 批准后职位或事实被修改：版本哈希、来源快照和 DOCX 必须仍从冻结包得到同一内容。

---

### Task 1: 为批准记录和事实快照增加兼容迁移

**Files:**
- Create: `db/migrations/003_atomic_approval_snapshot.sql`
- Modify: `src/applypilot/db.py`
- Test: `tests/test_t7_approval_integration.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_db_integration.py`

**Interfaces:**
- Consumes: 当前 `resume_versions`、`workflow_runs`、`fact_revisions`、`audit_events` schema。
- Produces: `workflow_approvals` 与 `resume_version_facts` 两张表；`schema_ready()` 会要求 `003_atomic_approval_snapshot.sql` 已应用。

- [ ] **Step 1: 写迁移兼容性失败测试**

```python
def test_t7_migration_preserves_legacy_versions_and_is_idempotent():
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with db.connect(dsn) as conn:
            conn.execute(db.SCHEMA_PATH.read_text(encoding="utf-8"))
            job = conn.execute(
                "INSERT INTO jobs (raw_text) VALUES ('legacy JD') RETURNING id"
            ).fetchone()
            version = conn.execute(
                "INSERT INTO resume_versions (job_id, content) VALUES (%s, %s::jsonb) RETURNING id",
                (job["id"], '{"sections":{"experience":[]}}'),
            ).fetchone()
            db.migrate(conn)
            db.migrate(conn)
            saved = conn.execute(
                "SELECT job_id, content FROM resume_versions WHERE id=%s",
                (version["id"],),
            ).fetchone()
            assert saved == {"job_id": job["id"], "content": {"sections": {"experience": []}}}
            assert conn.execute(
                "SELECT version FROM schema_migrations "
                "WHERE version='003_atomic_approval_snapshot.sql'"
            ).fetchone()
            assert db.schema_ready(conn)
```

- [ ] **Step 2: 确认迁移测试按预期失败**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_t7_approval_integration.py::test_t7_migration_preserves_legacy_versions_and_is_idempotent`

Expected: FAIL，因为 T7 migration 尚未应用且 `schema_ready()` 尚未检查该版本。

- [ ] **Step 3: 添加仅增量的 schema migration**

在 `003_atomic_approval_snapshot.sql` 中创建：

```sql
CREATE TABLE workflow_approvals (
    run_id TEXT PRIMARY KEY REFERENCES workflow_runs(id),
    draft_revision INTEGER NOT NULL CHECK (draft_revision > 0),
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    version_id BIGINT NOT NULL UNIQUE REFERENCES resume_versions(id),
    graph_reconciled BOOLEAN NOT NULL DEFAULT FALSE,
    approved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE resume_version_facts (
    version_id BIGINT NOT NULL REFERENCES resume_versions(id),
    fact_id TEXT NOT NULL,
    fact_revision INTEGER NOT NULL CHECK (fact_revision > 0),
    snapshot JSONB NOT NULL,
    PRIMARY KEY (version_id, fact_id),
    FOREIGN KEY (fact_id, fact_revision)
        REFERENCES fact_revisions (fact_id, revision)
);
```

迁移只创建新表和约束，不修改旧版本内容。`src/applypilot/db.py::schema_ready()` 增加对上述 migration 版本的检查。

`schema_ready()` 同时将 `workflow_approvals` 和 `resume_version_facts` 加入表存在性查询。更新 `tests/test_runtime.py` 对 readiness 查询结果的 mock；更新 `tests/test_db_integration.py::clean`，在版本表前显式清空两张新表，保持测试隔离。

- [ ] **Step 4: 运行迁移测试并确认通过**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_t7_approval_integration.py::test_t7_migration_preserves_legacy_versions_and_is_idempotent`

Expected: PASS；旧版本 ID、职位 ID 和 JSON 内容保留，migration 重复执行无副作用。

- [ ] **Step 5: 提交迁移切片**

```powershell
git add db/migrations/003_atomic_approval_snapshot.sql src/applypilot/db.py tests/test_t7_approval_integration.py
git commit -m "feat: add approval snapshot schema"
```

### Task 2: 构建可复现的批准数据包和 SHA-256

**Files:**
- Create: `src/applypilot/approval_snapshots.py`
- Create: `tests/test_approval_snapshots.py`

**Interfaces:**
- Consumes: 职位数据库行、`ResumeSections` JSON、锁定后从 `fact_revisions.snapshot` 读取的事实修订快照。
- Produces: `build_approval_package(*, job: dict, sections: dict, fact_snapshots: list[dict], draft_revision: int) -> dict` 与 `hash_approval_package(package: dict) -> str`。完整 package（包含 facts）只用于构造与哈希；持久化时 facts 单独写入 `resume_version_facts`。

- [ ] **Step 1: 写规范化和变更敏感性测试**

```python
def test_approval_package_hash_is_stable_and_binds_fact_revision():
    package = {
        "schema_version": 1,
        "draft_revision": 4,
        "job": {"id": 7, "title": "Java 后端"},
        "sections": {"education": [], "skills": [], "experience": []},
        "facts": [{"id": "f1", "revision": 2, "snapshot": {"content": "批处理"}}],
    }
    reordered = {
        "facts": package["facts"], "sections": package["sections"],
        "job": package["job"], "draft_revision": 4, "schema_version": 1,
    }
    assert hash_approval_package(package) == hash_approval_package(reordered)
    changed = {**package, "facts": [{"id": "f1", "revision": 3, "snapshot": {"content": "批处理"}}]}
    assert hash_approval_package(package) != hash_approval_package(changed)
```

测试文件导入 `build_approval_package` 和 `hash_approval_package`。另写 `test_package_contains_job_and_only_cited_revisions()`：输入两个已检索事实，但 claim 只引用 `f1`，断言 package 保留职位原始 JD、sections 和 `f1` 的 revision snapshot，不含 `f2`；把 claim 改为引用未检索的 `f3` 时断言构包抛出 `ValueError`。

- [ ] **Step 2: 确认 hash 测试先失败**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_approval_snapshots.py::test_approval_package_hash_is_stable_and_binds_fact_revision`

Expected: FAIL，因为批准快照构建器尚不存在。

- [ ] **Step 3: 实现批准包构造和规范 JSON 哈希**

实现 `approval_snapshots.py`：将 section 顺序固定为 `education`、`skills`、`experience`；仅选择被主张引用的事实，按 fact ID 排序并拒绝工作流未检索到的引用；保留职位原始 JD、解析结果和 URL。`hash_approval_package()` 使用：

```python
payload = json.dumps(
    package, ensure_ascii=False, sort_keys=True, separators=(",", ":")
).encode("utf-8")
return hashlib.sha256(payload).hexdigest()
```

- [ ] **Step 4: 运行快照单元测试**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_approval_snapshots.py`

Expected: PASS；对象键顺序不影响哈希，职位、主张文本、引用事实内容或修订变化会改变哈希，重复相同输入得到同一 64 位小写摘要。

- [ ] **Step 5: 提交快照切片**

```powershell
git add src/applypilot/approval_snapshots.py tests/test_approval_snapshots.py
git commit -m "feat: build canonical approval snapshots"
```

### Task 3: 以单个 PostgreSQL 事务保存批准版本

**Files:**
- Create: `src/applypilot/approvals_repo.py`
- Modify: `src/applypilot/api.py`
- Test: `tests/test_t7_approval_integration.py`

**Interfaces:**
- Consumes: `build_approval_package()`、工作流 `retrieved_facts` 和 migration 中两张新表。
- Produces: `get_approval(conn, run_id) -> dict | None`、`persist_approval(conn, *, run_id, draft_revision, job_id, sections, retrieved_facts) -> dict`、`set_graph_reconciled(conn, run_id) -> None`。`persist_approval()` 在事务内构造 package 与 hash，返回 `{version_id, draft_revision, content_sha256, package, graph_reconciled}`。`get_approval()` 从版本 content 和 `resume_version_facts` 重建完整 package，并重算哈希与记录值核对；不匹配时失败关闭，以便重试和 summary 使用相同且可验证的快照。

- [ ] **Step 1: 写完整写入和事务回滚测试**

在 `tests/test_t7_approval_integration.py` 里用 module-scoped `PostgresContainer("pgvector/pgvector:pg16")` 初始化 `db.init_schema()`；每个测试用独立 `db.connect(dsn)`，先 `TRUNCATE workflow_approvals, resume_version_facts, resume_claims, resume_versions, jobs, workflow_runs, audit_events, fact_revisions, facts CASCADE`。`approval_case` fixture 创建职位、一个 confirmed `f1`（revision 1）、对应 `fact_revisions.snapshot`、等待审批的 workflow run，并返回 `(conn, run_id, job_id, sections, retrieved_facts)`。sections 有一个 experience claim（text=`Built batch API`、fact_ids=`["f1"]`、matched_requirements=`["Java"]`）；retrieved_facts 是含 `f1` 修订 1 的 `Fact` 模型实例；`fact_revisions.snapshot` 为 `{content: "Built batch API", skills: ["Java"], status: "confirmed", enabled: true}`。职位行含标题 `Java 后端`、公司 `T7 fixture`、来源 `paste` 和原始 JD。

```python
def test_persist_approval_writes_version_claims_facts_and_one_event(approval_case):
    conn, run_id, job_id, sections, retrieved_facts = approval_case
    record = persist_approval(
        conn, run_id=run_id, draft_revision=2, job_id=job_id,
        sections=sections, retrieved_facts=retrieved_facts,
    )
    assert record["draft_revision"] == 2
    assert len(record["content_sha256"]) == 64
    assert conn.execute("SELECT count(*) AS n FROM resume_claims WHERE version_id=%s",
                        (record["version_id"],)).fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM resume_version_facts WHERE version_id=%s",
                        (record["version_id"],)).fetchone()["n"] == 1
    saved_content = conn.execute("SELECT content FROM resume_versions WHERE id=%s",
                                  (record["version_id"],)).fetchone()["content"]
    assert "facts" not in saved_content
    assert get_approval(conn, run_id)["package"] == record["package"]
    assert hash_approval_package(get_approval(conn, run_id)["package"]) == record["content_sha256"]
    assert conn.execute(
        "SELECT count(*) AS n FROM audit_events WHERE event_type='resume.approved' "
        "AND payload->>'run_id'=%s", (run_id,)
    ).fetchone()["n"] == 1


def test_persist_approval_rolls_back_every_table_when_event_insert_fails(approval_case):
    conn, run_id, job_id, sections, retrieved_facts = approval_case
    conn.execute("CREATE FUNCTION fail_t7_event() RETURNS trigger LANGUAGE plpgsql AS "
                 "$$ BEGIN RAISE EXCEPTION 'injected event failure'; END $$")
    conn.execute("CREATE TRIGGER fail_t7_event BEFORE INSERT ON audit_events "
                 "FOR EACH ROW EXECUTE FUNCTION fail_t7_event()")
    try:
        with pytest.raises(psycopg.Error, match="injected event failure"):
            persist_approval(conn, run_id=run_id, draft_revision=2, job_id=job_id,
                              sections=sections, retrieved_facts=retrieved_facts)
    finally:
        conn.execute("DROP TRIGGER fail_t7_event ON audit_events")
        conn.execute("DROP FUNCTION fail_t7_event()")
    assert conn.execute("SELECT count(*) AS n FROM resume_versions").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM resume_claims").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM resume_version_facts").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM workflow_approvals").fetchone()["n"] == 0
```

- [ ] **Step 2: 确认持久化测试先失败**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_t7_approval_integration.py::test_persist_approval_writes_version_claims_facts_and_one_event`

Expected: FAIL，因为 `approvals_repo.py` 尚不存在。

- [ ] **Step 3: 实现事务仓储和事实修订核验**

`persist_approval()` 在一个 `with conn.transaction():` 中，以稳定 ID 顺序锁定全部 retrieved facts，要求每条仍启用、confirmed 且 revision 与工作流快照一致；锁定并读取 job；只为主张引用的事实读取不可变 `fact_revisions.snapshot`；在事务内调用 `build_approval_package()` 与 `hash_approval_package()`。随后拆分 package：`resume_versions.content` 只存 `schema_version`、`draft_revision`、职位快照和 sections；事实正文仅存入 `resume_version_facts`。再插入所有 `resume_claims`、唯一 `workflow_approvals` 和一个 `resume.approved` 事件。事件 payload 仅含 run、revision、version、hash 和事实数量。返回从 `workflow_approvals` 读回的字典行，并附完整的内存 package 和 hash。插入失败必须原样抛出以触发整笔事务回滚。

事实不存在、已停用、未确认或修订不匹配时，仓储抛出专用 `ApprovalConflict`；API 将它映射为 HTTP 409，不吞掉其他数据库错误。

仓储内的核心顺序固定如下，版本插入及其后的每条 SQL 都必须保留在同一个事务块：

```python
with conn.transaction():
    job = lock_job(conn, job_id)  # SELECT ... FOR SHARE
    locked_facts = lock_and_validate_facts(conn, retrieved_facts)  # ordered SELECT ... FOR UPDATE
    fact_snapshots = load_cited_fact_revisions(conn, sections, locked_facts)
    package = build_approval_package(
        job=job, sections=sections, fact_snapshots=fact_snapshots,
        draft_revision=draft_revision,
    )
    content_sha256 = hash_approval_package(package)
    version_content = {key: value for key, value in package.items() if key != "facts"}
    version_id = insert_approved_version(conn, job_id, version_content)
    insert_claim_rows(conn, version_id, sections)
    insert_fact_snapshot_rows(conn, version_id, fact_snapshots)
    insert_approval_record(conn, run_id, draft_revision, content_sha256, version_id)
    insert_approval_event(conn, run_id, draft_revision, version_id, content_sha256,
                          len(fact_snapshots))
```

将 `lock_job`、`lock_and_validate_facts`、`load_cited_fact_revisions`、`insert_approved_version`、`insert_claim_rows`、`insert_fact_snapshot_rows`、`insert_approval_record` 和 `insert_approval_event` 作为同模块内小型私有函数实现；数据库错误不得在仓储内吞掉。

- [ ] **Step 4: 运行仓储集成测试**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_t7_approval_integration.py -m integration`

Expected: PASS；注入审计事件失败后所有批准相关表均为零行；正常路径每个引用快照只存一次，且事件只有一条。

- [ ] **Step 5: 提交事务仓储切片**

```powershell
git add src/applypilot/approvals_repo.py src/applypilot/api.py tests/test_t7_approval_integration.py
git commit -m "feat: persist approvals atomically"
```

### Task 4: 序列化编辑/批准并支持 checkpoint 对账

**Files:**
- Modify: `src/applypilot/api.py`
- Modify: `src/applypilot/approvals_repo.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `get_approval()`、`persist_approval()`、`set_graph_reconciled()` 与 `build_approval_package()`。
- Produces: approve/edit API 在同一 run 的 PostgreSQL advisory lock 内核对状态；summary 返回 `resume_version_id`、`draft_revision`、`content_sha256`、`approval_reconciliation_pending`。已存在批准记录时 summary 先从 PostgreSQL 生成快照响应，不依赖 LangGraph checkpoint 可读。

- [ ] **Step 1: 写 checkpoint 失败后同请求恢复测试**

```python
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
    second = client.post(f"/api/workflows/{run_id}/approve",
                         json={"approved": True, "expected_revision": 1})
    assert second.status_code == 200
    assert second.json()["resume_version_id"] == version_id
    assert second.json()["content_sha256"] == first.json()["content_sha256"]
    with db.connect(client.database_dsn) as conn:
        assert conn.execute("SELECT count(*) AS n FROM workflow_approvals WHERE run_id=%s",
                            (run_id,)).fetchone()["n"] == 1


def test_pending_approval_summary_survives_checkpoint_read_failure(client, monkeypatch):
    run_id, _ = reach_waiting_approval(client)
    import applypilot.api as api
    real_build_graph = api.build_graph
    failed = False
    def build_graph_failing_invoke_once(*args, **kwargs):
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
```

在现有 `tests/test_api.py::client` module fixture 中增加 `c.database_dsn = dsn`。新增以下具体 helper；其他 T7 API 测试调用它获取独立 run 和 fact：

```python
def reach_waiting_approval(client):
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
    assert wait_for_status(client, run_id)["status"] == "WAITING_APPROVAL"
    return run_id, fact["id"]
```

- [ ] **Step 2: 确认对账测试先失败**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_api.py::test_approval_retry_reconciles_checkpoint_without_duplicate_rows`

Expected: FAIL，因为 API 当前先推进 checkpoint，且不持久化批准幂等记录。

- [ ] **Step 3: 实现 run 锁、批准先查和两阶段流程**

在 `approvals_repo.py` 增加 `workflow_lock(conn, run_id)` context manager，使用 session-level advisory lock 并在 `finally` 释放。approve 和 edit 都在锁内读取 checkpoint、比较 revision 并完成其 checkpoint 操作。批准首先查 `workflow_approvals`：同 run/修订的批准请求复用记录并只执行必要对账；不同修订或非批准请求返回 409。新批准先调用 `persist_approval()` 提交业务事务，再恢复 LangGraph。捕获 `ApprovalConflict` 并映射到 HTTP 409；图已 READY 时只补 `graph_reconciled`；图仍等待 approval 时恢复一次；图推进异常或状态无法识别时返回 202 并暴露 pending 状态。

`_summarize_state()` 必须先查 PostgreSQL 的批准记录。若存在记录，则由存储的版本 content 还原 sections，并返回 `resume_version_id`、`draft_revision`、`content_sha256`、`waiting=false` 和 `approval_reconciliation_pending`；`graph_reconciled=false` 时 status 为 `APPROVAL_RECONCILIATION_PENDING`，否则为 `READY_TO_APPLY`。此分支不得调用 `graph.get_state()`，所以 checkpoint 暂时不可用也能展示冻结版本，并且编辑/审批入口会因已批准记录而拒绝操作。未批准的 run 才走现有 checkpoint 摘要路径。

API 的操作顺序应保持为：

```python
with db.connect(dsn) as conn:
    with workflow_lock(conn, run_id):
        approval = get_approval(conn, run_id)
        if approval is None:
            state = graph.get_state(config)
            require_waiting_approval_and_current_revision(state, req.expected_revision)
            if not req.approved:
                graph.invoke(Command(resume={"approved": False, "feedback": req.feedback}), config)
                return _summarize_state(run_id)
            approval = persist_approval(
                conn, run_id=run_id, draft_revision=req.expected_revision,
                job_id=state.values["job_id"], sections=state.values["resume"].model_dump(mode="json"),
                retrieved_facts=state.values["retrieved_facts"],
            )
        return reconcile_graph_or_202(graph, config, approval, req)
```

两个 reconcile helper 的职责是：READY checkpoint 只更新 `graph_reconciled`；approval checkpoint 只在请求明确批准且 revision 匹配时 `graph.invoke(Command(...))`；其他 checkpoint 状态保留审批记录并返回 pending。拒绝请求仍走 T6 rejection 分支，不调用 `persist_approval()`。

- [ ] **Step 4: 加入并发和过期修订测试**

```python
def test_two_concurrent_approvals_return_one_version(client):
    run_id, _ = reach_waiting_approval(client)
    responses = post_approval_concurrently(client, run_id, expected_revision=1, count=2)
    assert {r.status_code for r in responses} == {200}
    assert len({r.json()["resume_version_id"] for r in responses}) == 1
    with db.connect(client.database_dsn) as conn:
        assert conn.execute("SELECT count(*) AS n FROM workflow_approvals WHERE run_id=%s",
                            (run_id,)).fetchone()["n"] == 1


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
```

定义 `post_approval_concurrently()` 并在测试文件导入 `ThreadPoolExecutor`：

```python
def post_approval_concurrently(client, run_id, expected_revision, count):
    body = {"approved": True, "expected_revision": expected_revision}
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(client.post, f"/api/workflows/{run_id}/approve", json=body)
                   for _ in range(count)]
        return [future.result() for future in futures]
```

每个请求处理函数使用自己的 `db.connect()`；测试不能共享 psycopg connection。两个请求必须用相同 run 和草稿修订。

- [ ] **Step 5: 运行 API / 对账集成测试**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_api.py`

Expected: PASS；同请求网络重试复用同一版本，两个并发批准只写一个版本/事件，过期修订不写数据，旧 T6 编辑和退回流程继续通过。

- [ ] **Step 6: 提交 API 对账切片**

```powershell
git add src/applypilot/api.py src/applypilot/approvals_repo.py tests/test_api.py
git commit -m "feat: reconcile approval checkpoints idempotently"
```

### Task 5: 让批准导出使用冻结快照并完成端到端验收

**Files:**
- Modify: `src/applypilot/api.py`
- Modify: `src/applypilot/templates/review.html`
- Modify: `tests/test_api.py`
- Modify: `docs/design.md`
- Modify: `docs/roadmap.md`
- Modify: `docs/validation-and-demo.md`
- Create: `docs/t7-execution.md`

**Interfaces:**
- Consumes: 持久化批准记录与 `resume_versions.content` 中的 `job_snapshot`、`sections`；引用事实从 `resume_version_facts` 读取。
- Produces: 新批准版本的导出不查询可变职位标题或事实正文；旧版本走兼容读取路径。

- [ ] **Step 1: 写批准快照不会漂移测试**

```python
def test_approved_docx_uses_frozen_job_snapshot_after_source_changes(client):
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
        assert frozen_fact["content"] == "Built batch API"
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
```

在实现中用 `workflow_approvals.version_id` 关联到 `resume_versions` 再取得 job ID；测试不得把 `version_id` 误当 job ID。

- [ ] **Step 2: 确认导出测试先失败**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_api.py::test_approved_docx_uses_frozen_job_snapshot_after_source_changes`

Expected: FAIL，因为现有导出直接读取 `jobs.title`。

- [ ] **Step 3: 实现快照优先的导出和查询**

当批准包有 `job_snapshot` 时，使用其 `title` 和简历 sections 生成 DOCX；旧版本缺少快照时沿用原 `jobs.title` 路径。查询版本同时返回存储的 `content_sha256` 与 revision，不重新读取事实当前行计算结果。

```python
saved = version["content"]
job_title = saved.get("job_snapshot", {}).get("title") or version["title"] or "未命名职位"
sections = ResumeSections.model_validate(saved["sections"])
data = render_docx(job_title, sections)
```

- [ ] **Step 4: 更新设计和验证文档**

在审核页对已批准 run 使用 `resume_version_facts.snapshot` 显示来源，不再通过 `facts_repo.get_fact()` 读取可变事实当前行。`review.html` 为 `APPROVAL_RECONCILIATION_PENDING` 明确展示“已批准、待恢复对账”，提供冻结版本下载链接并隐藏编辑/审批操作；`READY_TO_APPLY` 展示已冻结版本下载链接。API 集成测试验证事实更新后审核页仍显示批准时快照。按模板现有方式完成人工 UI 验收，并记录结果。

在 `README.md` 更新 T7 完成状态；在 `docs/design.md` 将 T7 的 PostgreSQL 批准记录、hash 绑定、对账 pending 状态写入审批契约；在路线图标记 T7 完成并把下一项写为 T8；在验证手册和新建的 `docs/t7-execution.md` 记录真实迁移命令、测试结果、故障注入结果、已知限制。不得将未实测的重启扫描或多实例恢复写为已完成。

更新 `tests/test_api.py` 中旧的“同一 run 第二次 approve 返回 409”断言：相同 `expected_revision` 的成功批准重试应返回 200，并断言版本 ID、draft revision 和 hash 与首次响应一致。

- [ ] **Step 5: 运行 T7 集成与完整回归**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_t7_approval_integration.py tests/test_api.py`

Expected: PASS；覆盖迁移、事务回滚、事实快照、同请求重试、并发、对账 pending 和 DOCX 历史稳定性。

Run: `.venv/Scripts/python.exe -m pytest -q`

Expected: PASS；全部既有和新增测试通过，只有已记录的既有弃用警告。

- [ ] **Step 6: 检查差异并提交交付记录**

```powershell
git diff --check
git status --short
git add README.md src/applypilot/api.py src/applypilot/db.py src/applypilot/approvals_repo.py src/applypilot/approval_snapshots.py db/migrations/003_atomic_approval_snapshot.sql tests/test_api.py tests/test_t7_approval_integration.py tests/test_approval_snapshots.py tests/test_runtime.py tests/test_db_integration.py docs/design.md docs/roadmap.md docs/validation-and-demo.md docs/t7-execution.md
git commit -m "feat: freeze approved resume snapshots"
```

验收时人工确认新增批准事件只有一条、版本内 hash 与 API 响应一致、DOCX 读取快照，并检查无敏感数据泄漏。
