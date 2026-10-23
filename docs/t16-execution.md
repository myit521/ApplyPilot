# T16 合成 JD 技术名逐项核对

日期：2026-10-11。本轮不发模型请求，也不修改 T11/T15 原始样例或 T15 原始报告。目标是先把 T15 暴露的金标粒度问题缩到一个可复核的子集，避免把复合句的字符串差异误报为解析错误。

## 标注口径

- 只标 JD 原文明确出现的**具名技术、产品或框架**，每项保留原文片段与 `required` / `preferred` / `mentioned` 级别。只看职位正文的要求和加分语境，不把岗位标题自动当作硬性要求。
- 职责、通用能力和业务主题暂不进入这个子集。例如“业务接口开发”“慢查询分析”“跨团队联调”“高可用”即使可能有用，也需要另一套逐项口径；它们出现在模型 `keywords` 时只列入人工复核，不自动判错。
- `jd08` 原文只明确 **MySQL 调优**，没有明确要求 PostgreSQL 调优；标注 MySQL、PostgreSQL、SQL 三个具名技术，不替原文补写组合能力。`jd09` 没有具名技术，“后端”仅在岗位标题中。
- 此处按技术名忽略大小写精确比对，复合要求可拆成多项；不使用子串或语义近似匹配。它不评判 `required` 等完整句子、教育、地点、职责、未知项、事实匹配或召回。

[冻结标注](../tests/fixtures/t16_named_technologies.json)绑定 T11 JD fixture 和 T15 原始报告各自的 SHA-256；[离线脚本](../scripts/evaluate_t16_keywords.py)同时核对报告所记 fixture 摘要、10 个用例 ID、原文证据片段和模型输出结构。更改 fixture、报告或缺失用例会失败。

运行：

```powershell
$env:PYTHONPATH = 'src'
python -m scripts.evaluate_t16_keywords --output docs/evaluation/t16-named-technologies.json
python -m pytest -p no:cacheprovider -q tests/test_t16_keywords.py
```

本次离线输出为 [逐例报告](evaluation/t16-named-technologies.json)：10 份合成 JD 中，22 个具名技术均在 T15 输出中找到，22 个级别也与这份人工标注相同；另有 10 个输出词条列为未评分的人工复核项。其中 `jd09` 的“后端:required”来自标题，按上述口径不能视为原文明确的必备技术。这些数字只描述同一小组合成样例的一个字段子集，**不是 JD 解析准确率、精确率或真实岗位效果**。

验证：`tests/test_t11_evaluation.py`、`tests/test_t15_live_evaluation.py` 与 `tests/test_t16_keywords.py` 合计 **16 项通过**；完整无 Docker 回归 **135 项通过、107 项未选、1 条依赖弃用警告**。均未调用真实模型。

T15 报告中的 `source_commit` 是历史隐私清理前的提交 ID，清理后该 ID 不再是公开分支的当前地址；原报告保留原值以免篡改运行记录。下一步需独立冻结完整 JD 原子要求、职责和未知项的映射规则，并在新的、来源清楚且脱敏的样本上复核，再考虑更大范围的模型评测。
