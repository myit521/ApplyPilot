# ApplyPilot

面向校招的事实约束简历定制与投递追踪工具：把一个职位的要求、可引用的个人经历、人工批准的简历版本和投递结果连起来。

当前交付范围是本机单用户、单应用实例的 MVP 候选。开发分支为 `codex/t26-delivery-integration`；[PR #1](https://github.com/myit521/ApplyPilot/pull/1) 保持草稿、尚未合并，`main` 的功能仍停在 T6。下文描述开发分支，请勿将其当作默认分支已有能力。

## 使用流程

确认资料与事实 → 粘贴 JD → 核对逐项证据与缺口 → 生成、退回或编辑并校验 → 人工批准并冻结版本 → 导出 DOCX → 用户自行投递后记录结果。

- 事实有来源、确认状态和修订；生成表述携带引用，数字、技能规则和语义复核共同限制无依据改写，最终内容由用户核对。
- 新任务在入队时保存职位快照，复用已通过门禁的解析；批准时用业务事务冻结职位、事实、资料和版本，支持同修订安全重放及图状态对账。
- 持久任务和 checkpoint 支持单实例有界重试、取消及中断恢复；模型调用日志区分已报告与未知用量，不承诺外部调用恰好一次或总费用配额。
- 手工投递记录只绑定已批准版本；`user_reported` 是用户自述，`unknown` 是未知结果。下载文件不代表已提交或招聘网站已确认。

FastAPI 提供 API 和审核页面，PostgreSQL 承担业务事务与快照，LangGraph 用于人工中断和恢复执行位置。向量检索是可选能力；应用容器未包含可选嵌入模型栈。Playwright 用于本项目页面测试，浏览器扩展和招聘网站自动填写尚未实现。

## 已有验证

最近一次代码验收基准为 `dc475bf`（T34，2026-10-23）：

| 证据 | 实际范围 |
| --- | --- |
| [T34 本机完整回归](docs/t34-execution.md) | 399 项通过、3 条既有弃用警告；包含临时 PostgreSQL、恢复及 Edge 浏览器，供应商 HTTP 为替身；修复冻结标签及指定旧版选择 |
| [T33 托管 CI](https://github.com/myit521/ApplyPilot/actions/runs/39399600353) | 已成功，只执行非 integration 测试，不包含本机浏览器或容器验收；T34 的两处回归属 integration，CI 不执行，托管状态以 PR 当前检查为准 |
| [T27 真实模型单例](docs/t27-execution.md) | 一个合成案例、4 次受限 API 请求；使用演示适配器，不是默认适配器完整浏览器真实模型验收 |

这些证据不代表真实招聘场景质量、生产可靠性或性能提升。检索对照仅限固定小型合成集，详见 [T24](docs/t24-execution.md) / [T25](docs/t25-execution.md)。完整历史与接口契约保留在[历史 README](docs/history-before-t33.md)，设计图和计划不作为已实现证据。

## 启动

本机验证环境为 Windows、Python 3.13.5 和 Docker Desktop；应用镜像另在本机 Linux/x86_64 构建验证。以下两种启动方式选一种，均在仓库根目录执行。已有数据升级先备份、停止旧进程，再迁移启动，不删除数据卷。

**Compose：**

```powershell
docker info
docker compose up --build -d --wait
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

应用和数据库分别只绑定 `127.0.0.1:8000` 与 `127.0.0.1:5432`。Compose 会读取 `.env` 和进程环境；无密钥启动及独立演示按[交付检查单](docs/delivery-checklist.md)，不要把个人工作数据库用于故障彩排。保存职位不调用模型；解析和生成需要模型配置，会发送所选材料并产生费用。

**Windows 直接运行 Python：**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
docker compose up -d --wait db
$env:DATABASE_URL = 'postgresql://applypilot:applypilot@localhost:5432/applypilot'
$env:PYTHONPATH = 'src'
python -m applypilot.db
# 需要真实模型时才配置；不把密钥写入仓库或录屏。
$env:DEEPSEEK_API_KEY = '<自己的密钥>'
$env:DEEPSEEK_MODEL = 'deepseek-chat'
python -m uvicorn applypilot.api:app --app-dir src --host 127.0.0.1 --port 8000 --log-config src/applypilot/logging.json
```

Python 不自动读取 `.env`；迁移需执行至 006，Compose 应用启动时自动迁移。首次可选向量模型加载可能下载文件。默认凭据仅适用于本机演示，不直接暴露公网。

入口：`/profile` 确认资料和事实，`/jobs` 保存/解析/匹配职位，首页查看任务，`/review/{run_id}` 审核，`/applications` 留痕，`/docs` 查看实际 API。

`/health/live` 只表示进程存活，`/health/ready` 检查数据库、checkpoint 和迁移，不验证模型质量或密钥。checkpoint 初始化或长连接失效后修复依赖并重启，本阶段不自动重连。

## 测试与无费用彩排

准备上述 Python 依赖后执行；浏览器彩排还需要 Playwright 包、本机 Edge 和 Docker。测试临时替换供应商 HTTP，不读取真实模型密钥；不能把跳过视为通过。

```powershell
python -m pytest -p no:cacheprovider -q -m "not integration"
python -m pytest -p no:cacheprovider -q tests/test_t13_browser_rehearsal.py
python -m pytest -p no:cacheprovider -q --tb=short
```

具体演示顺序、可选截图/摘要和失败恢复见[交付检查单](docs/delivery-checklist.md)。自动彩排是临时服务上的无头浏览器测试，结束后服务和数据库退出，不是可常驻点击的假模型演示模式。旧 `scripts/smoke_e2e.py` 已停用，不读取简历或调用模型。

## 交付与发布边界

- 当前只支持本机单用户单实例；多用户、认证、归属校验、限流、数据删除与持久化用量配额尚未交付。
- 默认适配器完整浏览器真实模型流程、真实岗位质量、多页真实 DOCX 视觉表现和招聘网站兼容仍未验收。
- 不实现自动提交、验证码绕过或批量骚扰投递。日志及规则不保证零幻觉、零费用或全部错误被捕获。
- 尚无独立 LICENSE，许可需维护者确定。T28 已清理确认过的公开可达隐私标识；旧 GitHub 对象、缓存、Actions 日志、fork 或其他副本未证明清除。公开材料只用合成数据，不提交真实简历、密钥或导出文件。

| 文档 | 用途 |
| --- | --- |
| [交付检查单](docs/delivery-checklist.md) | 当前版本、启动、演示顺序与剩余发布条件 |
| [产品与架构](docs/design.md) | 用户问题、数据职责与一致性设计 |
| [开发路线](docs/roadmap.md) | 历史任务、当前范围冻结及需求驱动的后续项 |
| [验证与恢复](docs/validation-and-demo.md) | 验证矩阵、故障及恢复要求 |
| [简历与面试证据](docs/resume-evidence.md) | 可用表述、实现证据和面试追问 |
| [历史 README](docs/history-before-t33.md) | 原阶段进度、历史测试数据与接口契约 |
