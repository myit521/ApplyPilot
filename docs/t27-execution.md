# T27：CI 冻结证据复现与限额真实模型流程

日期：2026-10-19；起点 `69e9681`，继续 PR #1，不自动合并。

## 先审查方案

优先验证交付链，不增加功能。T26 的本机 318 项回归不能代替托管 CI；真实模型只使用新编合成资料，先核对生成内容再批准测试版本，不接触招聘网站。

## CI 失败与修复

[PR CI 第 21 次运行](https://github.com/myit521/ApplyPilot/actions/runs/39226440048)安装依赖成功，但离线测试为 **10 failed、190 passed、118 deselected**。错误集中在 T16/T17 冻结文件字节哈希。不是模型调用失败，也不是需要更新金标。

根因有两个方向：Git 默认换行转换将 `tests/fixtures/t11_jds.json` 的 LF 变成 CRLF；T15 报告虽然 Git blob 为 LF，本机原始报告为 CRLF，T16/T17 保存的是原始 CRLF 报告哈希。因此把所有 JSON 强制为 LF 仍有 7 项失败。

`.gitattributes` 明确现有契约：`tests/fixtures/*.json` 使用 LF，`docs/evaluation/*.json` 使用 CRLF。未修改 JSON 内容、标注哈希或校验逻辑。今后生成新的报告和字节标注时须沿用该契约；若迁移格式，需新证据版本，不能静默改写旧金标。

复现使用只读 Git 索引导出至全新的忽略目录，不更改全局 Git 配置：

```powershell
git -c core.autocrlf=true checkout-index --all --prefix=D:/ApplyPilot/.pytest_cache/t27-ci-before/
# 在导出的目录运行
python -m pytest -p no:cacheprovider -q tests/test_t16_keywords.py tests/test_t17_jd_atoms.py --tb=short
```

修复前：10 failed、1 passed。增加属性后导出另一个全新目录，运行与 CI 相同的离线范围：

```powershell
python -m pytest -p no:cacheprovider -q -m "not integration" --tb=short
```

本机结果：**200 passed、118 deselected、1 warning in 7.55s**。随后提交 `82ce2fd` 的[托管 CI 第 23 次运行](https://github.com/myit521/ApplyPilot/actions/runs/39227415219)成功，实际为 **200 passed、118 deselected、2 warnings in 4.08s**。CI 只运行离线范围，不把 118 项排除项算作通过。

## 限额真实模型流程

本轮临时验证工具只使用新编合成身份、Java 接口开发事实和一份合成 JD，临时 PostgreSQL 与本机 HTTP 服务隔离。模型请求复用 T14 单次调用函数，最多 6 次，每次最多 1200 输出 token；不在网络层自动重试。请求与 token 上限不等于人民币费用上限。

使用空向量替身，验证关键词路径、真实 JD 解析/生成/语义复核及人工审批 API、导出、用户自述投递记录的接缝，不测本地向量或真实网站。父 Agent 必须核对生成文本后才能为该合成工作流写入审批信号。该工具不是产品配置开关，也不证明默认生产适配器已具备相同预算机制。

实际完成一条合成案例：确认资料和事实 → 保存 JD → 真实解析 → 关键词匹配 → 持久任务内解析、生成、语义复核 → 等待审批 → 主审核对文本和引用 → 批准冻结 → DOCX → `unknown`、`user_reported` 的测试记录。ready、审核页和记录页均为 200，批准结果 `READY_TO_APPLY`，checkpoint 对账完成。

请求模型标识为 `deepseek-chat`，4 次响应标识均为 `deepseek-flash`；总计 1187 输入、298 输出、1485 token，不换算为未经核实的费用。校验重试为 0。匹配报告正确显示 Java 支持，但两条不含具名技术词的职责仍为 unknown，不能表述成完整语义匹配。

主审核对的两条生成文本为“Java”和“参与 Java 接口开发，并为接口编写单元测试。”，均引用同一条已确认合成事实，没有新增指标、技能或贡献扩大。先用本机 Edge 只读打开实际审核页，核对输入框与模型结果一致，目视检查[截图](evaluation/t27-review.png)，之后才写入绑定 run_id/修订的审批信号。此处审批由测试操作者代行，只针对虚构资料，不是产品自动批准能力。

导出 DOCX 为 36843 字节，解析出的姓名、邮箱、岗位和两条正文符合合成资料；没有把 DOCX 作为公开简历上传，也未做 Word 多页视觉验收。API 工作流不是全程浏览器输入自动化；本轮只增加了真实生成内容的审核页渲染检查。

脱敏[结果 JSON](evaluation/t27-live-workflow.json)保留合成输入、解析、主张、引用、请求用量、批准与记录。为便于复核，保存实际执行的[临时工具文本快照](evaluation/t27-live-harness.txt)，其 SHA-256 与结果中的 `harness_sha256` 一致；它不是受支持的产品 CLI。复现时复制到全新的 `.pytest_cache/<本轮目录>/live_workflow.py`，使用 README 的依赖及本机 Python 运行，在报告出现 `awaiting_operator_review` 后人工核对，再在同目录写 `approve-synthetic.json`，内容为该报告中的 run_id 和 expected_revision；不得跳过核对或复用旧信号。它会调用真实服务、等待至多 10 分钟并在上下文结束时清理临时库。环境密钥优先，否则仅提取本地 `.env` 的对应值，不打印或复制密钥。

Windows-MCP 启动调用曾超时，但后台 PID 和报告正常推进；没有重复派发。收尾独立确认该 PID 已退出、容器 ID 已不在 `docker ps -a` 中。临时日志有 LangGraph 未显式登记的 msgpack 类型警告；当前版本可运行，升级前需单独验证严格序列化允许列表，不把它忽略成长期兼容保证。

## 外部复审与后续范围

CodeBuddy 使用 Read/Grep/Glob、无 MCP 的只读任务，核对合成输入、请求/输出上限、审批信号和清理，结构化 success、stderr 为空，未执行测试。主审保留 6 次请求上限，未采纳“为了覆盖所有重试将其提高到 8 次”的建议；预算耗尽应失败并记录，不能为了演示成功追加费用。实际未触及上限。

确认同一 JD 在保存后显式解析一次、启动工作流后又解析一次，会增加费用且两份结构可能不同；作为后续需设计的输入快照一致性问题，不在证据交付中偷偷改主流程。默认 DeepSeekAdapter 仍没有本工具的输出预算机制。

旧 `scripts/smoke_e2e.py` 的样例含具体学校/单位，且审批调用没有当前修订号，本轮不运行它、不发送该样例；需要另行修订并核对隐私历史，不能据 T26 的文件路径扫描认定所有源码样例均脱敏。该项、重复解析和生产预算门禁在合并前应继续处理；本 PR 保持草稿。
