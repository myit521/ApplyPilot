# T20：非要求与可选项的保守处理

T18 的合成 JD 暴露两类错误：h12 的“不限制实习经历”进入 `required`/经验要求，h20 的“GraphQL 可选”进入 `preferred`。另有 h07、h08、h18、h19 的非要求技术仅标为 `mentioned`，但匹配报告把它们作为独立技术要求展示。

解析器现在对明确含“不限制”“不要求”“不需要”“无需”“非必需”“经验不限”“不作为…要求”“可选”或“仅是…示例”等措辞的条件做一次语义校验。如果这类文字出现在必备、加分或经验要求中，或对应技术词仍被标为必备/加分，则沿用现有的一次修复调用；第二次仍不合规则返回最近一次的 `JDParseError`，不保存为成功解析。技术词核对只在原 JD 的分句内执行，避免“Java 必备，不要求 Redis”把 Java 一并拒绝；“接口可选参数”的“可选”不被当作岗位可选项。复杂的同词正反叙述可能需要人工改写或复核，不宣称这是一套通用中文否定理解器。

匹配报告对已有解析数据另设保护：明显含非要求/可选措辞的 `required` 或 `preferred` 条目不参与证据匹配，列入待核对项；仅标为 `mentioned` 且未被其他条件引用的关键词也不生成“支持/无证据”的要求卡片。这避免把 h12、h20 及四例非要求技术展示成待满足要求。若关键词同时出现在明确条件中，条件仍正常匹配。

该保护不改数据库 schema 或历史记录。旧解析结果原文仍可在职位详情中看到；T20 时事实检索仍使用全部 `mentioned` 关键词做候选召回，该词项边界随后由 [T21](t21-execution.md) 调整。候选排序效果尚未测量，候选也不能成为支持证据。尚未使用真实招聘网站或个人简历测试。

定向回归命令：`python -m pytest -p no:cacheprovider -q tests/test_t20_jd_polarity.py tests/test_jd_parser.py tests/test_t19_disjunctive_match.py tests/test_t4_match.py -m "not integration"`，结果 32 passed、3 deselected。Docker Desktop Linux Engine 恢复后，`python -m pytest -p no:cacheprovider -q --tb=short` 全量回归 279 passed、3 warnings（依赖弃用警告）。
