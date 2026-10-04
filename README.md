# ApplyPilot

基于个人事实约束的校招简历定制与投递追踪助手：让生成内容有来源、批准版本可追溯、投递结果有记录。

> 当前是 Python 原型，尚未完成产品 MVP。产品完成度基线为 2026-09-29 的 `f586664` 审计；已完成 T1 运行基线、T2 资料与事实确认、T3 职位录入、T4 逐项证据匹配、T5 校验门禁及 T6 编辑与反馈（T6 于 2026-10-04 验收），见相应执行记录。设计计划不作为实现证据。

## 当前能力与限制

已具备 JD 解析、事实检索、分段简历生成与校验、LangGraph 人工中断、PostgreSQL checkpoint、简单审核页面及基础 DOCX 导出。

2026-10-04 在按锁文件安装的隔离虚拟环境中，完整测试 **139 项通过**；使用临时 PostgreSQL、无向量替身及生成模型替身。覆盖无数据库导入、生命周期、草稿确认、修订冲突、迁移、职位校验/保存/历史/显式解析、关键词证据状态与引用、数字和技能边界、语义复核失败关闭、空草稿阻断、反馈再生成、编辑重验与过期修订冲突；不代表真实模型质量、DOCX 排版或招聘网站兼容性已验证。条件见[验证手册](docs/validation-and-demo.md)。

尚未完成：

- 可靠的审批冻结事务、幂等任务与重启恢复。
- 投递记录写入、结果更新与来源标记。
- 浏览器辅助填写。当前没有接通 Playwright 或浏览器扩展。

简历校验已修复数字子串误判、技能子串扩大及复核解析失败放行；空简历和空白表述不能进入审批。数字/技能规则及语义复核仍无法证明经历真实或语义判断永不出错，最终内容须由用户核对；不能宣称「保证不编造」「生产级」或「自动恢复」。职位匹配报告的状态基于可见的关键词规则；旧候选召回仍可能因 `ILIKE` 子串匹配产生噪声，候选不能单独成为支持证据。详见[审计报告](docs/audit-2026-09-29.md)与[T5 执行记录](docs/t5-execution.md)。

## 目标使用流程

确认个人资料与事实 → 粘贴 JD → 查看逐项证据与缺口 → 生成、编辑并校验 → 批准版本 → 用户自行投递 → 记录渠道、时间、版本和结果。

两周 MVP 不包含自动提交、验证码绕过或批量投递。扩展填写作为后续增强。

## 本地运行原型

已验证 Windows + Python 3.13.5。准备 Docker Desktop；职位录入和历史查询不需要模型密钥，显式解析与生成需要模型服务配置。requirements.txt 固定直接依赖，requirements.lock 固定该平台的传递依赖。其他系统/Python 版本尚未验证。PowerShell 在当前代码目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
docker compose up -d --wait db
$env:DATABASE_URL = 'postgresql://applypilot:applypilot@localhost:5432/applypilot'
$env:PYTHONPATH = 'src'
python -m applypilot.db
$env:DEEPSEEK_API_KEY = '<替换为自己的密钥，仅保存在本地环境>'
$env:DEEPSEEK_MODEL = 'deepseek-chat'
python -m uvicorn applypilot.api:app --app-dir src --host 127.0.0.1 --port 8000
```

页面位于 `http://127.0.0.1:8000`，接口文档位于 `/docs`。`/profile` 可录入联系资料、导入/新增/编辑/确认/停用事实。修改重置为草稿，确认必须针对当前修订；教育信息在教育事实中维护。`/jobs` 可录入职位、查看分页历史和 JD 原文；保存不调用模型，点击解析才会发送 JD 到模型服务。实际接口以 `/docs` 为准。

已知运行约束：

- Jinja2 已加入直接依赖；导入模块和 create_app 不连接数据库，lifespan 启动阶段初始化 checkpoint。
- `/health/live` 表示进程存活；`/health/ready` 检查 checkpoint 连接、当前数据库和必要业务表。它也要求 T2 迁移版本与资料/历史表就绪，但不验证模型密钥、模型下载或生成效果。
- checkpoint 初始化失败时存活接口仍可用，就绪和业务接口返回 503；修复依赖后重启。运行中 checkpoint 连接失效会报告未就绪，本阶段不自动重连。
- 当前代码不会自动读取 `.env`，需设置进程环境变量。
- 首次向量模型加载可能下载文件，离线使用需事先准备缓存。
- Compose 仅启动数据库并建立基础表。新库和已有数据卷都必须执行 `python -m applypilot.db`，按版本执行 `db/migrations`；重复执行安全。已有事实保留为 legacy 草稿，清除旧向量，须逐项复核确认。不要删除数据卷来完成升级。

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
| [T5 执行记录](docs/t5-execution.md) | 数字/技能边界、语义复核与失败门禁 |
| [T6 执行记录](docs/t6-execution.md) | 反馈再生成、文本编辑、修订与差异 |
| [简历表达](docs/resume-evidence.md) | 技术亮点的证据要求与面试准备 |

## 发布状态

旧 README 曾声明 MIT，但仓库缺少独立 LICENSE 文件。发布前由维护者确认许可并补齐。参考外部项目不代表已引入其代码；实际采用时登记来源、固定版本并保留许可声明。历史提交可能保留曾误提交的简历产物，公开前需另行检查历史中的个人信息。

## T2 确认契约

API 创建/导入事实一律为草稿。模型无法指定 ID、修订、确认状态或升级证据；人工填写证据也只代表用户声明，不是第三方核验。事实和联系资料更新/确认都要求 `expected_revision`，过期返回 409。历史快照与当前记录在同一事务写入，历史表禁止更新/删除。

教育事实的 school、degree、major、start_date、end_date 是唯一可编辑教育来源；资料 GET 只链接这些事实及各自状态/修订。确认的启用教育事实在 top-K 之外保留，并直接构造教育段落。待审批草稿引用的事实发生变化后，批准或重新生成会返回 409，需创建新工作流。完整审批冻结事务、版本快照和并发原子性仍属 T7。

`scripts/smoke_e2e.py` 使用真实模型，会发生外部请求和费用；提取后打印完整草稿，只有人工输入 CONFIRM 才逐项确认。

## T3 职位录入契约

`POST /api/jobs` 必填 title、company、raw_text，source 默认 paste；url 可选。保存成功返回完整职位记录（201），不自动解析。JD 原文保留空白和换行；职位/公司最多各 200 字符，来源最多 100，JD 最多 30,000，URL 最多 2,048；空白必填项或非法 URL 返回 422。URL 只保存来源，不抓取网页。

`GET /api/jobs?limit=20&offset=0` 返回 items、total、limit、offset，按 ID 倒序；limit 范围 1–100。`GET /api/jobs/{id}` 查询完整记录。`POST /api/jobs/{id}/parse` 显式调用模型，结果写入 parsed，不覆盖手动职位名；解析失败不删除原记录。旧调用方需补 title/company，并把自动解析改为显式调用；冒烟脚本已同步。

页面保存失败时保留输入；网络错误后可先查历史再决定是否重试，当前录入不保证幂等。职位详情页可生成逐项证据报告，列出支持/部分支持/无证据/未知、命中的关键词与事实修订引用。默认只用关键词基线；可选语义候选单独展示，不改变证据状态。实现与验证见 [T3 执行记录](docs/t3-execution.md) 与 [T4 执行记录](docs/t4-execution.md)。


## T4 证据匹配契约

`GET /api/jobs/{id}/match` 需要已解析 JD，按必备项、加分项、职责与技术关键词输出 `requirements`；每项包含 `status`、判定 `reason`、关键词、已匹配词和事实证据。证据限定启用且已确认的事实，含事实 ID、修订、来源、类型、最多 500 字摘录、关键词及命中依据。未解析返回 409，不确定的解析项标成 unknown。

状态按解析出的关键词逐一精确匹配事实技能标签或正文：全部找到为 supported，部分找到为 partial，一个都没找到为 no_evidence；解析没有给出可核对关键词时为 unknown。状态是可解释的规则结果，不证明经历真实或语义等价。默认不调用嵌入模型；传 `include_semantic=true` 才会返回额外候选，语义相似本身不会构成证据。报告实时读取事实，不保存历史快照；后续版本冻结按 T7 实施。细节见 [T4 执行记录](docs/t4-execution.md)。

## T5 简历校验门禁

只有至少一条非空、有有效事实引用且通过数字与技能边界校验的简历才能进入语义复核和人工审批。复核服务异常、空响应、无效 JSON、格式不符或引用了不存在的主张都会以 `SEMANTIC_REVIEW_UNAVAILABLE` 失败，不会被解释为“无越界”；工作流 API 返回状态和错误供人工处理。实现与测试见 [T5 执行记录](docs/t5-execution.md)。

## T6 编辑与反馈

拒绝意见会作为下一轮生成请求的一部分；审核页允许修改已有主张文本，事实引用由服务端保留，修改后重新运行 T5 校验。草稿修订号保护编辑和批准操作免于覆盖新版本，审核页显示相对上一修订的差异。修改已批准版本须创建新工作流；原子冻结和历史快照仍由 T7 完成。实现与测试见 [T6 执行记录](docs/t6-execution.md)。
