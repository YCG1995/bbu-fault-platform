# 子 Skill 统一格式规范（Child Skill Spec）

> 所有由本生成器产出的 BBU 故障分析子 Skill 必须符合本规范。统一格式保证：可复用同样的脚本、报告模板；新增故障只改 profile 不改流程；多个子 Skill 可被同一上层流程串联。

## 目录

1. 目录结构
2. fault-profile.yaml 字段规范（脚手架输入）
3. evidence-rules.yaml 证据规则 schema
4. 判定表达式与根因逻辑
5. 报告模板规范（MD/HTML）
6. 子 Skill SKILL.md 标准流程
7. 扩展指南

## 1. 目录结构

```
bbu-fault-<slug>/
├── SKILL.md                          # 固定工作流（由模板按组件条件渲染，基本不手写）
├── references/
│   └── fault-profile.md              # 人读故障知识：名称/场景/现象/决策链/处理方案（由 profile 渲染）
├── assets/
│   ├── fault-profile.yaml            # 规则/根因事实源；案例索引由案例库 case.yaml 重建
│   ├── evidence-rules.yaml           # 由 profile 导出（rules + temporal + root_causes）；统一 yaml
│   ├── quick-signatures.yaml         # [按需] 由 profile 的 quick_signatures 导出，供 router 浅扫
│   ├── alarm-code-dictionary.yaml    # [按需] 共享告警码字典
│   ├── common-rules.yaml             # [按需] 公共前置检查规则（预检 --rules 直接用）
│   ├── clk-field-dictionary.yaml     # [按需] 时钟字段字典（随 parse_clock）
│   ├── toolkit-version.json          # 版本戳：builder 版本/digest/受管文件 sha256/生成代数
│   ├── report_template.md            # MD 报告模板（--update 时保留用户定制）
│   └── report_template.html          # HTML 报告模板（样式供 render_report 复用）
└── scripts/
    ├── log_format.py                 # [随 unpack/search] 通用格式分类学与盘点（公共依赖，生成时由 builder scripts/ 复制）
    ├── unpack_logs.py                # [按需] 递归解压 + 故障导向提取（生成时由 builder scripts/ 复制）
    ├── search_evidence.py            # 证据检索 + 时序 + verdict（生成时由 builder scripts/ 复制）
    ├── render_report.py              # 从 evidence/verdict/profile 预填 MD/HTML 报告草稿（生成时由 builder scripts/ 复制）
    ├── run_analysis.py               # [按需] 一键分析入口：解压→检索→摘要→报告→verify 门禁（生成时由 builder scripts/ 复制）
    ├── parse_clock.py                # [按需] 时钟日志结构化（生成时由 builder scripts/ 复制）
    ├── parse_cloudmat.py             # [按需] CloudMAT 工装日志结构化（生成时由 builder scripts/ 复制）
    ├── scan_rule_candidates.py       # [按需] 规则候选自动扫描（生成时由 builder scripts/ 复制）
    ├── gen_specialized.py            # [按需] 专项脚本复核结论归一（生成时由 builder scripts/ 复制）
    └── fm_alarm_align.py             # [按需] 网管告警码与证据对齐（生成时由 builder scripts/ 复制）
```

`[按需]` 项由 `components` 决定是否复制（见 2.8）。默认只做故障导向提取（`unpack_logs.py --include-rules`），不全量解压大包；`.bin/.pcap` 及密文默认不解析。

命名要求：目录名与 SKILL.md frontmatter 的 `name` 完全一致；hyphen-case（`^[a-z0-9]+(-[a-z0-9]+)*$`，≤64，无首尾/连续连字符）；建议前缀 `bbu-fault-`。

frontmatter 遵循 Agent Skills 规范（agentskills.io）：仅 `name`、`description`（≤1024，脚手架转义写入，含故障名/别名/关键词/告警码/英文触发词）。版本信息见 `assets/toolkit-version.json`，故障名见 `fault-profile.yaml`；`scripts/validate_skill_md.py` 可校验；SKILL.md 正文命令路径均相对本目录，可在 Claude Code、Agent Skills 兼容平台及通过 zip 分发的自研平台直接加载。

## 2. fault-profile.yaml 字段规范

完整带注释模板见 `assets/templates/child-skill/assets/fault-profile.yaml`（以该文件为准，本节是语义说明）。

### 2.1 `skill` 段

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 子 Skill 目录名，如 `bbu-fault-1588-clock-source` |
| `title` | 是 | 报告与交付中的中文标题 |
| `version` | 是 | 语义化版本，如 `1.0.0`；每次重新脚手架递增 |
| `execution_mode` | 否 | 枚举 `script`（默认，确定性一键流水线）/ `agent`（复杂故障多步编排）；渲染进 SKILL.md 与 frontmatter |

顶层另有两个可选字段：

| 字段 | 说明 |
|---|---|
| `category` | 故障大类，用于 router 按大类聚合；粒度待业务研发确认，不填则不参与大类聚合 |
| `quick_signatures[]` | 浅度扫描特征，`{id, file_hints[], any_of[], note}`；脚手架导出 `assets/quick-signatures.yaml` 供 router shallow 模式匹配；只标疑似、不作根因 |

### 2.2 `fault` 段

| 字段 | 说明 |
|---|---|
| `name` / `aliases` | 规范名 + 别名/口语名/英文标识 |
| `alarm_codes[]` | `{code, name}`；无告警码写 `[]` |
| `domain.device_types` | 枚举：`BBU/OSS`、`SDR`、`ITBBU`、`CloudMAT`，可多选 |
| `domain.modes / boards / version_scope` | 制式、单板/槽位、版本范围（自由文本，确切未知写"待补充"并在验证后修订） |
| `collection_sources` | 枚举：`device-tar`、`sdr-zip`、`dcm-zip`、`cloudmat-text` |
| `triggers[]` | 典型触发场景 |
| `exclusions[]` | 明确不属于本故障的情形（排除分支依据） |

### 2.3 `symptoms` 段

- `observable[]`：一线可感知现象（自由文本）。
- `description_keywords[]`：用于匹配用户自然语言故障描述的关键词/正则（中英都给）。
- `lookalikes[]`：`{fault, differentiator}`，相似故障与第一条区分依据。
- `impact`、`time_pattern`：影响面与时间特征。

### 2.4 `analysis.steps[]`（决策链）

每个 step：

| 字段 | 说明 |
|---|---|
| `id` | `S1`、`S2`…（全局唯一） |
| `title` / `objective` | 步骤名与本步要回答的问题 |
| `targets[]` | `{globs[], format, encoding}`：本步目标文件；format 取 `text/csv/xlsx/json/xml/gz-text/binary`；encoding 取 `auto/utf-8/gbk` |
| `rules[]` | 证据规则，见第 3 节，规则 ID 全局唯一（`E1`…）；可加 `verified: false` 标注未经案例验证（不得被 condition 引用） |
| `decision` | `{on_hit: {conclusion, goto}, on_miss: {conclusion, goto}}`；`goto` 可写步骤 ID、根因 ID（`R1`）或 `EXCLUDE`（排除本故障） |
| `binary_note` | 若本步依赖二进制/密文文件，写明人工解析工具与操作；无则省略 |

### 2.5 `root_causes[]`

| 字段 | 说明 |
|---|---|
| `id` | `R1`… |
| `name` | 根因名称 |
| `condition` | 证据判定表达式（见第 4 节），只允许规则 ID / `and` / `or` / `not` / 括号 |
| `mechanism` | 已由专家确认的故障机理：说明该根因为何导致用户现象；最终报告直接使用。未登记时报告明确写“未登记”，不推测 |
| `temporal[]` | 可选。本根因必须满足的时序约束 ID；未声明时自动取 first/second 都以肯定形式出现在 condition 中的约束（`not E4` 不参与）。condition 含否定引用而又有相关时序时**建议显式声明** |
| `confidence` | `high/medium/low`；low 必须在报告中显式提示人工复核 |
| `actions[]` | 有序处置动作 |
| `prerequisites[]` | 前提条件（窗口/备件/备份） |
| `risks` | 风险与注意事项 |
| `rollback` | 回退方案 |
| `verify[]` | 恢复验证：`{globs[], patterns[], expect: absent|present, hold_time}`。`hold_time` 只写**时长或整句判定描述**（如 `24h` 或 `更换后观察 24h 无失效`），脚本渲染时不再拼接固定"观察"前缀，避免语义重复 |
| `escalation` | 升级二线/厂家的条件与需附带材料 |

### 2.6 `cases[]`

`case.yaml` 是案例断言的唯一来源；`build_child_skill.py --cases-dir` 读取磁盘案例库并重建本节，生成到子 Skill 的 profile 只把它作为展示索引，不再要求手工同步维护。

- 索引字段：`{id, type, archive, description, expected_root_cause, expected_evidence[], not_expected_evidence[], notes}`；执行时以 `case.yaml` 的 `kind/input/expected` 为准。
- 旧工作 profile 可保留 `cases[]` 用于迁移检查：同 ID 的类型、根因或证据预期与 `case.yaml` 冲突时构建失败；缺失/额外项只记录在构建摘要 `case_sync`，由磁盘清单自动替代。
- 全量回归另执行覆盖率门禁：每个根因至少一个非负例案例；condition 引用的规则必须被该根因案例的 `hit_rules` 或 `not_hit_rules` 覆盖；每个根因至少一个案例跑一键报告门禁。
- 反例必须写 `expected.root_cause: NONE`，放入 `case.yaml`，不要只写在 profile 的旧案例中。

### 2.7 通用约束

- 不允许出现无法对应规则的结论：每个 `root_causes.condition` 引用的规则必须存在；每个 `expected_evidence` 必须在案例上验证过。
- 未知项统一写 `待补充` 并在 fault-profile.md 的"未验证项"列出；禁止写"可能/大概"式规则。
- 脚手架 `--check` 会校验：ID 唯一、枚举值（rule type/trend、confidence、case type）、condition 语法与引用、`verified: false` 不被引用、temporal 引用/方向/区间、decision.goto 目标、案例引用与自洽、正则可编译、globs 非空、components 键、skill.name。生成前必须通过。
- **共现不等于排除**：同一日志里其他故障的现象（如伴随的电源类失败）不得写进本案例的 `not_expected_evidence`，应作为独立规则并列提示。

### 2.8 `components` 段（可选，控制脚本/资源按需复制）

子 skill **只复制本故障用得到的脚本与资源，不需要的不复制**。脚手架按 `components` 显式声明取舍；未声明时按 profile 全文自动推断，生成时打印每个组件的取舍与依据（"显式"或"自动推断"）。

```yaml
components:
  unpack: true          # unpack_logs.py：处理递归压缩包/采集包
  search: true          # search_evidence.py：证据规则检索（有 rules 时默认 true）
  report: true          # render_report.py + run_analysis.py：最终报告与一键分析（随 search）
  parse_clock: false    # parse_clock.py：时钟日志（clockstatuslog/gnss/ocxo 等 mList）
  parse_cloudmat: false # parse_cloudmat.py：CloudMAT 工装纯文本日志
  alarm_dict: true      # assets/alarm-code-dictionary.yaml：引用告警码时
  clk_dict: false       # assets/clk-field-dictionary.yaml：随 parse_clock
  common_rules: true    # assets/common-rules.yaml：采集完整性等前置检查
  extra_scripts:        # 专项分析脚本（如 scripts/ocxo_aging_analyzer.py）：
    - scripts/ocxo_aging_analyzer.py   # 纳入受管清单（--update 同步、--inspect 体检）
                                       # builder 模板无同名源时不覆盖/不删除，仅登记跟踪
```

`extra_scripts` 为可选列表，声明本故障独有的专项脚本；未被声明的子 skill 内 `.py` 会被 `--inspect` 提示"不受管"，便于人工判断是否纳入。纯 CloudMAT 单日志可推断为 `unpack: false`；此时 `run_analysis.py` 只接受一个非压缩日志并复制到隔离工作目录，不要求解压脚本。

自动推断规则（信号取自 profile 全文，小写匹配）：

| 组件 | 推断为 true 的信号 |
|---|---|
| `search` | 存在任意证据规则 |
| `report` | 随 `search` |
| `parse_clock` | 规则 glob/文本出现 clockstatuslog、gnss、clockocxolog、inputclock、Clk[、ocxo、1588、synce、spoofing 等 |
| `parse_cloudmat` | 出现 cloudmat、itemList、TestItemResult、getItemResultInfo、拷机/工装、cycling 等 |
| `unpack` | 有规则且非"纯 CloudMAT 单文件"（glob 为单 `*.log`、无采集包特征）；时钟/采集包恒为 true |
| `alarm_dict` | `fault.alarm_codes` 非空、出现 8 位告警码（如 `198096802`）、`fm-active` 或"告警码" |
| `clk_dict` | 随 `parse_clock` |
| `common_rules` | `unpack && search` |

- 显式 `components` 布尔覆盖推断；推断不确定时（如跨域）优先在 profile 显式声明并与用户确认。
- 子 skill 的 `SKILL.md` 由模板按组件**条件渲染**：未复制的脚本与字典，其步骤与引用一并移除，不产生悬空引用。
- `--update` 时按最新 profile 重新计算并增删脚本/字典（脚本为分发件会同步最新；仅报告模板保留用户定制）。

## 3. evidence-rules.yaml schema（rules + temporal + root_causes）

由脚手架从 profile 导出，**统一 YAML 输出**；search_evidence.py 只读 `.yaml`（需 PyYAML）。形如：

```yaml
rules:
  - id: E1
    step: S1
    description: 活动告警中存在 1588 时钟源异常
    type: text                    # text（默认）| numeric_trend
    globs: ["**/当前告警/fm-active_*.csv", "**/fm-active_*"]  # 末项通吃 .csv/.xlsx 导出
    encoding: auto                # auto | utf-8 | gbk | gb18030 | big5
    format: text                  # text | csv | xlsx（.xlsx 文件按后缀自动走行检索）
    columns: ["告警码名称", "告警码"]
    all_of: ["1588时钟源异常"]
    any_of: []
    none_of: []
    min_matches: 1
    context_lines: 2
    case_sensitive: false
    time_extract:
      pattern: "(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2})"
      group: 1
      formats: ["%Y-%m-%d %H:%M:%S"]  # 可选：显式解析格式，按序优先尝试（先于自动嗅探），规避默认嗅探盲区
      epoch: false                     # 时钟类逗号时间如 "2026-09-06,05:41:39" 需写 "%Y-%m-%d,%H:%M:%S"
    associate_by:                   # 可选：从命中行提取关联键（网元/槽位/单板）
      pattern: "(\\d_\\d+)"         # 跨文件证据按此键分组，时序比较只匹配同键
      group: 1

  # numeric_trend 规则示例
  - id: E5
    step: S3
    type: numeric_trend
    description: 时钟频偏持续超标
    globs: ["**/clockstatuslog.txt"]
    value_pattern: "频偏[:：=\\s]+([\\-\\d.]+)"
    time_extract:
      pattern: "(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2})"
      group: 1
    trend: exceed_threshold         # exceed_threshold | sustained_above | monotonic_increase | monotonic_decrease
    threshold: 1000
    sustained_count: 3
    window_minutes: 30              # 可选：只看最近 N 分钟
temporal:
  - id: T1
    first: E1
    second: E2
    min_gap_minutes: 0
    max_gap_minutes: 30
    note: "告警必须在复位前 30 分钟内"
root_causes:                      # 由 profile.root_causes 导出的判定最小集，供 verdict 计算
  - id: R1
    name: 1588 时钟源异常导致复位
    condition: "E1 and (E2 or E3) and not E6"
    temporal: [T1]                # 可选；缺省自动关联
    confidence: high
```

### 3.1 规则类型

- **text**（默认）：关键字/正则匹配，一行命中计一次。
- **numeric_trend**：从行中提取数值序列，按 `trend` 判定：
  - `exceed_threshold`：至少 `sustained_count` 个采样点 > `threshold`
  - `sustained_above`：连续 `sustained_count` 个点 > threshold
  - `monotonic_increase` / `monotonic_decrease`：序列单调

### 3.2 时区归一化

时间戳提取后统一转 UTC（带时区的直接转，naive 时间按 `--local-tz-offset`，默认 +8）。证据输出标注 `UTC`。时序比较在 UTC 上进行，避免 syslog UTC 与本地时间混用导致间隔算错。

### 3.3 关联键（associate_by）

多网元/多槽位场景下，从命中行提取网元号/槽位号，两种写法：

```yaml
associate_by:
  pattern: "(?:^|/)(\\d+_\\d+)(?:/|$)"   # 从行内或路径提取网元号
  group: 1
# 或多站点包直接按站点/网元根目录自动分组：
associate_by:
  site: true
```

时序约束比较时，**只比较 associate 值相同（或同站点）的证据对**，避免 A 网元告警配 B 网元复位的串证；证据每条输出 `site`（按特征目录推断的站点根）与 `associate` 字段。

判定：对每个 glob 命中的文件**流式逐行扫描**（大文件不爆内存，`.gz` 自动透明解压；`.bak.N` 等伪装后缀 gzip 按 magic bytes 识别，坏文件跳过）；一行同时满足 `all_of` 且满足 `any_of`（空则视为通过）且不命中 `none_of` 计为一次匹配；匹配数 ≥ `min_matches` 则规则命中。`.xlsx`（OOXML）按"行=记录"检索：每 sheet 首行作表头，`columns` 命中的列拼 ` | ` 参与匹配（表头措辞不符时全列兜底）；`format: csv`/`xlsx` 或直接 glob `**/*.xlsx` 均可。

> **`none_of` / `not` 的使用边界**：只用于"同一对象上的互斥特征"（如同一行标注了 Success）。**不同故障的现象可以在同一份日志中同时存在**（如一次拷机同时有电源类和相位类失败），共现不等于排除，这类情况应作为独立规则（如 E3）在报告中并列提示，而不是写进另一根因的排除条件。时序约束同样要基于真实流程设间隔（如失败项与最终总结论的间隔可能等于整个拷机时长）。

时序判定：`temporal` 逐条检查是否存在 first 证据时间 t1、second 证据时间 t2 满足 `min_gap ≤ t2−t1 ≤ max_gap`（且 associate 键相同，如指定）；输出 `temporal_satisfied` 与 `temporal_violated`。**根因成立除布尔条件外，相关 temporal 约束必须全部满足。**

### 3.4 层级容错与多站点

- **glob 兜底**：用户二次打包、目录层级变化或多站点前缀导致规则路径不匹配时，search 自动按 basename 递归匹配，命中兜底的文件记入 `glob_fallback`，必须人工确认（避免同名文件误匹配）。
- **多站点识别**：unpack 按特征目录/文件（当前告警/BSP/BspLog/OamLog/CloudMAT/PkiRoot/collectinfo/clockstatuslog 等）聚类站点根（首个特征段定根、全部特征计入，同一候选根 ≥2 个特征才判定），写入 manifest 的 `sites` 与 `file_site_map`；BspLog 根下嵌套 tar 解开的 `BSP/` 目录归属同站点，不另立站点。多站点包规则用 `associate_by: {site: true}` 防串证。
- **高容错解压**：嵌套 zip/tar/gz/7z/rar（后两者依赖系统 7z/unar/bsdtar，缺失时登记 `unsupported_archives`）；文件头 magic bytes 识别（后缀被改也能解）；zip 单个成员损坏跳过并告警，不中断整包；zip 中文文件名按 UTF-8→GBK 探测修复。
- **统一格式分类**：所有文件由 `log_format.classify_file` 归类为 archive/compressed_text/compressed_binary/compressed_archive/container_doc/text/ciphered/binary/empty（判据：名称权威层→magic→二进制后缀→内容层，**以内容为准**）；manifest 记 `file_kind_counts`/`opaque_files`，检索结果记 `skipped_files`（命中 glob 但未解析的明细）；`log_format.py <目录|zip>` 可单独盘点任意包格式分布。
- **不脱敏**：数据均不含敏感信息，证据原文、IP、用户名、证书文件一律原样保留；不提供脱敏开关。

### 3.5 公共规则库

`assets/common-rules.yaml` 收录所有故障复用的前置检查（采集完整性 C1、设备在线 C2、版本可识别 C3、活动告警存在 C4、复位历史存在 C5）。子 Skill 可在 profile 中通过 `include` 引用，避免重复编写。

证据输出（JSON）：

```json
{
  "rule_id": "E1", "step": "S1", "hit": true, "match_count": 2, "min_matches": 1,
  "encodings": ["gb18030"],
  "evidence": [
    {"file": "相对路径", "line_no": 128, "line": "原文整行",
     "timestamp": "2026-09-03 20:39:04 UTC", "site": "站点根相对路径",
     "associate": "1_1001", "context": ["...", "..."]}
  ]
}
```

### 3.6 verdict（自动根因判定）

evidence-rules 含 `root_causes` 段时，search_evidence.py 在 evidence.json 顶层输出 `verdict`：

```json
{
  "primary": "R1", "status": "matched", "grouped_by_site": false, "truncated": [],
  "groups": {
    "ALL": {
      "hit_rules": ["E1", "E2", "E4"],
      "root_causes": [
        {"id": "R1", "condition": "E1 and E2 and E4", "substituted": "True and True and True",
         "value": true, "temporal_required": ["T1", "T2"], "temporal_ok": {"T1": true, "T2": true},
         "established": true, "confidence": "high"}
      ],
      "primary": "R1", "others": [], "temporal_conflict": [], "status": "matched"
    }
  }
}
```

- condition 用 `ast` 白名单安全求值，规则 ID 整词替换（`E1` 不会污染 `E10`）；非法表达式记 `error`，视为不成立。
- `established = value && 所有 temporal_required 满足`；`value` 为真但时序不满足时 `note` 提示且进入 `temporal_conflict`。
- `status`：`matched`（至少一个成立）/ `temporal_conflict`（仅因时序未判定）/ `unmatched`。
- ≥2 个站点时 `groups` 按站点各算一份（组内规则命中 = 组内证据数 ≥ `min_matches`；时序只认同站点配对），`ALL` 为汇总；`truncated` 列出证据达 `--max-evidence-per-rule` 上限的规则（分站点计数可能偏低）。
- run_regression 的根因断言与子 Skill 报告均直接取 `verdict`，两处判定逻辑同源。

正则使用 Python `re` 语法；写规则时优先用字面量字符串，含特殊字符（括号、方括号）必须转义。路径 glob 用 `**` 跨目录匹配，且必须同时兼容三类包的可能位置（给多个 glob）。时间格式支持 `-`/`/`/`.` 分隔、毫秒、`T...Z`（RFC5424）、`%Y%m%d%H%M%S`、epoch 等；新时间格式在脚本 `TIME_FORMATS` 中登记。

### 3.7 共享告警码字典

`assets/alarm-code-dictionary.yaml` 由生成器 `assets/shared/` 统一分发。基础字段：`{code/codes, name, applicable.boards, default_severity, reason_codes, extra_params, detection, detect_cycle, debounce, thresholds, masking, related_alarms, related_files, related_faults, handling, source, verified}`。

- 只收录权威来源（告警手册/公司文档/用户确认）条目；`verified: false` 不得作为结论唯一依据。
- 检索到告警码时用字典解释含义、原因码、检测周期/防抖、门限、关联告警、关联文件与处理措施；未收录条目在报告标注"字典待补充"。
- `reason_codes` 是随告警附加参数上报的子码（如 198096841 原因码 1-5）；多原因码告警必须先按 `masking` 屏蔽关系收敛根因（如"报文接收异常"屏蔽"协商异常/时钟质量差/服务器时间异常/PDV"），被屏蔽的原因码不得作为最终结论。
- `thresholds` 为机读门限（如压控电压 0x500/0xFB00、SyncE 降质 400ppb、1PPS 降质 15us/100ppb、锁星 ≥6 颗 cno>35dB 且锁定<4 持续 120s），与 `clk-field-dictionary.yaml` 的 `alarm_field_mapping` 配合定位日志字段。
- 主控时钟告警域（198092217/198096802/198096830/198096831/198096833/198096834/198096835/198096836/198096837/198096840/198097004/198098315）已按告警手册收录 12 条；198096840 手册检测周期为占位，引用时注明缺口。

### 3.8 CloudMAT 结构化解析

`parse_cloudmat.py` 对工装日志输出 JSON：`basic_info`（tfp/product/barcode/slot/run_mode/result/duration_seconds/ftp_upload）、`item_list`/`item_count`、`failed_items[]`（name、fail_rounds、首次/末次/连续起始轮次、reasons 含通过标准行）、`bitmaps[]`（位图与 itemList 对位的 failed_items）、`cycling_table`、`warnings`。

## 4. 判定表达式与根因逻辑

`root_causes[].condition` 使用 `and/or/not` 与规则 ID、括号组成的布尔表达式，例如：

```
E1 and (E2 or E3) and not E6
```

- search_evidence.py 跑完全部规则后自动计算 `verdict`（见 3.6）：各根因 condition 代入 + 关联 temporal 约束。子 Skill 以 verdict 为判定基准，对照决策链核对，不得手改结论。
- 根因成立 = condition 为真 **且** 与其相关的 temporal 约束（显式 `temporal[]` 或自动关联）全部满足；时序违反时不能直接判定，报告中列出矛盾证据。
- 多网元/多站点包按 `verdict.groups` 分站点计算（配合 `associate_by: {site: true}`），禁止跨网元拼证据。
- 多个根因同时满足时：按 profile 中 `root_causes` 顺序取**第一个**，其余在报告"其他可能"中列出；若需要更强消歧，必须在访谈中补充分支规则。
- 无根因满足：报告结论为"未匹配到本故障的已知根因"，输出已排除项与建议人工排查方向，**禁止强行归因**。
- 反例案例（`expected_root_cause: NONE`）要求所有根因 condition 均为 false（或时序约束不满足）。
- **命中口径一致性**：被 root_causes 引用的规则，命中语义必须与真实判定一致——"宽松/候选"型规则（把正常值也圈进命中范围）应收窄 `all_of/any_of` 或改由专项脚本复核（`--specialized`），否则 verdict 会与专项结论冲突；scaffold 校验会对描述含"候选/宽松/筛查/疑似"的规则给 warning。

## 5. 报告模板规范

模板：`assets/report_template.md` / `.html`（**模板即契约**，3 节结构与字段由模板固化）。只需守住以下不变量：

1. **三节顺序固定**：`## 1 结果统计`（总网元/正常/故障数 + 各站点一句话汇总表，网元名为锚点）→ `## 2 网元详情`（每网元 `2.N.1 故障根因` / `2.N.2 处理建议` / `2.N.3 故障证据`，证据按文件分组、四列 `规则｜行号｜时间戳（UTC）｜原文`）→ `## 3 故障现象与甄别`。
2. **面向非专业用户**：不写执行过程、覆盖明细、局限附录；根因机理取 profile 的 `mechanism`，未登记不推测。
3. **证据不可篡改**：证据行带 `<!-- ev:EV4:HASH -->` 指纹，路径/行号/原文/时间戳不得手改；MD 为事实源，HTML 由 MD 渲染且内容一致。
4. **交付门禁**：`render_report.py --verify` 退出码为 0（TODO 清零、指纹未改且来源于本 evidence、结论与 verdict 或 `--specialized` 一致、MD/HTML 一致）；非 0 不得交付。
5. **冲突处理**：verdict 与专项脚本冲突以专项为准，写"候选根因（未归因）"；`temporal_conflict` 不归因；多站点不跨网元拼证据。

## 6. 子 Skill 的固定工作流

由 `SKILL.md.tmpl` 渲染，不手写。核心约束：只跑一次 `run_analysis.py`（内部完成解压→检索→摘要→判定→报告→门禁）；只读 `run-summary.json` 与 `evidence.brief.md`，不整读大 JSON；以 verdict 为基准，不手改结论；规则有误回 builder 修 profile 并全量回归。

## 7. 扩展指南

- **新增根因分支**：在 profile 的 `analysis.steps` 增加规则（E 编号递增）与 decision 分支，在 `root_causes` 增加 R 项，并补一个能验证该分支的案例；重新脚手架后回归全部案例。
- **新增设备形态/采集方式**：在 `fault.domain` 增补枚举，为每条规则补 glob（参照生成器的 `references/platform/log-package-map.md`），并提供该形态的案例。
- **新增文件格式**：先在 `log_format.py` 的分类学中登记（magic/后缀/子类型/路由），unpack/search 自动跟随；若需新的解析行为再改对应脚本。脚本单一源在 builder `scripts/`，生成/更新时自动复制（无副本需同步），递增 `scaffold_child_skill.py` 的 `TOOLKIT_VERSION`，跑 `tests/selftest.py`；各存量子 Skill 用 `--inspect` 批量体检（stale/local-modified/missing/new 一目了然）再 `--update` 同步。
- **新增报告格式**：在 assets 增加模板并在子 Skill 的 SKILL.md 说明引用方式；MD 始终是事实源。
- **多语言/多站点**：关键字按编码分别给出（UTF-8/GBK），中文告警同时给告警码这一 ASCII 锚点。
- **从规则库导入（新增）**：当已有 `规则文件/` 下的黑匣子/蜂鸟规则库时，用生成器 `scripts/import_rule_library.py` 直接解析生成**候选 fault-profile**（`mode: triage`、规则 `verified: false`、`root_causes` 空、`analysis.steps` 只做证据检索），避免重复访谈录入。
  - **黑匣子映射**：`关键字 → any_of`（OR；re.compile 失败 re.escape；验证后可收窄为 all_of）、`日志路径和文件名 → globs`（`**/<basename>*`）、`故障呈现 → 分组键/描述`；检查规则/维修建议/拦截列按列名宽松取列（缺省置空）。
  - **蜂鸟映射**：描述性 `详细信息` 走「锚点抽取」作 any_of（日志文件名→【】/引号/括号标识符→领域词表）；仅「规则」sheet 有效。
  - **候选集语义**：只做证据检索，**不参与自动 verdict**（`mode: triage` 下 `scaffold --check` 豁免 root_causes/steps 非空，但不可交付为可判定 skill；候选 skill 的检索/报告为「分诊线索」口径，router 默认跳过）。
  - **验证通过后**：去掉 `mode: triage`、规则改 `verified: true`（或删除该字段）、补根因 condition 与案例，再走验证流程（规则级验证 → 端到端盲跑）。
  - **规则库更新**：用 `scripts/rule_library_diff.py` 对比新旧规则库/候选 profile，输出新增/删除/变更清单，供子 Skill `--update` 增量参考。
  - 两个脚本是 **builder 侧工具**，不分发给子 Skill。
- **多站点时钟合并（新增）**：`parse_clock.py --merge-sites` 合并多个站点时钟 JSON（`files[]` 拼接、`gnss.spoofing_suspicions` 列表拼接、`gnss_records` 求和）；`render_report.py --clock` 传**目录**（内含多站点 `*clock*`/`*brief*` JSON）时自动合并。**`gnss.spoofing_suspicions` 必须是 list**（每项 `{timestamp, sat_type, consistent_sats, confidence}`），禁止写成 int（脚本对 int 做兜底归一，但以 list 为准）；`gnss_records` 为 int 求和。
- **禁止**在子 Skill 里硬编码任何未在 profile 中登记的根因与处理动作；专家经验一律先入 profile、先过案例验证。
