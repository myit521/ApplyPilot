# T3 职位录入执行记录

开始：2026-10-02；验收：2026-10-03。基线：`b311ae1`；实现分支 `codex/job-entry`。范围为路线 T3，不含 T4 证据匹配。

## 实现内容

- `POST /api/jobs` 只保存职位，不构造模型适配器、不解析 JD。返回完整记录；职位、公司、来源、JD 长度和空白、NUL、URL 协议/主机/凭据/空白/Unicode 控制字符均校验；额外字段拒绝。JD 原文保持首尾空格和换行。
- `GET /api/jobs` 用一致性只读事务返回倒序分页与总数；`GET /api/jobs/{id}` 读取详情。旧记录元数据为空时仍能读取。
- `POST /api/jobs/{id}/parse` 是唯一显式解析入口。成功只写 parsed，不覆盖用户岗位名；密钥缺失、模型/结构错误时给出适当状态码，不包含提供商密钥错误文本，保存记录与上次成功解析不变。
- 新增 `/jobs` 中文页面，支持录入、历史翻页、详情及用户点击后显式解析；提交失败保留表单。动态数据通过 `textContent` 或 Jinja 转义显示；来源 URL 仅保存，不访问网站。首页提供页面入口。
- 冒烟脚本和已有 API/工作流集成样例更新为先保存再显式解析。
- 不改 schema、依赖或用户数据库；没有自动抓取、职位编辑/删除、任务启动或投递动作。

## 测试与复审

测试先行：T3 专项测试初次 RED 为 28 failed、6 passed；补齐实现后专项 34 passed。审查输入边界时发现 URL C1 控制字符覆盖不完整，增加 Unicode 控制类别回归后再验证。

最终命令（Windows Python 3.13.5、锁定虚拟环境、Docker 临时 PostgreSQL、离线嵌入缓存、模型替身）：

```powershell
.venv/Scripts/python.exe -m pytest -p no:cacheprovider -q
```

结果：**108 passed，70.78 秒**，仅两项现存 Starlette/AnyIO、Testcontainers 弃用警告。最终 URL 控制字符修改后的无 Docker 命令 `-m "not integration"` 结果为 **75 passed、33 deselected，5.18 秒**。

另以 Microsoft Edge 无头浏览器和临时 PostgreSQL 完成 `/jobs` 验收：保存时不调用模型、原文精确保留、显式解析不改手工岗位名、模型失败不泄露模拟密钥且保留旧解析、空白输入失败时保留表单、历史上一页/下一页及详情、注入的 script/img 不执行；页面无 JavaScript 错误。浏览器验收使用合成模型，不验证真实模型输出质量。

独立审查（对照基线 `b311ae1`）未发现实现正确性问题。文档状态遗漏已补齐；路线将 T4 标为下一项。当前此功能仍为本机单用户页面；不提供 CSRF/认证保护用于公开部署。无 DB 迁移。

## 兼容说明

调用方现在须向 `POST /api/jobs` 提交 `title`、`company` 和 `raw_text`；解析从保存步骤拆为 `POST /api/jobs/{id}/parse`。相关仓库调用方与冒烟脚本已更新。URL 是用户提供的来源元数据，不会抓取。
