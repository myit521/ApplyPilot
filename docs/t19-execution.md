# T19：明确二选一条件的匹配解释

T18 的真实模型输出在 h10、h16 保留了“或/任一”的 `required` 原文，但关键词分别把两项技术都标为 `required`。原先 `build_match_report` 按“全部关键词命中”判定，用户若只具备其中一种会看到 `partial`，与 JD 原意冲突。

本轮仅在**一条条件恰好提取到两个关键词**、原文含“技术 A 或 技术 B”，且明确写“任一”或“至少…一种”时按任一项命中判 `supported`。普通合取条件仍按全部命中；更复杂的嵌套条件不据此推断。证据仍只来自已确认且启用的事实，报告列出实际命中的技术，提示人工核对上下文。

`tests/test_t19_disjunctive_match.py` 用 T18 冻结的 h10、h16 输出验证只有一个技能即可支持，同时验证两者都没有时是 `no_evidence`，合取条件只有一项时仍为 `partial`，没有紧随“或”选择项的“任一/至少一种”标记时不自动判支持。同句前部无关的“至少一种”不能触发误判。定向命令 `python -m pytest -p no:cacheprovider -q tests/test_t19_disjunctive_match.py tests/test_t4_match.py -m "not integration"`：8 passed、3 deselected。本机 Docker Desktop Linux Engine 不可用，3 条相关容器集成测试本轮未运行。

这只修复匹配报告的显式二选一判定。`search.required_skill_terms` 仍会把两个技术都用于召回覆盖率打分；T18 h12 的否定条件误升格、h20 的可选项误入加分以及非要求关键词单列仍待单独处理。不能据此宣称任意复杂 JD 都能正确解释。
