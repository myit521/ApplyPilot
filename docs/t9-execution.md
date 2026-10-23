# T9 人工投递记录执行记录

验收日期：2026-10-09。基准提交 `184be9b`。本阶段把“批准简历后用户自行投递并留痕”接成可用的本机流程，不操作招聘网站。

## 实现与边界

- `005_manual_applications.sql` 在旧 `applications` 表上增加来源、操作时间、更新时间和修订号，保留旧行及原有唯一约束。旧行来源为 `legacy_unverified`，新建记录由服务端固定为 `user_reported`；没有网站确认来源。
- `POST /api/applications` 只接受 `workflow_approvals` 中的批准版本，且版本必须属于指定职位。状态仅允许 `unknown`、`submitted`、`failed`；其中 `submitted` 仅表示用户自述。请求需带 `Idempotency-Key`：相同创建载荷重试返回同一记录，异参或同职位/版本/渠道重复返回 409。
- `GET /api/applications` 和 `GET /api/applications/{id}` 查询历史；`PATCH /api/applications/{id}` 只修改用户自述记录的状态、操作时间和备注，要求 `expected_revision`，过期返回 409。创建与更新各在同一事务写一条审计事件。为保留留痕，本阶段没有删除接口。
- `/applications` 页面从已批准版本中选择，供用户在外部自行操作后登记结果；审核页提供入口。未知结果可以留空时间。页面明确提示下载/导出不等于提交，记录也不证明网站确认。
- 新库与已有数据卷都须执行 `python -m applypilot.db`；`/health/ready` 要求 005 迁移。

## 验证

在 Windows Python 3.13.5 上，以真实临时 PostgreSQL 和项目已有合成适配器运行：

```powershell
$env:PYTHONPATH = 'src'
python -m pytest -p no:cacheprovider -q tests/test_t9_application_repo.py tests/test_t9_application_api.py
python -m pytest -p no:cacheprovider -q
```

最终聚焦结果：**16 passed，1 条 Testcontainers 导入路径弃用警告**；全量结果：**210 passed，1 条同类警告，103.17 秒**。全量测试后对时区与空值校验及相应断言又运行同一聚焦命令，仍为 16 passed。覆盖迁移幂等及旧行、未批准/错职位拒绝、同键重试、异参和并发重复冲突、部分 PATCH 保留原字段、结果修订冲突、审计事件与重连查询。CodeBuddy 实现了数据库与存储层切片，但其会话没有命令执行工具；上述测试由协调方在本机运行并复核。DSH 仅用文件读取与搜索做只读复审，没有运行测试；其确认的部分 PATCH 覆盖数据及 422 页面提示不可读问题均已修复。

## 未覆盖与后续

这只是单用户本机记录账本，没有浏览器填写、自动提交、网站回执校验或多用户权限。`user_reported` 不能解释为招聘网站已收到简历。原有 `(job_id, version_id, channel)` 唯一约束意味着同渠道重试只能修改同条记录，旧版同渠道记录也会占位；本阶段保留该约束和旧行，不自动合并不同来源。页面主流程与 DOCX 视觉验收属于 T10；评测和交付基线属于 T11/T12。真实投递与模型调用均未在本阶段测试中发生。
