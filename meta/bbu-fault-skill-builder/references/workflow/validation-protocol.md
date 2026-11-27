# 验证协议（Validation Protocol）

> 规则没在真实案例上跑过，就不算 Skill。本协议分两段：规则级验证（生成前）、端到端盲跑（生成后）。

## 一、规则级验证（生成前）

### 步骤

1. **入库案例**：让用户提供案例 zip/文本，登记案例清单（ID、类型、设备、采集时间、已知根因、期望证据）。正例至少 1 个；争取反例与边界案例。
2. **解压**：
   ```bash
   python3 scripts/unpack_logs.py <案例包> -o <workdir>/<case_id> [--max-bytes 5000000000] [--max-depth 10]
   ```
   检查 manifest：文件数、总大小、未解压的二进制/失败项；若用户明确说应存在的目录没解出来，先修脚本或记录采集失败。
3. **逐条规则回放**：先 `scaffold_child_skill.py --profile <profile> --check` 过静态校验；再把 profile 脚手架到临时目录（或直接用其导出的 `evidence-rules.yaml`），运行：
   ```bash
   python3 scripts/search_evidence.py <workdir>/<case_id> --rules <evidence-rules.yaml> --json <case_id>_evidence.json
   ```
   控制台末行 `verdict: <status> primary=<R?>` 即自动判定结果，应与案例已知根因一致。
4. **填《规则验证表》**并与用户复核：

| 规则 ID | 规则含义 | 案例 ID | 期望 | 实际 | 结论 |
|---|---|---|---|---|---|
| E1 | 例：活动告警含 1588 异常 | C1 | 命中 | 命中 2 行（fm-active，行 128/341） | 通过 |
| E2 | 例：时钟参考源不可用 | C1 | 命中 | 未命中 | 不通过→修订 |

5. **判定与修订循环**：
   - 假阴性（该命中没命中）：核对路径 glob 是否覆盖该包形态、关键字是否与原文一致（空格/大小写/编码）、时间窗是否需要放宽；与用户确认后修订。
   - 假阳性（反例命中）：收紧 `all_of`/`columns`/增加 `none_of`；与用户确认区分特征。
   - 阈值类规则：在案例上核对实际值分布，阈值必须由用户确认来源。
6. **冻结规则**：全部规则在正例通过、反例不通过后，请用户确认冻结；未验证规则允许保留但必须标 `verified: false`，且不得被任何根因 condition 引用。

### 规则级通过标准

- 每个被根因 condition 引用的规则，在其对应正例上全部命中（允许证据条数 ≥ min_matches）；
- 反例上所有根因 condition 均为 false；
- 每条证据都能给出文件、行号、原文；
- 二进制依赖项已确认人工解析方式或在报告中显式标注。

## 二、端到端盲跑（生成后）

### 方法

1. 用 `build_child_skill.py --cases-dir <案例库根>` 一键生成并回归后，**扮演对结论一无所知的使用者**：只提供“故障描述原文 + 案例 zip”，严格按子 Skill 的 `SKILL.md` 执行一次 `run_analysis.py`；不得手工拆步或代替规则判定。
2. 结论必须与 `verdict` 一致，不一致时修 profile 并重新一键构建，不手改报告。`run_analysis.py` 已自动执行 `--final` 和 `--verify`；`ok: true` 才表示报告可交付。
3. 最好换一个未参与规则编写的案例（同根因的新案例）做盲跑；若只有一个案例，至少在全新输出目录完整跑一遍，并对同目录再跑一次确认幂等。

### 核对清单

| 检查项 | 通过标准 |
|---|---|
| 输入处理 | 无 zip 时正确降级为可能性分析；有 zip 时正常解压 |
| 解压完整性 | manifest 条目数与实际一致；嵌套压缩已递归解开；二进制与失败项已标注 |
| 范围预检 | 设备形态/采集方式识别正确；与 profile 适用范围不符时有提示 |
| 证据完整性 | 每个判定为真的规则至少 1 条证据，含文件路径/行号/原文/上下文 |
| 结论正确性 | `verdict.primary/status` 与案例已知结论一致；报告结论与 verdict 一致；置信度标注合理 |
| 反例排除 | 反例案例 `verdict.status == unmatched`，报告输出"未匹配本故障根因"并给出排除依据 |
| 处理方案 | 与根因一一对应，含动作/前提/风险/回退/验证/升级路径 |
| 最终报告 | 由一键入口以 `--final` 生成，无 TODO、无脚本运行过程；只呈现故障原因、证明依据和解决办法，路径/行号/原文未被手改 |
| 机器门禁 | `render_report.py --verify report.md --evidence evidence.json --html report.html` 退出码 0（TODO 清零、指纹行未改、结论与 verdict 或 `--specialized` 一致、MD/HTML 一致） |
| 专项脚本 | verdict 与专项脚本判定不一致时：报告用 `--specialized` 复核结论 + "候选根因（未归因）"写法，`--verify` 仍通过；长期应收窄规则使命中口径与专项判定一致 |
| 报告一致性 | MD 与 HTML 内容一致；证据可溯源；未验证项如实列出。仅人工定制草稿时才在补完 TODO 后用 `--from-md` 重渲 |
| 可复现性 | 同样输入重复跑结果一致（evidence.json 可复现） |

### 回归要求

- 任何一次规则修订后，重新脚手架，并对**全部已入库案例**重新跑规则级验证 + 盲跑（防止修一条规则破坏旧案例）。
- 记录回归结果到案例清单，随子 Skill 交付时附《验证表》。
- 交付前 `scaffold_child_skill.py --inspect <子skill>` 确认与 builder 同步（无 stale/missing）；`validate_skill_md.py <子skill>` 确认 frontmatter 符合 Agent Skills 规范；builder 升级后存量子 Skill 一律先 inspect 再 `--update`。

## 三、交付物清单（随子 Skill 一并交付）

1. 子 Skill 目录（路径）；
2. 已冻结的 `fault-profile.yaml`（案例展示索引由案例库自动重建）；
3. 《规则验证表》（逐规则 × 逐案例）；
4. 盲跑产出的 MD/HTML 报告样例（每类案例至少一份）；
5. 局限清单：未覆盖机型/版本、未验证规则、二进制人工解析项、采集失败已知情况。

## 四、案例库（标准化、与 skill 解耦）

案例**不包含在任何 skill 目录内**，也不随 skill 打包/分发；统一放在 skill 之外的独立案例库根目录，由 `--cases-dir` 显式指定位置（不写死绝对路径）。每个 case 是一个**独立、自描述、可复现**的文件夹。

### 4.1 目录结构

```
<cases-root>/                       # 案例库根（skill 外，--cases-dir 指向）
└── <skill-slug>/                   # 按子 skill 分目录（如 bbu-fault-gnss-clock）
    └── <case-id>/
        ├── case.yaml               # 元数据 + 结构化预期（断言唯一来源）
        ├── input/                  # 输入：归档 zip/tar/gz/bz2；或解压后的目录树
        └── expected/               # 可选：期望报告片段等人工附件（不参与自动断言）
```

- 初始化骨架：`python3 scripts/init_case.py --cases-dir <root> --skill <slug> --id <case-id> --kind positive [--clock]`
- 案例 id 用 `case-序号-短描述`（kebab-case），如 `case-01-ant-open`、`case-05-dual-site`。

### 4.2 case.yaml 字段

| 字段 | 说明 |
|---|---|
| `id` / `skill` / `description` / `device` / `tags` | 案例标识；`skill` 必须等于目录名对应的子 skill slug |
| `kind` | `positive` 正例 / `negative` 反例 / `boundary` 边界 / `corruption` 损坏容错 / `multisite` 多站点 |
| `input.archive` | 相对 case 目录的归档路径；或 `input.unpacked_dir` 指向已解压目录树 |
| `pipeline` | 执行阶段，默认 `[unpack, search]`；时钟类追加 `parse_clock`（检测到 `parse_clock` 预期会自动追加） |
| `expected.root_cause` | 期望根因 id；反例无结论用 `"NONE"` |
| `expected.hit_rules` / `not_hit_rules` | 必须命中 / 必须不命中的规则 id |
| `expected.temporal_satisfied` / `temporal_violated` | 时序约束的期望结果 |
| `expected.match_counts` | 规则命中次数，支持 `>=N` / `==N` / `<=N`（如 `E1: ">=1"`） |
| `expected.evidence_contains` | 至少一条证据同时满足：`rule` + `all_of`（原文必含子串）+ `file_suffix`/`file_basename` |
| `expected.manifest` | 解压清单断言：`sites`（站点数）、`warnings`、`unsupported`、`corrupt`（损坏成员数） |
| `expected.parse_clock` | 时钟断言：`alarms`（type/action/ref）、`reset_reasons`（reason_id）、`health_bits`（位名）、`anomaly_contains`、`spoofing_suspicions`、`timestamp_fixed`、`total_records`/`ocxo_count`/`gnss_records` |

### 4.3 输出稳定性铁律（必须遵守）

回归结果必须**跨机器、跨目录层级、跨重复运行完全一致**：

1. **不比对绝对路径**：证据文件只用 `file_suffix`（路径后缀，如 `clockstatuslog.01`）或 `file_basename`，绝不写完整路径。
2. **不比对行号**：包内前缀层级变化会改变行号，行号只供人工查阅，不进断言。
3. **不整行比对**：用 `all_of` 关键字子串与 `match_counts` 次数表达，避免字段顺序/空格波动。
4. **每次全新工作目录**：回归在临时 `--workdir` 内按 case 重建，结果文件落盘后逐字段比对；记录输入 `input_hash`（归档 sha256 摘要），输入被改动可察觉。
5. **时间断言**：合成案例时间写死（如 `2026-09-10`）；时序只判"先后/满足与否"，不依赖当前系统时间。
6. 期望只写"该 case 必须成立/必须不成立"的项，不确定的项不要写进断言（宁缺毋滥）。

### 4.4 运行回归

```bash
# 回归某 skill 全部案例（自动 rglob 发现 case.yaml）
python3 scripts/run_regression.py --cases-dir <cases-root> --skill <slug> \
    --skills-dir <skills安装根> --json report.json
# 回归单个 case
python3 scripts/run_regression.py --case <case-id 目录> --skills-dir <skills安装根>
```

退出码非 0 即存在 FAIL/ERROR、覆盖率缺失或端到端失败；每条失败打印"期望 vs 实际"。`expected.root_cause` 直接与 `verdict.primary` 比对（与子 Skill 判定同源）。任何规则修订后必须对**全部案例**重新回归。

- **案例单源**：`case.yaml` 是唯一断言来源；构建时自动重建 profile 的案例展示索引，旧 profile 同 ID 登记与 `case.yaml` 冲突会拒绝构建。
- **覆盖率门禁**：全量 `--cases-dir --skill` 自动检查每个根因至少一个非负例案例，且 condition 规则均被该根因案例的正/反断言覆盖。
- **端到端门禁**：构建自动使用 `--e2e auto`，每个根因选一例，并覆盖反例、多站点和 `conflict` 标签案例，执行 `run_analysis.py`、检查 MD/HTML 和 `--verify`；手工验证可用 `--e2e all` 跑全部案例。
- **失败保护**：构建在临时目录完成上述验证后才替换正式目录，失败日志默认保留在 `<skills根>/.bbu-build-logs/<skill>`。

生成器自身脚本（unpack/search/scaffold/regression/render_report）改动后，先跑 `python3 tests/selftest.py`（合成案例全链路），再回归真实案例库。

### 4.5 数据与打包边界

- 案例数据**不复制进**子 skill 目录、不进入交付 zip；skill 只含规则、脚本与字典。案例库随验证环境单独保存与传递（移植公司平台时由 `--cases-dir` 重新指向）。
- 数据均不含敏感信息，证据原文与报告样例不做脱敏；如后续引入真实含敏感字段的案例，先与用户确认是否可入库。

## 五、打包形态与多站点专项验证

每个子 Skill 至少用以下三种形态的合成/真实案例各验证一次：

| 形态 | 构造方式 | 验收点 |
|---|---|---|
| 标准包 | 原始目录直接 zip | 规则全命中、时序正确 |
| 二次打包 | 标准包再套一层 zip（或 tar/7z/rar），可改后缀 | 递归解开，结果与标准包一致 |
| 层级变化 | 包内加任意层前缀目录（如 `客户A/2026年10月/站点X/原始结构`） | glob basename 兜底命中，`glob_fallback` 已人工确认 |
| 多站点 | 一个包内含两个站点目录，各自一套 BBU 结构 | manifest `sites` 识别 ≥2；`associate_by: {site: true}` 下时序不跨站点配对 |
| 伪装后缀压缩 | tar/zip 内 `.bak.N`、无 `.gz` 后缀但为 gzip 内容 | 解包生成 `*.gunzipped` 副本且文本可检索；manifest 无 unsupported |
| 任意后缀压缩文本 | `.dat.gz`、`.N.gz` 等非 `.log/.txt` 命名的 gzip/bz2 文本 | 按内容路由 `compressed_text`，逐行检索命中 |
| 二进制/密文登记 | `.bin/.pcap/BspMoni/uartlog`、`.txt` 实为二进制结构体 | `log_format.py` 盘点归类 opaque；manifest `opaque_files` 有明细；命中 glob 时记 `skipped_files` |
| DCM 多槽位时钟 | `<子网>_<网元>/BspLog/` 下多槽位 `*PLAT.EXE_*clockstatuslog*` + BSP tar 内时钟文件 | `parse_clock --merge` 按 `clockstatuslog@<槽位>` 分组不混流 |
| fm xlsx 导出 | zip 内单个中文名 `.xlsx` | xlsx 整文件提取不展开 XML；`columns` 列匹配命中告警行 |

- 容错验证：人为损坏 zip 中一个成员（截断/翻转字节），整包其余成员仍提取成功，损坏项登记 `skipped`+`warnings`。
- zip 成员名为中文时，提取结果不得乱码。
