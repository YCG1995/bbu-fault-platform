# 技能工坊 AI 生成行为评审

## 1. 模拟对象

- 工坊草稿：`bbu-fault-vsw-usbflash-ai`
- 输入：用现场样例构造的 USB FLASH 日志包（normal + 写保护 + sdb I/O error + SMART）
- 模型：`deepseek-flash`
- 流程：创建草稿 → 上传日志 → `analyze` → `check` → `build`

## 2. 实际结果

| 阶段 | 结果 |
| --- | --- |
| 日志分析 | 状态变成 `analyzed` |
| AI 起草 | 实际失败：YAML 解析报 `mapping values are not allowed here` |
| 失败后行为 | 自动回退到确定性 skeleton，仍标记 `analyzed` |
| 生成 profile | 1 条通用规则 E1，0 个根因，`triggers/exclusions/observable/impact` 多数为空 |
| 校验 | `check.ok=true` |
| 构建 | `build.ok=true`，`verified=false` |
| 发布风险 | `publish` 只检查 `build.ok`，未验证草稿仍可被管理员发布 |

AI 失败原因不是模型完全不响应，而是它把日志样例直接写进 YAML 描述时没有给含冒号的行加引号，导致 YAML 解析失败。更关键的是，失败被静默降级成了“可构建”的 skeleton。

## 3. 主要问题

### P0：AI 失败被静默吞掉

`bbu/workshop.py::analyze` 捕获 `yaml.YAMLError` 后只往 `analysis.notes` 写一句“AI 起草失败”，状态仍设为 `analyzed`。UI 和后续 `build` 很容易把它看成一次正常分析。

建议：

- 增加 `analysis.ai_status`：`success / invalid_output / fallback`；
- 失败时状态改为 `needs_manual` 或 `draft_failed`，不要设为 `analyzed`；
- 在 UI 顶部显示“AI 起草失败，当前为确定性骨架”；
- 失败草稿禁止直接 `publish`，除非管理员显式选择“按骨架发布”。

### P0：未验证草稿可发布

`workshop.publish` 只检查 `build.ok`，不检查 `build.verified`。当前 AI 草稿没有根因、没有案例，仍可被管理员发布进技能库。

建议：

- 默认要求 `build.verified=true` 才允许发布；
- 增加 `allow_unverified_publish` 配置，默认 false；
- 未验证草稿只能进入“候选技能”区，不进入正式 router 目录；
- 发布前强制至少 1 个正例 + 1 个反例。

### P0：工坊输入太薄，AI 拿不到完整故障知识

当前 `create_draft` 主要接收 `name/title/fault_name/category/description`，而用户提供的 `triggers/exclusions/observable/impact/device/boards/version/处理动作/风险/验证方式` 没有结构化入口。AI 只能从简短 description 和日志样本猜。

建议：

- 工坊增加“结构化故障卡”步骤：故障名、别名、设备/单板/版本、现象、触发条件、排除条件、日志位置、根因、处置、风险、验证；
- `analyze` 请求把这些字段显式传给模型；
- 模型不允许把未知字段全部写成“待补充”后仍然算完成，UI 给出缺失项清单。

### P1：AI 输出用自由 YAML，缺少修复闭环

`bbu/prompts.py` 里已经定义了 `WORKSHOP_REPAIR_HINT`，但代码中没有使用。AI 第一次 YAML 不合法时没有带错误重试，直接 fallback。

建议：

- 第一次解析失败后，把 YAML 错误和缺失字段回传模型，要求“只输出合法 YAML”重试一次；
- 仍失败时保存原始输出摘要，状态为 `invalid_output`；
- 更稳的方案是让模型输出 JSON（平台已有 `json_schema` 校验能力），后端再 `yaml.safe_dump` 转 YAML，避免冒号、换行、引号问题。

### P1：规则候选扫描噪声大

本次日志里最关键的 `protect_status: 20102`、`usb 1-1: USB disconnect`、`Buffer I/O error on dev sdb4` 没有被转成高质量规则；扫描器反而给出：

- “clock x1”关键词规则；
- “2021-01-02T03 x2”时间戳规则。

这些规则既不能定位 USB FLASH，也会造成误命中。

建议：

- 增加 USB FLASH 领域词表：`protect_status`、`usb 1-1`、`sdb`、`Buffer I/O error`、`blk_update_request`、`this flash not support`；
- 不要用日志时间戳生成规则；
- 对 `protect_status` 解析“字段名 + 值”，不要只做泛关键词统计；
- 规则候选要保留文件和行号，AI 必须基于候选写规则。

### P1：没有案例库，生成没有验收依据

当前流程没有 C1–C5 真实日志包，`build` 走 `--skip-regression`，所以只能证明“文件生成了”，不能证明“判定正确”。

建议：

- 工坊创建草稿时强制录入案例卡；
- 至少 1 个正例 + 1 个反例才能进入 `build` 的正式模式；
- 没有案例时只允许生成 `triage` 草案，不允许发布；
- 每个根因至少一个案例覆盖，规则命中/未命中要写进 `case.yaml`。

### P2：环境与路径问题

- 当前环境没有 `7z`，真实 DCM 包里的 `kernel.klog.1.7z` 无法解压；部署包应带 `7z/7za` 或 `py7zr`。
- 合成目录出现 `glob_fallback`，真实包需要标准 `OssLogVsw/`、`BspLog/` 路径；工坊应显示 `glob_fallback` 告警，提醒人工确认。

## 4. 建议的改造顺序

1. 先修 `analyze` 的失败状态和 `publish` 的 verified 门禁；
2. 再上结构化故障卡和 JSON 输出；
3. 再改规则候选扫描器；
4. 最后接真实 C1–C5 案例回归，通过后再发布。

## 5. 结论

当前工坊的 AI 起草能力可以“生成一个结构合法的骨架”，但还不能直接承担“生成可发布故障技能”的职责。最需要优化的不是模型文风，而是：

- 失败必须显式暴露；
- 未验证不能发布；
- 先收集完整故障知识和案例，再让 AI 写规则；
- AI 输出必须经过 schema/修复闭环，而不是自由 YAML 一次成功或失败。
