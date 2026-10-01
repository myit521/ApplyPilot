# 浏览器集成与开源参考

更新：2026-09-30；开源调研快照：2026-09-29。调研检查了 GitHub 仓库、源码、manifest、许可证和测试结构，**没有安装运行这些项目，也没有验证真实招聘网站兼容性**。仓库后续可能变化，采用前需固定实际 commit 并复核。

## 1. 决策

插件能减少重复填写，但不能解决 ApplyPilot 的事实可信性、批准版本和投递状态问题。因此先把它作为输入/输出入口，后端继续负责确认资料、受限生成、版本和记录。

路线：结构化资料及批准包 → 外部插件 JSON 桥接 PoC → 可选导出 → 自有薄扩展。两周必交付只包含第一步，其他阶段有独立预算和停止条件，见[路线](roadmap.md)。

默认考虑国内企业校招，不承诺全网站通用。真实站点、具体表单和允许的操作范围尚未确认。无需登录真实账号即可先用合成资料和本地表单验证设计。

## 2. 参考项目与取舍

| 项目与已查许可 | 观察到的能力 | 可借鉴部分 | 接入决定与限制 |
| --- | --- | --- | --- |
| [OpenJobAutofill](https://github.com/Br1an67/OpenJobAutofill)，MIT | 浏览器插件，结构化资料备份导入，字段目录映射 | 作为外部 JSON 导出验证目标 | 优先 PoC；不是现成后端 API；content 脚本较大，未见完整测试体系，不整体复制 |
| [AI-Resume-Form-Filling-Assistant](https://github.com/1lck/AI-Resume-Form-Filling-Assistant)，GPL-3.0 | 侧边栏、增量填写、字段路径映射、测试与 CI | 交互、重复填写控制、映射变换和测试思路 | 源码包含 valuePreview 进入模型提示，不能声称零资料发送；复制改编需遵守 GPL，不能直接改标 MIT |
| [AutoApply](https://github.com/geckguy/AutoApply)，MIT | 扩展 + FastAPI，字段计划与执行回读，后端来源限制 | 薄扩展/后端分工、FillInstruction、失败分类 | 最接近可借鉴架构；不引入第二套后端，不照搬宽权限 |
| [local-resume-autofill](https://github.com/LKRCharon/local-resume-autofill)，MIT | 本地资料保险库、适配器、离线执行和 fixture 测试 | 本地优先、最小权限、兼容性等级 | 开发预览；声明的站点适配主要是 fixture 验证，不能写成真实网站已兼容 |
| [Resume-Matcher](https://github.com/srbhr/Resume-Matcher)，Apache-2.0 | 简历匹配/改写、评测与确认事务测试 | 不编造雇主、身份保持、响应丢失后确认重放等测试 | 借鉴质量门禁，不照搬 ATS 分数营销或整套系统 |
| [Reactive Resume](https://github.com/reactive-resume/reactive-resume)，MIT | 结构化编辑、模板、导入导出 | 编辑体验、字段模型、导出布局 | 后续参考，不把重型编辑器移入两周 MVP |
| [JSON Resume schema](https://github.com/jsonresume/jsonresume.org/tree/master/packages/schema)，MIT | 通用简历字段约定 | 对外字段命名和兼容性 | 旧 resume-schema 仓库已归档；通用 schema 不包含 ApplyPilot 的事实修订和批准来源 |

许可证为当日读取结果，不等于完成依赖合规。实际采用时记录仓库 URL、commit、文件范围、修改和许可，新增第三方声明；当前仅参考不虚构已采用清单。

旧 AIHawk 自动投递项目链接已转向 [invisible_playwright_mcp](https://github.com/feder-cr/invisible_playwright_mcp)，当前用途与原介绍不同，不按旧热度推荐。未确认完整开源源码的商业插件不列为可直接复用项目。

## 3. 第一阶段：外部 JSON 桥接

[OpenJobAutofill options.js](https://github.com/Br1an67/OpenJobAutofill/blob/main/src/options.js) 的备份导入使用以下外层字段。下例仅说明已观察的格式，不是可直接用于生产的完整资料：

```json
{
  "format": "OpenJobAutofillProfileBackup",
  "version": 1,
  "exportedAt": "2026-09-29T00:00:00.000Z",
  "profileV2": {
    "schemaVersion": 2,
    "updatedAt": "2026-09-29T00:00:00.000Z",
    "sections": {},
    "customSections": []
  }
}
```

sections 中 simple 分区使用 values/custom，repeat 分区使用 items。插件会按自身已知分区键归一化；“JSON 能解析”不等于“字段正确导入”。适配器必须测试归一化后的字段、重复组、日期、缺失值和中文标签。

ApplyPilot 内部使用自己的版本化资料结构，再由单一适配器转换；不让第三方备份格式成为核心领域模型。只导出人工确认的资料和指定批准版本，用户预览后下载。

导出记录保存版本、资料修订、生成时间、格式版本与文件哈希，状态为 EXPORTED。外部插件可能编辑或覆盖内容，后端无法观察实际最终值，因此不能自动标记 FILLED 或 SUBMITTED。

PoC 用合成资料验证三类表单：原生基础控件、教育/经历重复组、动态或受控控件。4–6 小时内记录成功字段、失败原因及人工操作；不通过就停止桥接，核心流程继续手动填写。

## 4. 后续自有薄扩展

推荐职责：

- content script：扫描当前表单、执行经确认的字段动作、回读最终值。
- side panel/popup：展示字段、来源、修改前后值、跳过项和失败原因。
- background：持有本机服务配对凭据，与后端通信；不把凭据交给网页。
- 后端：返回已批准数据、生成受限映射计划、验证版本、保存执行摘要。
- 模型：只建议字段 ID 到资料路径的映射及白名单变换，不生成任意 JS 或可执行选择器。

[AutoApply 表单契约](https://github.com/geckguy/AutoApply/blob/main/backend/models/form_schema.py) 和[执行器](https://github.com/geckguy/AutoApply/blob/main/extension-chrome/content/filler.js) 可参考。其安全检查和宽站点权限须分别评估，不能整体复制后声称安全。

### 建议数据契约（尚未实现）

| 对象 | 必要字段 | 校验要求 |
| --- | --- | --- |
| ApplicationPackage | schema_version、package_id、job_id、resume_version_id、profile_revision、fact_revisions、payload_hash、approved_at | 内容来自批准快照；身份资料与生成文案分开；修订变更需重新确认 |
| FormScan | origin、page_url、tab/session 标识、form_fingerprint、fields | 网页是不可信输入；剔除隐藏/密码/验证码和无关文本 |
| FillPlan | plan_id、package_id、fingerprint、expires_at、instructions | 绑定当前来源与表单；过期或导航变化须重新扫描 |
| Instruction | field_id、source_path、action、transform、reason、review_required | 路径和动作白名单；缺失值跳过，禁止猜测 |
| ExecutionReport | plan_id、字段成功/跳过/失败、原因、时间 | 回读实际值；默认不保存敏感原文；不得据此宣称已提交 |

重复组使用稳定组 ID，不能把教育第二条误映射到第一条。映射缓存绑定 schema/adapter 版本和表单指纹。模型给出的 confidence 仅供排序，不能当作已测准确概率。

允许初期动作 fill/select/check/skip；上传文件、签署同意条款、密码、验证码交人工。默认只填写空值，覆盖已有值须在预览中明确同意。恢复历史计划不自动重放。

目标状态：PREVIEW → CONFIRMED → EXECUTING → FILLED/PARTIAL/FAILED；页面变化为 STALE，用户取消为 CANCELLED。这些是拟议扩展状态，不是当前数据库已有实现。Application 的投递状态独立维护。

## 5. 权限与数据流

参考 Chrome 官方 [activeTab](https://developer.chrome.com/docs/extensions/develop/concepts/activeTab) 和[网络请求说明](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests)。

优先 activeTab、scripting、storage，用户触发时授予当前页面能力；访问本地后端需要另外声明相应主机权限，不能认为 activeTab 自动允许跨域请求。避免默认所有站点、所有 frame 权限。

本地服务须校验 Host/Origin，并使用可撤销配对凭据；CORS 不是鉴权。只向 content script 下发当前字段所需数据。扩展凭据留在 background，模型密钥留在后端。

映射模型尽量只接收脱敏字段结构，不发送真实资料值；网页标签、选项、页面上下文也可能含个人信息，应清理。简历生成本身可能需发送必要事实，必须明确告知数据流，不能笼统宣传“所有资料不出本机”。

即使没有点击提交，网站也可能自动保存输入。因此用户确认填写前应知道数据将写入目标站点。不得用“不自动提交”暗示没有数据传输。

## 6. 验收与适配等级

兼容性分为 declared（仅声明）、fixture_verified（受控页面验证）、manually_verified（记录真实站点、日期、版本和允许范围）。不能将 fixture 成功直接升级为全网站兼容。

最低回归覆盖：普通控件、动态控件、重复组、日期/布尔转换、页面导航、过期计划、已填值保护、部分失败、取消、敏感字段、提交按钮不被执行、无多余网络请求。

记录成功填写字段/实际尝试字段、支持字段覆盖率、人工纠正次数及同一 fixture 的操作时间。分母、排除项和失败样例必须公开；没有运行数据就不写提效百分比。“测试中未触发提交”只是对应样例的结果，不是所有网站的绝对保证。

## 7. 接入前检查单

- 固定第三方 commit，复核许可证、依赖和权限变化。
- 使用合成资料完成导入及字段回读，不上传真实简历测试未知服务。
- 明确站点允许范围，不绕过风控，不处理验证码，不批量骚扰投递。
- 数据契约、错误回退、人工确认和版本来源均通过，再决定是否开发自有扩展。
