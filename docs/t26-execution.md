# T26：默认分支集成与本机交付复验

日期：2026-10-19。仅更新交付文档与脱敏证据，不改变业务代码。

## 开始前的方案审查

继续增加检索功能不是当前首要任务：开发链已有完整合成主流程，默认分支却仍停在 T6。先将已有实现整理成可审查、可运行的交付候选，避免把分支阶段成果误认为默认版本已发布。无需引入新编排平台、插件、微服务或浏览器自动投递。

## 版本与验证

- 代码基准：`4ae17c1772345479c98a60facb7983a42ea57ed6`。
- 新鲜获取的 main：`56ace869c09e5ea5118896705cb6086b8b791b2e`。
- `git rev-list --left-right --count origin/main...HEAD`（文档提交前）：`0 42`；main 是祖先，无需逐分支重放。
- `python -m pytest -p no:cacheprovider -q --tb=short`：**318 passed, 3 warnings in 153.68s**。警告来自依赖弃用；常规模型调用是替身。浏览器与临时数据库测试包含在内。
- 隔离项目 `applypilot-t26-check` 按实际 Dockerfile 构建；Docker Engine 29.7.2、Compose v5.5.0。应用/数据库绑定本机 18026/15426，不使用个人资料或模型密钥。
- live、ready 均为 200；保存合成职位后重启应用，ready 回到 200，记录和原文保留；解析无密钥返回 503 和“模型尚未配置，职位已保留”。
- 对该新建隔离项目执行 `down --volumes` 后核对容器和卷均为 0；不清理其他项目。

脱敏结果见 [JSON](evaluation/t26-delivery.json)。执行器主动重定向 stdout/stderr：完整回归摘要在 `.pytest_cache/t26-full.txt`，Compose 构建、重启和清理分别在 `.pytest_cache/t26-delivery/up.log`、`restart.log`、`cleanup.log`。这些是本机临时文件，不是 pytest cacheprovider 生成的缓存；`-p no:cacheprovider` 不妨碍主动写入该目录。它们不随仓库分发，公开 JSON 是脱敏摘录。路径检查未发现被跟踪的 `.env`、简历 DOCX/PDF 或私有简历上下文；这不是完整秘密扫描或 GitHub 旧对象缓存清理证明。

隔离验证使用临时空 `empty.env` 与以下 `override.yml`，均位于 `.pytest_cache/t26-delivery`。`!override` 需要支持该标签的 Compose（本轮 v5.5.0），用于替换而非追加默认端口：

```yaml
services:
  db:
    ports: !override
      - "127.0.0.1:15426:5432"
  app:
    image: applypilot-t26-check:local
    ports: !override
      - "127.0.0.1:18026:8000"
    environment:
      DEEPSEEK_API_KEY: ""
```

```powershell
docker compose --env-file .pytest_cache/t26-delivery/empty.env -p applypilot-t26-check -f docker-compose.yml -f .pytest_cache/t26-delivery/override.yml up --build -d --wait --wait-timeout 120
```

复现前确认项目名、端口及卷没有被现有工作占用；不要直接重用其他人的项目。清理只对本轮新建项目执行同组选项下的 `down --volumes`，其他正常工作数据禁止删除。

## 文档与集成

刷新 README、验证手册与简历证据，增加[本机交付检查单](delivery-checklist.md)。早期测试计数作为历史保留，不伪装为新验收。

集成目标为 `main`，使用草稿 PR 展示 T7–T25 以及本次交付材料；未授权自动合并。真实模型全流程、网站兼容性、公开部署认证及许可确认仍属于限制，不能以 318 项通过替代。

## 外部只读复审与主审复核

DSH 本次命令不可用，未产生报告，不算成功。改用已安装 CodeBuddy，限制 Read/Grep/Glob、禁用 MCP、禁止执行与修改；同时核对 stdout、stderr 和结构化结果，结果为 success、is_error=false，stderr 为空。报告核对文档、启动配置与指定测试，未运行测试，也不代替主审验收。

报告称“禁用 cacheprovider 后日志无法存在”为确认缺陷；主审检查上述实际日志后驳回该推断，并采纳补写主动重定向路径的建议。隔离端口缺少覆盖说明属复现信息不足，已补模板和实际命令。跳过浏览器测试及历史日期建议已在交付检查单与手册说明。报告不能静态核实 318 项、Git 计数和镜像状态，这些由主审实测负责；JSON 仅收录已有记录字段，不补造没有独立保存的初始 ready 字段。
