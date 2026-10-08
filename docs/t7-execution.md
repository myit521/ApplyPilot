# T7 原子批准与冻结快照执行记录

验收日期：2026-10-08。T7 在既有 PostgreSQL、LangGraph checkpoint 和审批 API 上补齐批准事务、不可变内容快照、幂等重试及待对账页面；未引入微服务或新的运行时依赖。

## 实现范围

- `003_atomic_approval_snapshot.sql` 增加 `workflow_approvals` 与 `resume_version_facts`，关联批准修订、版本、事实修订和内容哈希。
- 批准包由规范顺序的简历分区、职位快照、引用事实快照和草稿修订组成，以规范化 JSON 计算 SHA-256。只快照简历实际引用的事实。
- 一个 PostgreSQL 事务锁定职位与引用事实，并写入简历版本、主张、事实快照、批准记录和 `resume.approved` 审计事件。被引用事实缺失、未确认、停用或修订变化时事务拒绝批准；注入审计写失败时整笔回滚。
- edit 与 approve 通过 run 级 advisory lock 串行化。数据库批准记录是批准结果的权威来源；相同修订的批准重试返回同一版本和哈希。
- checkpoint 恢复失败时保留已批准版本，API 返回 `202` 与 `APPROVAL_RECONCILIATION_PENDING`。工作流摘要先读取 PostgreSQL，不依赖 checkpoint 可读；审核页展示冻结内容、DOCX 下载和“重试状态同步”，隐藏编辑、批准和退回控件。首页可重新进入待对账页面。
- DOCX 标题与批准审核页引用事实均来自快照。早期简历版本没有职位快照时，DOCX 继续使用其关联职位标题。

## 验证

2026-10-08 在 Windows Python 3.13.5 执行：

```powershell
python -m pytest -p no:cacheprovider -q
```

结果：**156 passed**。集成测试使用临时 PostgreSQL、空向量替身和模型替身，不调用真实生成模型；pytest 汇总只有 `testcontainers.postgres` 导入路径弃用警告。

测试覆盖迁移就绪检查、批准包规范化与哈希、批准原子写入、引用事实修订冲突、事务回滚、同修订重试、并发批准、编辑/审批竞争、checkpoint 失败后恢复、批准摘要在 checkpoint 不可读时可用、职位或事实变更后审核与 DOCX 保持冻结快照，以及旧版本兼容导出。

另在临时 PostgreSQL 与合成职位/事实上，通过本机浏览器人工检查审核页：pending 页面显示冻结版下载和重试按钮，不显示编辑/批准/退回控件；点击重试后，checkpoint 仍不可用时页面保留 pending 状态和下载链接；首页显示“重试对账”入口。该浏览器验收没有使用个人资料或真实职位数据。

## 边界与后续

恢复是用户触发的单 run 重试，不会在进程启动时扫描 pending 记录；未验证多实例自动恢复。checkpoint 可用性恢复后，重复批准会继续对账并返回同一冻结版本。此项不构成任务队列或 exactly-once 承诺，T8 仍需实现持久化任务、启动恢复、限次重试和可观测性。

真实模型输出质量、DOCX 排版与分页、第三方招聘网站兼容性，以及认证和多用户资源隔离不在本次验收范围内。当前应用仍是本机单用户原型。
