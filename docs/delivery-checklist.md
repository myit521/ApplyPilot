# 本机交付与演示检查单

更新：2026-10-23。范围是单用户、单应用实例的 MVP 候选，不含招聘网站自动提交。

## 交付版本

当前代码验收基准为 `dc475bf`（T34，2026-10-23），开发分支为 `codex/t26-delivery-integration`。[PR #1](https://github.com/myit521/ApplyPilot/pull/1) 的最终 head 以 PR 为准。PR 保持草稿、未合并，base 为 `19115a7`，main 功能仍到 T6。不要把旧的分支领先提交数当作当前状态。

最近一次本机完整回归为 **399 passed、3 warnings in 148.20s**，见 [T34](t34-execution.md)。T34 修复投递选择框优先显示冻结公司/职位，以及从审核链接指定的旧版本被最近 100 条排除的问题；DSH 只读复核，主审执行测试。T33 托管[离线 CI](https://github.com/myit521/ApplyPilot/actions/runs/39399600353) 已成功，T34 CI 以 PR 当前检查为准；两处新回归属于 integration，托管离线 CI 不执行它们，本机全量结果才是相应证据。T26/T28 的历史 SHA 与公开可达历史清理范围保留在原记录和 [T28 汇总](evaluation/t28-privacy-audit.json)，历史记录不覆盖改写。

## 无密钥启动检查

在项目根目录执行，Docker Desktop 需要可用：

```powershell
docker info
docker compose up --build -d --wait
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

应用只绑定本机 8000，数据库只绑定本机 5432。先确认端口空闲；已有数据库保留数据，不删除卷。Compose 会读取环境和默认 `.env`，要演示无密钥场景，应使用独立空 env 文件并确保进程没有设置 `DEEPSEEK_API_KEY`。直接运行 Python 不自动读取 `.env`。

访问 `/jobs`，录入合成职位，保存后查看原文和历史；无模型密钥时解析应提示“模型尚未配置，职位已保留”，HTTP 503。`docker compose restart app` 后等待 ready 恢复，再检查职位仍在。正常关闭用 `docker compose down`，不要对已有数据使用 `--volumes`。本次隔离验证端口为 18026/15426，结果见 [T26](t26-execution.md)。

## 自动化合成主流程

准备 README 所述 Python 测试环境、安装锁定依赖，并让 Docker 可用：

```powershell
python -m pytest -p no:cacheprovider -q tests/test_t13_browser_rehearsal.py
python -m pytest -p no:cacheprovider -q --tb=short
```

第一条为 T32 增强的 T13 浏览器流程，使用默认 DeepSeek 适配器、供应商 HTTP 替身、临时数据库和合成资料；覆盖退回、编辑重校验、批准、DOCX 内容及人工记录，并核对六次调用日志。需安装 Playwright Python 包及本机 Edge，缺少依赖时跳过不能当作演示通过。详见 [T32](t32-execution.md)；合成配置和 HTTP 替换只在测试中生效，不是运行时开关。该流程已在 T34 的 399 项完整回归中再次通过，不验证真实招聘网站或真实生成质量。

这是无头浏览器自动验收：测试结束时临时服务和数据库退出。不能在结束后访问临时端口，也不把测试替身称为常驻演示模式。无需向测试提供个人简历或真实模型密钥。

需要本机合成截图与调用摘要时，在项目根目录执行以下可选命令；不将产物提交到公开仓库：

```powershell
$previousArtifactDir = [Environment]::GetEnvironmentVariable('APPLYPILOT_DEMO_ARTIFACT_DIR', 'Process')
try {
    $env:APPLYPILOT_DEMO_ARTIFACT_DIR = Join-Path (Get-Location) '.pytest_cache/demo'
    python -m pytest -p no:cacheprovider -q tests/test_t13_browser_rehearsal.py
    if ($LASTEXITCODE -ne 0) { throw '浏览器验收未通过，请保留实际错误。' }
} finally {
    if ($null -eq $previousArtifactDir) {
        Remove-Item Env:APPLYPILOT_DEMO_ARTIFACT_DIR -ErrorAction SilentlyContinue
    } else {
        $env:APPLYPILOT_DEMO_ARTIFACT_DIR = $previousArtifactDir
    }
}
```

产物为 `.pytest_cache/demo/review.png`、`applications.png` 和 `observation-summary.json`。摘要只保留白名单字段；五次 reported、一次 missing 是设定的合成响应，不能讲成真实 token 消耗或费用。脚本恢复原进程环境，不删除用户文件。

## 八分钟演示讲解顺序

自动验收可展示命令、源码断言与保存的合成截图；人工交互则按下一节启动真实服务与模型。两种方式须先说明，不把自动测试的截图说成真实模型结果。

| 时间 | 展示内容 | 应说明的边界 |
| --- | --- | --- |
| 1 分钟 | 确认事实和来源，录入一个合成 JD | 确认是用户声明，不是经历真实性认证 |
| 1 分钟 | 逐项证据与缺口、JD 入队快照 | 候选及匹配分数不是录用概率 |
| 2 分钟 | 退回、编辑至修订 3、事实引用与前后差异 | 模型复核可能误判，最终仍须人工核对 |
| 1 分钟 | 点击批准、查看冻结版本与 DOCX 正文 | 批准前无冻结版，历史不由当前资料补造 |
| 1 分钟 | 记录 user_reported/unknown | 导出不代表投递，用户自述不代表网站确认 |
| 2 分钟 | 六次配对日志、wf_ ID 回归、恢复用例 | 替身 usage 不是实际计费，checkpoint 不保证外部请求恰好一次 |

## 人工演示真实模型

只有这一部分需要用户自己的模型服务密钥，会发送所选 JD 和个人事实并产生费用。使用合成资料，在本机环境设置 `DEEPSEEK_API_KEY`，再执行 `docker compose up -d --force-recreate app`；不要在终端录屏、截图、仓库或日志中展示密钥。T27 已完成一个限额适配器、关键词路径的真实模型 API 案例并目视核对审核页，见[执行记录](t27-execution.md)；默认适配器与全程浏览器输入仍未按同一方式验收，运行异常须保留错误并明确说明。

1. `/profile` 新增并逐项确认合成资料与事实，确认是用户声明，不是第三方真实性认证。
2. `/jobs` 粘贴 JD，显式解析，核对非要求、二选一及待核对项；查看要求与事实引用。
3. 启动草稿，在审核页检查引用并编辑，重新校验后人工批准。T29 起新任务保存入队 JD 快照；复用当时已有的解析，不再次请求解析模型。职位后来变化需创建新任务，不改变已有任务的输入。
4. 导出 DOCX，对照批准快照；更改当前事实不能改变历史批准内容。
5. 用户自行投递后在 `/applications` 登记版本和结果；仅导出不代表已投递，记录来源是用户自述。

把贡献从“参与”扩大为“独立完成”、加入无来源数字，展示校验拦截；语义复核不可用时不能自动批准。并发批准、冻结写入失败和中断恢复用隔离测试展示，不对个人工作数据注入故障。检索候选不等于证据，匹配分数不等于录用概率。

## 发布前剩余边界

- 当前只支持本机单用户单实例；公网部署先补认证、对象归属、限流和数据删除策略。
- worker 有界重试不保证模型调用恰好一次；checkpoint 长连接失效需按手册重启，不能盲目重复提交。
- 默认模型调用按 T31 输出脱敏 JSON 事件。README 与镜像启动已启用日志配置；自定义启动方式须自行加载配置。按 call_id 配对，缺失或无效 usage 不能当零费用；进程强杀或日志丢失可能留下未知调用，不将日志当作持久化账本或硬预算。
- 默认适配器的完整浏览器真实模型流程、多页真实 DOCX、真实招聘网站、完整日志跨模块关联仍未完成验收。
- T28 已停用旧冒烟入口并清理已确认的源码标识和公开可达历史；旧 GitHub 对象、Actions 日志及其他副本未证明清除。T29 已处理新任务重复解析及默认单次输入/输出边界；任务/账户总配额尚未实现。
- 升级先备份并执行迁移 006，旧任务不会被补造成有原始输入快照。默认模型截断会失败，不自动提高上限或继续使用部分输出，详见 [T29](t29-execution.md)。
- checkpoint 升级先在备份副本验证类型兼容；遇到未知类型不要开启 pickle 或放行全部模块。锁定库可能把受阻对象返回为原始数据，不保证整条记录拒绝，详见 [T30](t30-execution.md)。
- T24/T25 检索数值只适用于固定小型合成集；不写成真实用户效果或性能提升。
- 仓库尚无独立 LICENSE，维护者需确认许可；不替维护者选择。公开材料仅合成数据；旧 GitHub 对象缓存的清理状态未确认。

详细故障、备份和恢复见 [验证手册](validation-and-demo.md) 与 [T12](t12-execution.md)。
