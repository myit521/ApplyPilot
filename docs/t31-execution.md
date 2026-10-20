# T31：模型调用日志观测

日期：2026-10-20。基准 `3be78f1` 的 T30 CI 已通过（run 39355803012）。继续草稿 PR #1，不合并。

## 开始前的方案审查

已有单次输入/输出上限和 worker 有界重试，但无法从模型调用日志区分节点、执行次数、供应商 token 用量和未知用量。直接增加总费用配额会依赖尚未确定的计费、重试及崩溃窗口契约，因此本轮只补可独立验收的请求观测，不加数据库账本、计价、调度平台或依赖。

## 实现与统计口径

- 默认 DeepSeek 适配器在调用 HTTP 前发出 `model_call_started`，正常返回或抛错时发出 `model_call_finished`，通过随机 `call_id` 关联。记录 provider 固定标识、耗时、结果和可用的 run/job/attempt/step；不记录提示词、生成文本、密钥、原始异常、响应体、模型名称或服务 URL。
- 这是适配器尝试调用 HTTP 的边界，不证明请求已经发给供应商、更不证明已收费。输入预检查拒绝只发 finished，`request_started=false`、`outcome=input_rejected`，不计为 HTTP 尝试。
- 每次调用单独记录。`outcome` 为 success、retryable_error、error 或 input_rejected。success 仅表示传输/信封通过，后续 JD、事实或语义校验仍可能失败，不能将其理解为业务批准。
- 只在 HTTP 200 的 JSON 对象中检查供应商 usage；prompt_tokens/completion_tokens/total_tokens 必须全部为非负整数（不接受 bool），且前两项之和等于总数，才记为 `reported`。额外供应商字段忽略，不做缓存折扣、推理细分或人民币计价。
- 无 usage 为 `missing`；结构/数值不满足要求为 `invalid`；超时、非 200、非 JSON 等无法读取 usage 为 `unavailable`。后三类 token 字段均为 null，不能按零相加。供应商明确返回合法的三个零才记 reported/0。
- 截断或其他不合格文本仍按 T29 拒绝；若此前可读取合法 usage，失败调用也保留该用量。响应失败不等于无成本。
- worker 作用域提供 run_id/job_id/attempt_count，节点作用域补 parse_jd/generate_resume/semantic_review。ContextVar 嵌套恢复，实际图执行中的上下文传播由测试验证；共享适配器不保存可变任务标签。任务重试沿用 run_id，获得新 call_id 与当前 attempt。
- 人工退回和编辑的后续模型请求沿图配置保留 run/job，但不归入已经结束的 worker attempt，attempt_count=null。图外的事实导入、页面解析等默认适配器调用也会记录，未绑定的任务/节点字段为 null；不能把它们强行归入某个任务。
- ModelAdapter 的 complete→str 协议不变。注入的替身或其他实现不会自动获得供应商用量报告；不要把其无记录当作零调用。

## 查看日志

直接启动沿用 README 的命令，增加 `--log-config src/applypilot/logging.json`；Dockerfile 同样启用镜像内配置。配置仅对 applypilot 与 uvicorn 开 INFO，其他 logger 的 root 为 WARNING；结构化模型事件以一行 JSON 输出到 stderr，服务启动和访问日志仍是普通文本。

Compose 重新构建后用 `docker compose logs --no-log-prefix app` 查看；不需要新增日志平台。要保留本机日志，可将启动命令的 stderr 重定向到忽略目录。日志仍有 UUID 和职位记录编号，不应直接上传整份调试日志。

按 run_id 筛选，再按 call_id 配对；started 无 finished 可能来自进程中断、日志丢失或未完成请求，必须保持未知。按完成事件统计时区分请求次数、reported 用量和未知调用数，并先对重复采集的 call_id 去重。attempt_count 是 worker 的执行次数，不是节点修复次数或供应商内部重试次数。

## 限制

日志是尽力输出：日志级别、处理器故障、丢失、截断、重复采集和进程强杀都可能造成不完整记录。日志输出失败不会让已付费请求被当作业务失败而重试。没有数据库持久化、缺口对账、任务总预算、账户配额、恰好一次保证或人民币成本承诺；最终计费以供应商为准。调用未结束的窗口也不能由本轮消除。

现有批准和导出 version ID 的跨模块关联仍未补齐。不做真实模型调用，不改变人工批准与手工投递边界，不增加对招聘网站的操作。

## 委派与验收

CodeBuddy 被分派一次限定文件的只读方案核对，超过约定窗口仍无 stdout/stderr/报告；核实进程后停止并保留退出记录，不计为审查通过，不重复派发。独立子 Agent 在无聊天历史、限定文件下复核源码与测试，未发现待修复缺陷，未运行测试；追加限定检查日志配置、镜像复制路径、启动命令及子进程测试，也未发现缺陷。主审复核结论并负责实际运行。没有为 CLI 超时关闭沙箱或扩大权限。

17 个新增请求观测用例在旧实现上全部失败，接入后连同旧适配器测试为 60 passed。图/worker/日志测试合计 15 passed：包含实际 LangGraph 节点线程、一次超时后的 checkpoint 继续、manual 退回生成、任务关联与未知用量。独立子进程加载发布日志配置，未设置 caplog 也能输出事件，1 passed。

独立标签 `applypilot:t31-observability` 构建成功；禁网容器内用 HTTP 替身验证配置与事件输出，合法合成 usage 2+1=3 正确显示 reported。未启动或替换用户现有 Compose 容器、未访问真实供应商。

最终运行 `python -m pytest -p no:cacheprovider -q --tb=short`：**396 passed、3 warnings in 152.63s**，包括临时 PostgreSQL、备份恢复、任务中断恢复及本机合成浏览器流程。警告为既有 Testcontainers/websockets 弃用项，不在本轮无关升级。模型响应为替身，不是供应商真实计费或实际生成质量验证。

## 下一项

先用当前观测检查完整演示流程的可解释性，再决定是否需要持久化调用账本。若实施总预算，必须先定义预留、结算、失败未知、并发、重启及人工继续的契约，不能简单累加当前日志或用进程内计数器拦截。
