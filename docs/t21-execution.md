# T21：检索词的要求级别边界

T18 的模型输出同时包含岗位要求、职责词和只在背景中提及的技术。T20 已阻止后者在匹配报告中显示成要求，但事实检索仍把所有 `keywords` 放进全文和向量查询；`required_skill_terms` 在没有必备关键词时还会把加分或职责词当成必备技能覆盖率评分。

本轮只修改检索词构造：必备/加分技术仍可用于候选召回；`mentioned` 技术只有出现在安全的条件或职责文本中才参与召回。明确含非要求/可选措辞的旧解析条目不进入查询，其独有技术词也被排除。职位标题与类别仍用于向量查询，因此这不是把背景词从所有上下文彻底消除。没有必备关键词时，必备技能覆盖率只可回退到安全的必备原文，不再借用加分或职责词。

测试直接使用 T18 冻结输出：h07/h08/h18/h19 的非要求技术以及 h20 的可选 GraphQL 不进入检索词；h07 的“备份与恢复”职责词保留；h12 的“不限制实习经历”不进入向量文本。同时覆盖“Java 必备，不要求 Redis”、只有无关提及词，以及仅有加分/职责词的必备技能评分边界。定向测试 `python -m pytest -p no:cacheprovider -q tests/test_t21_search_terms.py tests/test_t20_jd_polarity.py tests/test_t19_disjunctive_match.py --tb=short`：35 passed。

DSH 只读复审发现职责中的否定括注仍可能被召回；已用“负责消息链路（不需要 RabbitMQ）”反例复现并修复。当前会排除整条含明确非要求措辞的职责，代价是其中的正向职责信息也暂不进入查询；后续如有真实需求再做更细的分句处理。

2026-10-13 离线全套回归 `python -m pytest -p no:cacheprovider -q -m "not integration" --tb=short`：183 passed、107 deselected、1 warning。首次默认全量运行因 Docker Linux Engine 不可用得到 183 passed、8 failed、99 errors；容器错误发生在 Testcontainers 初始化。

随后确认 Docker Engine 29.7.2 可用，在代码提交 `8c884e6` 上补跑 `python -m pytest -p no:cacheprovider -q --tb=short`：**290 passed、3 warnings，耗时 125.82 秒**。包含此前未验证的 107 项容器集成测试，以及合成数据浏览器彩排。警告为 Testcontainers 与 WebSockets 的依赖弃用提示；测试启动时另有 Requests 依赖版本提示。

这只验证词项选择规则。候选排序的实际 Recall@5、真实职位分布与向量模型效果仍未测量；复杂正反混合条件仍需要人工复核。
