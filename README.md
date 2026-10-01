# ApplyPilot

基于个人事实约束的校招简历定制与投递追踪助手：让生成内容有来源、批准版本可追溯、投递结果有记录。

> 当前是 Python 原型，尚未完成产品 MVP。产品完成度基线为 2026-09-29 的 `f586664` 审计；2026-10-01 已补 T1 运行基线，见 [T1 验证记录](docs/t1-execution.md)。设计计划不作为实现证据。

## 当前能力与限制

已具备 JD 解析、事实检索、分段简历生成与校验、LangGraph 人工中断、PostgreSQL checkpoint、简单审核页面及基础 DOCX 导出。

2026-10-01 在全新隔离虚拟环境按锁文件安装后，完整测试 **57 项通过**；使用临时 PostgreSQL、已缓存嵌入模型及生成模型替身。T1 覆盖无数据库导入、启动失败、连接释放和就绪检查；不代表真实模型质量、DOCX 排版或招聘网站兼容性已验证。条件见[验证手册](docs/validation-and-demo.md)。

尚未完成：

- 事实导入后的人工确认、逐项职位匹配分析。
- 用户反馈进入再生成、人工编辑后完整校验。
- 可靠的审批冻结事务、幂等任务与重启恢复。
- 投递记录写入、结果更新与来源标记。
- 浏览器辅助填写。当前没有接通 Playwright 或浏览器扩展。

现有校验存在数字子串误判、复核解析失败放行、空草稿可批准等问题。当前结果须逐条人工核对；不能宣称「保证不编造」「生产级」或「自动恢复」。检索实际使用 `ILIKE` 与向量召回，不是旧文档所称的 PostgreSQL 全文检索。详见[审计报告](docs/audit-2026-09-29.md)。

## 目标使用流程

确认个人资料与事实 → 粘贴 JD → 查看逐项证据与缺口 → 生成、编辑并校验 → 批准版本 → 用户自行投递 → 记录渠道、时间、版本和结果。

两周 MVP 不包含自动提交、验证码绕过或批量投递。扩展填写作为后续增强。

## 本地运行原型

已验证 Windows + Python 3.13.5。准备 Docker Desktop 和模型服务配置；requirements.txt 固定直接依赖，requirements.lock 固定该平台的传递依赖。其他系统/Python 版本尚未验证。PowerShell 在当前代码目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
docker compose up -d --wait db
$env:DATABASE_URL = 'postgresql://applypilot:applypilot@localhost:5432/applypilot'
$env:DEEPSEEK_API_KEY = '<替换为自己的密钥，仅保存在本地环境>'
$env:DEEPSEEK_MODEL = 'deepseek-chat'
python -m uvicorn applypilot.api:app --app-dir src --host 127.0.0.1 --port 8000
```

页面位于 `http://127.0.0.1:8000`，接口文档位于 `/docs`。当前页面不能完成所有资料和职位录入，实际接口以自动生成的接口文档为准。

已知运行约束：

- Jinja2 已加入直接依赖；导入模块和 create_app 不连接数据库，lifespan 启动阶段初始化 checkpoint。
- `/health/live` 表示进程存活；`/health/ready` 检查 checkpoint 连接、当前数据库和必要业务表。它不验证模型密钥、模型下载、列级迁移或生成效果。
- checkpoint 初始化失败时存活接口仍可用，就绪和业务接口返回 503；修复依赖后重启。运行中 checkpoint 连接失效会报告未就绪，本阶段不自动重连。
- 当前代码不会自动读取 `.env`，需设置进程环境变量。
- 首次向量模型加载可能下载文件，离线使用需事先准备缓存。
- Compose 仅启动数据库；初始化 SQL 主要适用于新数据卷，不是迁移机制。不要删除数据卷来完成升级。

默认凭据仅用于本机演示；服务不要直接暴露公网。不要提交真实简历、密钥或导出文件。

## 测试

```powershell
python -m pytest -q -m "not integration"
# 以下需要 Docker；API 测试可能加载嵌入模型
python -m pytest -q
```

无需为测试收集设置 DATABASE_URL 或先启动导入数据库。

## 文档导航

| 文档 | 用途 |
| --- | --- |
| [当前项目审计](docs/audit-2026-09-29.md) | 实现、验证边界与缺陷 |
| [产品与架构](docs/design.md) | 目标产品、职责与一致性规则 |
| [开发路线](docs/roadmap.md) | P0/P1/P2、任务依赖与验收 |
| [浏览器集成](docs/browser-integration.md) | 开源参考、接口与分阶段接入 |
| [验证与演示](docs/validation-and-demo.md) | 测试复现、回归矩阵与演示 |
| [简历表达](docs/resume-evidence.md) | 技术亮点的证据要求与面试准备 |

## 发布状态

旧 README 曾声明 MIT，但仓库缺少独立 LICENSE 文件。发布前由维护者确认许可并补齐。参考外部项目不代表已引入其代码；实际采用时登记来源、固定版本并保留许可声明。历史提交可能保留曾误提交的简历产物，公开前需另行检查历史中的个人信息。
