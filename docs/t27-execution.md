# T27：CI 冻结证据复现与限额真实模型流程

日期：2026-10-19；起点 `69e9681`，继续 PR #1，不自动合并。

## 先审查方案

优先验证交付链，不增加功能。T26 的本机 318 项回归不能代替托管 CI；真实模型只使用新编合成资料，先核对生成内容再批准测试版本，不接触招聘网站。

## CI 失败与修复

[PR CI 第 21 次运行](https://github.com/myit521/ApplyPilot/actions/runs/39226440048)安装依赖成功，但离线测试为 **10 failed、190 passed、118 deselected**。错误集中在 T16/T17 冻结文件字节哈希。不是模型调用失败，也不是需要更新金标。

根因有两个方向：Git 默认换行转换将 `tests/fixtures/t11_jds.json` 的 LF 变成 CRLF；T15 报告虽然 Git blob 为 LF，本机原始报告为 CRLF，T16/T17 保存的是原始 CRLF 报告哈希。因此把所有 JSON 强制为 LF 仍有 7 项失败。

`.gitattributes` 明确现有契约：`tests/fixtures/*.json` 使用 LF，`docs/evaluation/*.json` 使用 CRLF。未修改 JSON 内容、标注哈希或校验逻辑。今后生成新的报告和字节标注时须沿用该契约；若迁移格式，需新证据版本，不能静默改写旧金标。

复现使用只读 Git 索引导出至全新的忽略目录，不更改全局 Git 配置：

```powershell
git -c core.autocrlf=true checkout-index --all --prefix=D:/ApplyPilot/.pytest_cache/t27-ci-before/
# 在导出的目录运行
python -m pytest -p no:cacheprovider -q tests/test_t16_keywords.py tests/test_t17_jd_atoms.py --tb=short
```

修复前：10 failed、1 passed。增加属性后导出另一个全新目录，运行与 CI 相同的离线范围：

```powershell
python -m pytest -p no:cacheprovider -q -m "not integration" --tb=short
```

结果：**200 passed、118 deselected、1 warning in 7.55s**。真实托管修复结果须以新提交的 CI 为准，不能以本机模拟替代。

## 限额真实模型流程

本轮临时验证工具只使用新编合成身份、Java 接口开发事实和一份合成 JD，临时 PostgreSQL 与本机 HTTP 服务隔离。模型请求复用 T14 单次调用函数，最多 6 次，每次最多 1200 输出 token；不在网络层自动重试。请求与 token 上限不等于人民币费用上限。

使用空向量替身，验证关键词路径、真实 JD 解析/生成/语义复核及人工审批 API、导出、用户自述投递记录的接缝，不测本地向量或真实网站。父 Agent 必须核对生成文本后才能为该合成工作流写入审批信号。该工具不是产品配置开关，也不证明默认生产适配器已具备相同预算机制。

当前阶段为准备与只读复审，尚未写入真实模型成功结论。旧 `scripts/smoke_e2e.py` 的样例含具体学校/单位，且审批调用没有当前修订号，本轮不运行它、不发送该样例；需要另行修订并核对隐私历史，不能据 T26 的文件路径扫描认定所有源码样例均脱敏。
