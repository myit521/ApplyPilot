# T18：新合成 JD 留出样本

T18 新写 20 份合成 JD，在任何 T18 模型调用之前冻结原文和 92 条来源分句标注。它们与 T11 的 10 份 JD 没有完全相同的原文；这只是新的定向压力样本，并非随机抽取的真实职位，也不能称为真实场景准确率。样本没有个人简历、真实招聘页面或投递行为。

样本位于 `tests/fixtures/t18_holdout_jds.json`。每个 `source_span` 都必须是对应 JD 的原文子串；`kind` 描述分句性质。复合技能有时放在同一分句，因此 92 是**分句数**，不能与 T17 的逐技能原子覆盖数相加或比较。`one_of`、`optional`、`non_requirement` 和 `unspecified` 是人工复核线索；现有 `JobRequirements` 没有前三者的专用字段，不能用字段相等来给这些类别打分。尤其要检查“非要求”是否被误升格成 `required`、二选一是否被误解成两项都必备。`unspecified` 不自动判为必备或加分。

离线校验脚本 `scripts/validate_t18_holdout.py` 检查数量、唯一性、T11 原文不重复、敏感联系方式模式及来源片段存在性。它无法判断语义标注是否正确，所以对 92 条标注又做了只读人工核对；自动校验通过不能代替人工复核。

模型探测由 `scripts/evaluate_t18_live.py --output docs/evaluation/t18-live.json` 执行。固定最多 20 次请求，每例最多 1000 输出 token，0 次重试；每次记录后落盘，HTTP、传输或解析失败立即停止，已有报告拒绝覆盖。报告只保存解析后的结构、调用元数据和错误类别，不保存密钥或未经解析的模型正文。生成报告后仍需逐项人工核对语义，不能直接把“解析成功”报告成“要求提取正确”。

当前本机全量测试因 Docker Desktop Linux Engine 不可用而未完成：本轮运行结果为 148 passed、8 failed、99 errors；错误和失败均发生在 Testcontainers 初始化阶段。T18 定向测试 7 passed。容器集成测试应在可用的 Docker 环境或 CI 中复跑。
