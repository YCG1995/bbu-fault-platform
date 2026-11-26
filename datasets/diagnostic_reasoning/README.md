# 诊断推理数据集

全部为合成日志，用于技能回归、planner 检索评估和报告推理链展示，不代表真实现网验证。

- `*.corpus.jsonl`：按案例生成的推理语料，含现场描述、竞争假设、证据顺序、鉴别点、误判陷阱和处置动作。
- `*.benchmark.jsonl`：离线基准题，字段 `query` 用于拼 planner 输入，`root_cause` 与 `expected_rules` 为金标准。
- `playbooks/*.yaml`：从技能 profile 抽出的专家排查策略，可直接作为技能目录的 reasoning 摘要来源。
- `summary.yaml`：案例数量、根因分布和类型分布。

这些语料不是模型自由生成的思维链；每条推理都绑定到真实规则编号和案例断言，报告只能陈述实际命中与未命中。
