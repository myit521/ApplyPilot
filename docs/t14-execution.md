# T14 合成样例真实模型探针

日期：2026-10-10。请求代码基准提交 `4054055`，逐例结构化结果见[报告](evaluation/t14-live-probe.json)。本阶段只检查已有 DeepSeek 提示词和解析逻辑在两条合成样例上的实际返回，不测简历生成、真实用户数据或招聘网站。

`scripts/evaluate_t14_live_probe.py` 从 T11 固定样例选择 `jd01` 和 `cl08`，分别调用一次 Chat Completions。请求名为 `deepseek-chat`、温度 0.2、JSON 模式，JD 最大输出 1000 token，语义复核最大输出 300 token；超时 60 秒，不重试。HTTP 错误只保留状态，不保存错误正文；报告不保存密钥、请求头或原始模型响应。脚本从进程环境变量读取密钥，报告记录样例 SHA-256、源提交、返回模型标识、停止原因及 token 用量。独立复核后，脚本增加了本地响应长度上限，并把非正常停止原因记为不完整；本次报告仍保留运行时原始提交号 `4054055`，没有为这些防护再次调用模型。重复执行会产生新的请求和用量。

```powershell
$env:PYTHONPATH = 'src'
# 仅在本机进程中预先设置 DEEPSEEK_API_KEY；不要将密钥写入命令历史或仓库。
python scripts/evaluate_t14_live_probe.py --output docs/evaluation/t14-live-probe.json
python -m pytest -p no:cacheprovider -q tests/test_t14_live_probe.py
```

本次两例均返回 HTTP 200，`finish_reason=stop`。`jd01` 抽取了岗位、上海、Java 与 Spring Boot 必备条件及业务接口职责；它把人工金标的一条“Java 和 Spring Boot”拆成两条，语义相符但不是逐字一致。`cl08` 对“参与接口评审，没有负责架构设计”被改写成“独立设计商城系统架构”返回 `SEMANTIC_OVERRUN`。请求名为 `deepseek-chat`，响应 `model` 均为 `deepseek-flash`；因此只能按实际响应标识说明本次运行，不能把模型名当作固定版本。两次实际合计输入 451 token、输出 181 token、总计 632 token；未据此推算费用。

探针脚本首次运行前的两条离线约束测试 **2 项通过**；同次无 Docker 回归 **126 项通过、107 项未选、1 条 Testcontainers 弃用警告**。后续防护回归 **3 项通过**，完整无 Docker 回归 **127 项通过、107 项未选、1 条同类警告**。CodeBuddy CLI 审查尝试 90 秒内没有返回结果，不能视为通过；另一个只读审查 Agent 确认了调用次数与错误隔离，并指出上述本地边界缺口，随后已补齐。

两个挑选的合成例子不能估算 JD 解析准确率、语义越界召回、生成内容支持率或真实场景性能。仍需在固定且人工复核的更大样本上报告所有成功与失败；真实用户数据必须另行处理授权、脱敏和保留期限。
