# T1 执行记录

日期：2026-10-01。范围仅 roadmap.md 的 T1，未授权推进 T2–T12。
工作在独立 worktree；复制上一轮公共文档作为上下文，原目录修改保持不动。

## 决策

- 已有产品设计和 T1 验收获用户同意，直接执行，不引入 OpenSpec 工件链。
- 导入和工厂不联网；lifespan 初始化/释放应用拥有的 checkpoint 连接。
- 数据库初始化失败时服务保留存活与就绪检查，业务请求返回 503；修复依赖后重启。自动恢复属于 T8。
- 就绪检查验证当前 DB 与必要业务表；不通过真实模型请求做探针，也不等于模型质量验证。
- 保持注入 checkpoint 的测试能力，外部注入对象由调用方管理。
- 单元/集成测试用 marker 区分；单元命令默认不访问网络。
- 仅锁定并验证本任务依赖环境，不混入用户全局 Python 的其他应用依赖。

## 进度

- 已检查基线 f586664 和当前未提交文档；Docker Desktop 当前未运行。
- RED：新进程禁止网络的导入测试失败，定位 api.py 顶层 create_app 连接数据库。
- GREEN：生命周期修复后初始 6 项运行测试通过。
- RED：旧 checkpoint 连接断开、新 DB 探针可用时就绪误报 200。
- GREEN：补 checkpoint 查询后 7 项运行测试通过。
- uv pip compile 固定直接依赖，生成 Windows/Python 3.13 requirements.lock；uv venv + uv pip sync 全新安装成功。
- uv pip check：89 packages，全部兼容。uv 创建的 venv 默认不含 pip，所以检查使用 uv 而不是 python -m pip。
- 全套测试：57 passed，71.99 秒；真实临时 PostgreSQL、已有嵌入模型缓存、生成模型替身，无真实模型费用。
- 当前依赖有 Starlette/AnyIO 与 Testcontainers 弃用警告，不影响本次通过，暂不扩展为框架迁移。
- 无 Docker 入口：故意将 DOCKER_HOST 指向不可达地址，45 passed、12 deselected；无需启动容器或模型网络。
- 真实 Uvicorn 进程 + 不可达 DB：live 200、ready 503、业务 503、/docs 200；进程已退出。
- 独立只读审查未发现阻断问题；审查者未重跑全套，也未压力测试 checkpoint 写入与探针并发。
- Markdown 链接、代码围栏、git diff --check 均通过；测试临时容器已退出。
- 结论：T1 完成，T2–T12 未执行。仅将 T1 文件同步回 D:/ApplyPilot，保留原文档和私有上下文；未提交或推送。
