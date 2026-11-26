---
name: demo-bbu-clock-loss
description: "BBU 故障分析技能：时钟失锁（适用：BBU、ITBBU、SDR）。当用户提供 BBU/ITBBU/SDR 日志压缩包或 CloudMAT 工装日志，并描述以下故障时使用：告警码 CLK032；时钟失锁、参考时钟异常、时钟不同步、时钟、失锁、PLL、参考源、CLK032、频偏、不同步。功能：递归解压、证据检索（时序/关联键/数值趋势/多站点）、根因判定、MD+HTML 报告。仅覆盖本技能 profile 中已验证的根因分支。Use for BBU/ITBBU/SDR base-station fault analysis: log archive evidence search, root-cause verdict and MD/HTML report for '时钟失锁'."
---

# [示例] 时钟失锁诊断

## 输入与停止条件

- 必需：用户故障描述。可选：日志包、单个日志或已解压目录。
- 先核对 `references/fault-profile.md` 的适用范围与排除项；明显不属于本故障时退出，不强行套用。
- 无日志时只给可能性分析，明确“无日志、无证据”，不得给确定性根因。
- 路径相对本 SKILL.md 目录；跨目录执行用绝对路径。Windows 用 `python` 代替 `python3`。

## 一键分析

```bash
python3 scripts/run_analysis.py "<日志包|目录|单日志>" \
  --description "<用户故障描述原文>" \
  --out <输出目录>
```

多个日志文件可依次写在输入位置；目录只含并列采集包时自动合并。可选参数仅在对应输入已存在时添加：`--specialized <复核.json>`、`--fm-align <对齐.md/json>`、`--site <站点键>`。

脚本自动完成定向解压、证据检索、时钟轮转合并、根因判定、MD/HTML 报告和交付门禁。**不要拆成多步重复执行，不读脚本源码。** 同一输出目录二次运行输入未变化时命中增量缓存（`run-summary.json` 中 `cached: true`，跳过解压与检索重跑）；`--no-cache` 强制全量。`--local-tz-offset` 默认 +8（东八区），跨时区采集按日志实际时区显式传入。

完成后只读 `<输出目录>/run-summary.json`：

- `ok: true`：报告已通过门禁，交付 `report.md` 和 `report.html`；需要简要说明时读取摘要中的 `status`、`primary`、`sites`。
- `ok: false`：只查看摘要指向的失败阶段日志尾部，修复输入、环境或 profile 的明确问题后重跑；不要原样盲目重试。
- 需要证据细节时先读 `evidence.brief.md`，不要整读 `evidence.json`、`manifest.json`。

## 判定约束

- 以 `verdict` 为判定基准；`unmatched` 不等于设备正常，`temporal_conflict` 不得直接归因。
- 多站点只认同站点证据，输出一份汇总报告，不跨网元拼证据。
- verdict 有误时回 builder 修 profile 并回归，不手改结论或证据。
- profile 声明专项复核时，先按其说明生成 `specialized.json`，再把 `--specialized` 传给一键命令；专项与宽松规则冲突时以专项为准。

## 报告要求

报告面向现场用户，只回答：

1. **故障原因**：是什么、为什么会导致该现象；未登记的物理机理明确写“未登记”，不推测。
2. **证明依据**：命中条件、时序关系，以及文件、行号、时间戳、原文；不得手改证据。
3. **解决办法**：按根因给出动作、前提、风险、回退、恢复验证和升级路径。

不写脚本运行过程、命令日志、检索覆盖明细或内部 TODO。未命中时说明证据缺口与下一步补采/排查建议，禁止强行归因。

## 数据边界

- 告警码含义按需查 `assets/alarm-code-dictionary.yaml`；`verified: false` 条目不得作为唯一结论依据。
- 时钟字段枚举与阈值按需查 `assets/clk-field-dictionary.yaml`。
- 二进制、密文和未登记格式默认只登记、不解析；缺少采集文件时写“证据不足”，不得把“未采到”写成“无故障”。
- 新根因、误判或新日志格式回 builder 更新 profile 并全量回归，不在子 Skill 内硬编码补丁。

<!-- 由 bbu-fault-skill-builder v2.11.0 生成/更新 | digest tk-8c5ee4035044 | 2026-11-24T09:03:10+00:00 -->
