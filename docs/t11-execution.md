# T11 标注评测与回归执行记录

日期：2026-10-09。基准提交 `2a4a35b`。本阶段先建立无需模型密钥的合成标注基线，并复现正常、失败、恢复流程；真实模型评测单列，不把替身测试记为模型质量。

## 数据与口径

- `tests/fixtures/t11_jds.json`：10 份合成 JD、22 条逐项要求、8 条合成事实；职位原文、结构化解析输入、人工要求类别、关键词状态和支持事实 ID 均显式保存。
- `tests/fixtures/t11_claims.json`：20 条主张，分为支持 6、越界 5、无支持 6、待人工判断 3；每条保留事实引用、人工标签、理由和确定性规则期望错误码。越界中有 3 条属于贡献或生产上线等语义问题，不能用数字/技能规则覆盖。
- `scripts/evaluate_t11.py` 只运行当前 `build_match_report` 与 `validate_claims`，不调用 JD 解析模型、语义复核模型、向量召回或招聘网站。报告保存输入文件 SHA-256、源提交、模式、逐例结果和分子/分母；零分母为 `null`。人工事实召回包括没有技术关键词但人工可见的事实，因此会显示关键词基线的缺口；证据精确率单列防止额外引用被掩盖。

## 运行结果

在 Windows Python 3.13.5 上执行：

```powershell
$env:PYTHONPATH = 'src'
python scripts/evaluate_t11.py --output docs/evaluation/t11-deterministic.json
python -m pytest -p no:cacheprovider -q tests/test_t11_evaluation.py
python -m pytest -p no:cacheprovider -q tests/test_api.py::test_full_api_flow tests/test_api.py::test_stale_approval_writes_no_version tests/test_t7_approval_integration.py::test_persist_approval_rolls_back_every_table_when_event_insert_fails tests/test_t8_worker_integration.py::test_transient_failure_resumes_from_checkpoint_after_restart tests/test_t8_worker_integration.py::test_killed_process_reclaims_claimed_task tests/test_t9_application_api.py::test_manual_record_create_update_and_persist
python -m pytest -p no:cacheprovider -q
```

当前合成基线：关键词状态 **22/22**；人工支持事实召回 **12/15**，所列证据精确率 **12/12**；规则可判违规被任意规则拦截 **8/8**，期望错误码命中 **8/8**。6 条仍需人工/语义判断（3 条明确语义越界、3 条不确定）没有计入规则召回。上述数值只针对这批合成样本及给定解析结果，不能解释为真实 JD 解析、真实模型、检索 Recall@5、投递成功率或生产性能。[逐例报告](evaluation/t11-deterministic.json)由 `ae12fd9` 的干净工作区生成，记录了两个 fixture 的 SHA-256 和 `working_tree_dirty=false`。

选定的正常 API 流程、过期批准、事务回滚、checkpoint 重启恢复、杀进程后任务收回和人工投递记录 **6 项通过**，使用临时 PostgreSQL 与模型替身。全量 **225 项通过**，有 1 条 Testcontainers 导入路径弃用警告；最后一处 JD 金标措辞修正后，T11 聚焦 **8 项通过**，离线指标未变化。

## 未运行与下一步

当前进程未配置 `DEEPSEEK_API_KEY`，未运行真实模型，也没有产生模型费用；因此不报告 JD 解析准确率、语义复核召回、真实生成支持率或 token/费用。演示用例是已有可执行集成测试与上述命令，真实浏览器加真实服务全链路彩排仍待独立运行。T12 将处理 CI、干净环境启动、日志脱敏和备份恢复；这些尚不能因本阶段测试通过而视为完成。
