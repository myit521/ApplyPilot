# T12 本机交付与恢复记录

日期：2026-10-09。基准提交 `ccc43f2`。范围是单机、单应用实例的演示环境；不构成公开部署或真实投递授权。

## 启动与复现

`Dockerfile` 使用 Python 3.13，安装 `requirements.runtime.txt` 中的固定版本，复制 `src/` 和 `db/`，以非 root 用户运行。Compose 只把应用 8000 与数据库 5432 绑定至 `127.0.0.1`；数据库沿用命名卷。应用启动先执行 `python -m applypilot.db`，迁移失败则不启动 Uvicorn。`/health/ready` 检查 checkpoint 和业务 schema；它不检查模型密钥或真实模型服务。

在 PowerShell 中运行：

```powershell
docker compose config --quiet
docker compose build app
docker compose up -d --wait
Invoke-WebRequest http://127.0.0.1:8000/health/live
Invoke-WebRequest http://127.0.0.1:8000/health/ready
docker compose logs --tail=100 app
```

无 `DEEPSEEK_API_KEY` 时可检查页面、存储和健康端点，显式模型功能不可用。启动命令不删除已有卷；升级前先备份。停止时使用 `docker compose down`，不要附加 `-v`。默认数据库密码仅供本机演示，不可直接开放公网。

本次以独立项目名 `applypilot-t12smoke` 和新卷启动：镜像构建成功，Compose 两服务健康，`/health/live`、`/health/ready` 均返回 200；插入合成职位后重启应用，该行仍在，重新就绪为 200。测试容器已用 `down` 停止，测试卷和原有 `applypilot_db-data` 都保留。第一次 Docker Hub 认证超时，稍后单独 `docker pull python:3.13-slim` 成功；没有改系统代理或权限。Docker 构建上下文约 410 kB，`.dockerignore` 排除 Git、文档、测试、环境文件和简历产物。Linux/x86_64 的固定运行依赖已在镜像内成功安装；其他架构未验证。

## 备份、恢复和故障处理

备份用 PostgreSQL 自带的 custom format `pg_dump`，先在数据库容器生成文件，再用 `docker cp` 复制到受控的本机位置。**恢复只对独立空实例操作**；先确认目标容器与源容器 ID 不同。不要把备份提交到 Git，也不要对现有数据卷运行 `pg_restore --clean`。

```powershell
$backupDir = 'D:\ApplyPilot-private-backups'  # 仓库外，仅本机受控目录
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$backup = Join-Path $backupDir 'applypilot-backup.dump'
$source = docker compose ps -q db
docker exec $source pg_dump -U applypilot -d applypilot --format=custom --file=/tmp/applypilot.dump
docker cp "${source}:/tmp/applypilot.dump" $backup
# 独立目标须使用新的容器与卷，并具有 pgvector 扩展；取得其容器 ID 后：
docker cp $backup "${target}:/tmp/applypilot.dump"
docker exec $target pg_restore -U applypilot -d applypilot --no-owner --no-acl /tmp/applypilot.dump
```

导出的文件含个人资料，使用后按本机数据保留要求清理。可执行演练是 `python -m pytest -q tests/test_t12_backup_restore.py`：它启动两个临时 PostgreSQL/pgvector 实例，在源库创建**合成**事实、批准版本、事实快照和人工投递记录，用真实 `pg_dump`/`pg_restore` 恢复到另一个实例，再核对迁移版本、批准包哈希、引用快照和投递记录，并重复运行迁移。演练 **1 项通过**；Testcontainers 的弃用警告不影响结果。此演练证明样例数据可恢复，不证明用户自己的备份已被验证。

数据库不可用时，`/health/ready` 返回非就绪或应用无法启动；先检查 `docker compose ps`、数据库健康与卷空间，恢复数据库后重启应用，不能删除卷“修复”。模型超时由任务记录有限重试，达到上限后显示失败；检查任务状态与错误类型再决定人工重试，避免把不确定的调用重复当作成功。待人工批准的工作流不会自动批准；若批准已写入而 checkpoint 暂不可用，使用审核页的冻结内容和手动对账入口，先查询批准记录，避免创建第二个版本。Worker 重启会回收运行中任务，模型调用可能重复，不能宣称恰好一次。恢复失败时保留源卷与备份文件，先在另一空库诊断扩展/版本/权限问题，不在源库反复恢复。

## CI、日志和发布边界

`.github/workflows/ci.yml` 在 Windows/Python 3.13 安装 `requirements.lock`，运行 `pytest -m "not integration"`，不配置模型密钥。YAML 本地解析通过；本机同一选择集 **124 通过、106 未选中**，有 1 条 Testcontainers 导入路径弃用警告。GitHub 托管 CI 只有推送后才有运行证据，不能把本机结果当成远端 CI 通过。Docker/Testcontainers 集成测试仍须本机运行。

Worker 的 JSON 事件只记录 `task_id`/`run_id`、`job_id`、步骤、尝试和重试次数、耗时、状态和异常类型，不输出异常正文、连接串或简历/JD。四项日志脱敏与异常路径测试通过。`task_id` 当前等于 `run_id`；批准版本 ID 不在 worker 事件中，跨批准与导出的统一追踪仍是后续工作。日志测试覆盖故障与重试路径，不能替代所有模块的隐私审计。

全量回归使用 `python -m pytest -p no:cacheprovider -q -W error::pytest.PytestUnhandledThreadExceptionWarning`，**230 项通过、1 条 Testcontainers 弃用警告**，未发生未处理的 worker 线程异常。回归中定位到已有 T8 杀进程恢复缺口：checkpoint 存在非空数据、却没有终态且 `next` 为空时，旧逻辑会将任务判失败。现在同一 `run_id` 从原 JD 重新进入工作流；测试在子进程实际进入模型调用后杀进程，再检查任务回收和进入待审核状态。该恢复是至少一次执行，不承诺模型调用恰好一次。

仓库现有四个 JD fixture 与 T11 合成标注数据是项目内编写的测试材料；浏览器集成文档只引用外部项目，没有将其代码纳入仓库。`git ls-files` 当前未列出 PDF/DOCX 等二进制简历产物，但 `git log --all --name-only` 显示历史提交曾有 `resume-v1.docx` 至 `resume-v5.docx`，`f586664` 才从当前树移除。**历史内容尚未逐项核对或清理，公开分发前必须做隐私审查**；不要误称当前 `.gitignore` 已消除历史风险。仓库仍缺独立 `LICENSE`，维护者须决定许可后再发布；旧 README 的 MIT 声明不等于已授权第三方复用。
