---
name: bbu-fault-skill-builder
description: "把 ITRAN/SDR 等基站的时钟、电源、光模块、链路、复位、升级、CloudMAT 拷机等故障诊断经验生成可复用子 Skill。用于创建、更新、同步 BBU 故障分析 Skill，或从真实案例、专家经验、黑匣子/蜂鸟规则库构建 fault-profile；不用于直接分析单次故障。输入日志与故障描述，输出根因、证据和处理方案报告。"
---

# BBU 故障分析 Skill 生成器

## 目标与边界

生成 `bbu-fault-<slug>` 子 Skill，不直接分析故障。一个子 Skill 只覆盖一类故障及其判别分支。

**交付门槛**：至少 1 个可访问的正例（日志、已知根因、预期证据）；根因条件只引用已在真实案例验证的规则；处理方案来自用户或专家确认。缺正例时只整理访谈与候选 profile，不生成"已验证"技能。

## 省 token 规则

- **读取按需**：只读当前阶段所需参考文件；脚本直接执行，仅在报错或需改动时才读源码片段。
- **读摘要不读全量**：不整读 `manifest.json` / `evidence.json` / 成员清单，改读 `*.summary.json`、`*.brief.md`、构建摘要。
- **yaml-only**：规则/配置统一 `.yaml`。
- **不重复提问**：先从案例提取候选，只补问缺失/冲突项，一次最多 4 问。

## 选型与产出

| 决策 | 取值 | 判据 |
|---|---|---|
| `execution_mode` | `script`（默认）/ `agent` | 规则闭合、可一键跑完 → `script`；多步推理、多脚本或多 Skill 汇总 → `agent` |
| `category` | 故障大类 | 供 router 按大类聚合，粒度与业务研发确认 |
| `quick_signatures` | 浅扫特征 | 源自日志特征字，导出 `assets/quick-signatures.yaml`；只标疑似、不作根因 |
| `mode` | 正式 / `triage` | `triage` 为规则库生成的候选 profile：规则全 `verified: false`、只挂 steps、报告为分诊线索，router 默认跳过；须案例验证后转正式 |

## 入口分层

| 入口 | 做什么 | 何时用 |
|---|---|---|
| `scripts/scaffold_child_skill.py` | 只渲染子 Skill（不校验、不回归） | 底层生成、调试 |
| `scripts/build_child_skill.py` | 临时构建 → 规范校验 → 判定/覆盖率/关键案例端到端 → 原子替换 | 只要产物、不打包 |
| `scripts/publish.py` | build + 打包 zip + 同步安装位 + 可选同步子 Skill | 交付分发（推荐） |
| `scripts/skill_sync.py` | builder ↔ 子 Skill 基础能力双向同步 | 更新已有子 Skill、回灌修复 |
| `scripts/import_rule_library.py` | 黑匣子/蜂鸟规则库 → 候选 profile（triage） | 有规则库时 |
| `scripts/init_case.py` | 初始化案例目录 | 建案例库 |
| `scripts/check_env.py` | Python / PyYAML / 7z 环境自检 | 首次使用、排错 |

## 构建流程

### 1. 受理并准备 profile

1. 确认故障范围、设备/版本、采集方式和正例。按 `references/workflow/intake-questionnaire.md` 补齐缺失字段；对外收集用 `references/workflow/submission-template.md`；日志包结构查 `references/platform/log-package-map.md`；单板域知识按单板域到 `references/domains/` 取用，不跨单板套用，也不在主线展开。
2. 复制 `assets/templates/child-skill/assets/fault-profile.yaml` 为工作 profile，填写规则、根因 `condition`、`mechanism`、处置动作和案例。字段含义仅在需要时查 `references/spec/child-skill-spec.md`。
3. 已有黑匣子/蜂鸟规则库时，用 `scripts/import_rule_library.py` 生成**候选** profile（`mode: triage`）；候选规则仍须案例验证，不能直接交付。

规则必须可检索：写明文件 glob、原文关键字/正则、命中阈值、关联键和必要时序。阈值须有来源；未验证规则标 `verified: false`，不得进入根因条件。

### 2. 构建与回归

在本 Skill 目录执行：

```bash
python3 scripts/publish.py --profile <fault-profile.yaml> --path <skills根> --cases-dir <案例库根>
```

`publish.py` 在同文件系统临时目录依次完成 profile 校验、子 Skill 生成、规范校验、全案例判定、根因/规则覆盖门禁和关键案例端到端报告门禁，全部通过后才原子替换正式目录，再打包（可附 CHANGELOG）和同步安装位；失败不覆盖旧版本，并自动保留完整阶段日志。只要产物、不打包用 `build_child_skill.py`，参数相同。完整参数（`--zip-out` / `--install-dir` / `--sync-skills` / `--dry-run` / `--log-dir`）见 `references/workflow/toolkit.md`「一键入口」。

案例库结构见 `references/workflow/validation-protocol.md`；需要初始化案例时使用 `scripts/init_case.py`。

- 更新已有子 Skill 时加 `--update`，不要加 `--force`。
- 仅生成草稿时显式用 `--skip-regression`；产物标记"未验证"，不得作为已验证技能交付。
- 判定与案例不符时修 profile 后重跑，不手改 verdict 或报告。
- 修改 builder 自身脚本/模板后才运行 `python3 tests/selftest.py`；不必在每次生成子 Skill 时跑 builder 全量自测。

### 3. 交付

只交付回归通过的子 Skill，并简述已验证案例、适用边界和未覆盖项。子 Skill 的日常运行只需一条命令，见生成后的 `SKILL.md`。

## 更新与回收（仅在用户要求时）

```bash
# 同步 builder 基础能力到子 Skill，保留其 profile、专项脚本和定制报告模板
python3 scripts/skill_sync.py --sync <子Skill目录>

# 查看子 Skill 中可回灌的通用修复；确认后才写回
python3 scripts/skill_sync.py --reclaim <子Skill目录>
python3 scripts/skill_sync.py --reclaim <子Skill目录> --apply
```

回收后运行 builder 全量自测。不要回收子 Skill 的 profile 或专项脚本。

## 按需参考

| 文件 | 何时读取 |
|---|---|
| `references/spec/child-skill-spec.md` | profile schema、组件、verdict、报告契约 |
| `references/workflow/intake-questionnaire.md` | agent 追问口径：规则、机理、处置与案例的验收标准 |
| `references/workflow/submission-template.md` | 发给业务/领域专家填写的对外表单 |
| `references/workflow/validation-protocol.md` | 建案例库、写稳定断言、处理回归失败 |
| `references/workflow/toolkit.md` | 高级脚本参数、规则库导入、同步与排错 |
| `references/platform/log-package-map.md` | 写 glob，识别 DCM/SDR/设备侧/CloudMAT/FM 数据格式 |
| `references/platform/input-collection-contract.md` | 平台对接：输入参数、DCM 采集项、缺项降级、场景模板覆盖、router 索引字段 |
| `references/domains/` | 单板域知识，按单板取用：VSWe 工装测试 → `clk-fixture-map.md`；VSWe 现场/网管维测 → `vswe-clock-maintenance.md`；ITRAN 寄存器 → `fpga-register-map.md`。分不清时看各文件头的"何时读本文件" |

所有命令路径相对本 SKILL.md 目录；跨目录执行时改用绝对路径。Windows 用 `python`，不要在 PowerShell 中拼接 `&&` 或合并 stdout/stderr。
