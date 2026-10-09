# T13 合成数据浏览器彩排记录

日期：2026-10-09。基准提交 `b5b7e49`。本次只验证本机真实 HTTP 服务与 Edge 页面接缝，不调用 DeepSeek、招聘网站，也不执行真实投递。

`tests/test_t13_browser_rehearsal.py` 在临时 PostgreSQL/pgvector 中初始化 schema，用本机 Uvicorn 启动真实 API、worker 和 checkpoint，用 Playwright 驱动已安装的 Microsoft Edge。模型接口注入固定合成响应；测试不设置模型密钥。浏览器依次完成联系资料和事实确认、保存和解析合成 JD、查看事实证据、启动草稿、进入人工审批、批准冻结版本、请求 DOCX，最后在人工记录页以“未知”结果保存合成记录。数据库再核对该记录绑定批准版本，来源为 `user_reported`。

```powershell
$env:PYTHONPATH = 'src'
$env:APPLYPILOT_DEMO_ARTIFACT_DIR = 'D:\ApplyPilot\docs\evaluation\t13-browser'
python -m pytest -p no:cacheprovider -q tests/test_t13_browser_rehearsal.py
```

本机 Windows/Python 3.13、Edge、Playwright 和 Docker Desktop 下连续运行三次，各 **1 项通过**；生成的[审核页](evaluation/t13-browser/review.png)与[人工记录页](evaluation/t13-browser/applications.png)截图已目视检查，仅含合成姓名、邮箱、公司、事实与随机测试 ID。测试有 3 条来自 Testcontainers/websockets 的弃用警告。未安装 Playwright 或 Edge 时会明确跳过，跳过不等于彩排通过。截图对应当次临时库，测试结束后服务与数据库容器已关闭。

完整回归 `python -m pytest -p no:cacheprovider -q -W error::pytest.PytestUnhandledThreadExceptionWarning` 为 **231 项通过、3 条依赖弃用警告**。浏览器服务在测试结束后断言已退出；DOCX 请求校验了 ZIP 文件头和 Word MIME 类型，内容快照仍由 T10 专项测试覆盖。

这比 T10 的模拟页面测试多验证了浏览器事件、真实 API、worker、checkpoint、审批事务、DOCX 和应用记录的接缝；它仍不测真实模型质量、多页 DOCX 视觉效果、浏览器插件或招聘网站兼容性。人工记录中的“未知”仅表明用户自述，不能推断已向网站提交。
