# 工具箱参考（Toolkit Reference）

> 生成器 `scripts/`（脚本唯一源，生成时复制到子 Skill）中各脚本的参数、容错特性与输出字段。
> 全部脚本零三方依赖，**Python ≥ 3.11**；规则/配置统一 `.yaml`（需 PyYAML；缺 PyYAML 时所有脚本给出明确提示）。命令示例写 `python3`，**Windows 下替换为 `python`**（或 `py -3`）；路径均相对 SKILL.md 所在目录。所有脚本在 Windows GBK 控制台/管道下自动切换 UTF-8 输出，不会因中文/符号崩溃。

## 一键入口（优先使用）

```bash
# builder：一键分发闭环——校验、生成、规范检查、全案例回归、打包、同步安装位、同步子 skill
python3 scripts/publish.py --profile <fault-profile.yaml> --path <skills根> --cases-dir <案例库根> \
    [--zip-out <目录>] [--changelog <仓库根 CHANGELOG.md>] [--install-dir <安装位>] \
    [--sync-skills <子skill目录> ...] [--dry-run] [--json <result.json>]

# builder：仅构建（校验、生成、规范检查、全案例回归；失败即停）
python3 scripts/build_child_skill.py --profile <fault-profile.yaml> --path <skills根> --cases-dir <案例库根> --json <build-result.json>

# 子 Skill：定向解压、检索、专项摘要、最终 MD/HTML、门禁；只输出小摘要
python3 scripts/run_analysis.py <日志包|目录|单日志> --description "<故障描述原文>" [--out <输出目录>]
```

- `--out` 可省略：默认输出到**首个输入的同级目录** `<输入名>.analysis`（包/日志/目录的旁边），绝不落在 skill 目录内；多文件输入按第一个文件命名。显式 `--out` 与默认行为等价，均带 `.bbu-run-analysis-owned` 防误删标记。

三者均用参数数组调用当前 Python，不拼接 shell；详细输出写入阶段日志。成功后只读构建摘要或 `run-summary.json`，失败先看摘要中的 `logs` 路径。`run_analysis.py` 的早期输入/资源错误也返回结构化失败摘要，显式 `--out` 时写入该目录；它只删除带 `.bbu-run-analysis-owned` 标记的旧工作目录，纯单日志子技能无 unpack 组件时接受一个非压缩日志。

**事务化构建**：`build_child_skill.py` 在 `<path>` 同文件系统的临时目录生成并验证，`--update` 先把旧版本复制到临时目录；全案例判定、覆盖率和关键案例端到端报告全部通过后才切换正式目录，失败不覆盖旧版本。`--log-dir` 保留完整日志；未指定时失败也会保存到 `<path>/.bbu-build-logs/<skill>`。发布失败日志对应保存到 `.bbu-publish-logs/<skill>`。

**run_analysis 增量缓存**：unpack/search/clock/cloudmat/precheck 阶段产物按「输入（文件内容哈希 / 目录弱签名）+ evidence-rules/profile + 相关脚本 + `--local-tz-offset`」计算缓存键，存于 `<out>/.cache/<sig>/`。输入/规则/脚本未变化时二次执行直接复用产物（`run-summary.json` 中 `cached: true`），跳过 unpack/search 重跑；`--no-cache` 强制全量。`--local-tz-offset` 默认 +8（东八区），跨时区采集请按日志实际时区显式传入。

**run_analysis 采集完整性预检**：存在 `assets/common-rules.yaml` 时默认自动执行 common-rules 预检并透传 `--precheck` 给报告；`--no-precheck` 关闭。

**publish 分发闭环**：`--dry-run` 只预览动作；`--changelog` 把仓库根 CHANGELOG 附入 zip（`package_skill.py --extra`，不落盘技能目录）；`--install-dir` 先把子 skill 完整复制到同文件系统临时目录，再原子切换安装位，切换失败自动恢复旧版本（纯 Python，Windows 可用）；`--sync-skills` 对既有子 skill 逐个跑 `skill_sync.py --sync` 同步 builder 基础能力。

## 目录

1. log_format.py — 通用格式分类学与盘点（共享模块）
2. unpack_logs.py — 递归解压 + 故障导向提取
3. search_evidence.py — 证据检索 + 时序 + verdict（`--brief` 精简摘要）
4. render_report.py / run_analysis.py — 报告渲染与交付门禁；子 Skill 一键入口（解压→检索→摘要→判定→报告→门禁）
5. parse_clock.py — 时钟日志结构化
6. parse_cloudmat.py — CloudMAT 工装日志结构化
7. scaffold_child_skill.py / build_child_skill.py / publish.py — 脚手架 / 构建+回归 / 打包分发
8. scan_rule_candidates.py — 规则候选扫描
9. init_case.py / run_regression.py — 案例库与回归
10. tests/selftest.py — 生成器自测
11. check_env.py / validate_skill_md.py / package_skill.py — 环境自检 / 规范校验 / 分发打包
12. import_rule_library.py / rule_library_diff.py — 规则库导入 / 差异对比（builder 侧）
13. skill_sync.py — 元 skill 与子 skill 基础能力双向同步 / 回归回收

---

## 1. log_format.py（通用格式分类学与盘点）

```bash
python3 scripts/log_format.py <已解压目录|zip包|单文件> [--json inv.json] [--sample 5] [--quiet]
```

统一格式分类学（`classify_file`/`classify_bytes`，零依赖），为 `unpack_logs`/`search_evidence`/`scan_rule_candidates` 的公共路由模块（子 Skill 随 unpack/search 自动复制）：

| kind | 判定依据 | 处理路由 |
|---|---|---|
| `empty` | 0 字节 | 登记忽略 |
| `archive` | 后缀或 magic（zip/tar/7z/rar） | unpack 递归解包 |
| `compressed_archive` | gzip/bz2/xz 内层是 tar/zip | 解压后继续递归 |
| `compressed_text` | gzip/bz2/xz 内层可解码为文本 | 透明解压后逐行检索 |
| `compressed_binary` | gzip/bz2/xz 内层是二进制；zstd（非 stdlib） | 登记 opaque |
| `container_doc` | `.xlsx` 等 OOXML 后缀 | xlsx 系按行检索；其余登记不解析 |
| `text` | 编码链可解（utf-8-sig→gb18030→big5→latin-1），无 NUL | 逐行检索；subtype: log/table/text |
| `ciphered` | 名称命中 bspmoni/verprotect/gnssoriginalmsg/uartlog | 登记 opaque，需专用工具 |
| `binary` | 其余不可读 | 登记 opaque（subtype: pcap/executable/database/ole2/unknown） |

- 判据顺序：名称权威层（密文/容器/归档后缀）→ magic bytes → 已知二进制后缀 → 内容层（NUL 判定 + 编码链）。后缀与内容冲突时以内容为准（如 `.txt` 实为二进制结构体、`foo.dat.gz` 实为文本）。
- CLI 输出按 kind 计数 + 每类样本路径；`--json` 明细含 path/kind/subtype/encoding/compression/note；`unhandled` 汇总 opaque 清单。传 zip 时不解包直接按成员头部字节分类。
- 用法：陌生包先盘点再写 glob；规则未命中时核对"文件存在但被归类 binary/ciphered"（skipped_files/Opaque 明细）。

## 2. unpack_logs.py

```bash
python3 scripts/unpack_logs.py <包> --list-only                       # 只列顶层清单
python3 scripts/unpack_logs.py <包> -o <workdir> --include-rules assets/evidence-rules.yaml \
    [--include-glob "**/collectinfo.log"] [--max-bytes N] [--max-depth N] [--copy-source]
```

- 递归 zip/tar/gz/bz2/xz/7z/rar（`.xz` 走 stdlib lzma；zstd 与 7z/rar 依赖外部工具，缺失时登记 `unsupported_archives`）。所有磁盘 IO 经 `long_path()` 防 Windows MAX_PATH=260 截断。
- 文件头 magic bytes 识别（后缀被改也能解）：全量/选择性模式下每个成员都嗅探文件头，`.bak.N` 等伪装后缀的 gzip 也能解压，产物保留原名加 `.gunzipped` 后缀；`.xlsx`/`.docx` 等 OOXML 容器文档**只提取不递归**（不展开内部 XML）。zip 单个成员损坏跳过并写 `warnings`，不中断整包；zip 中文文件名按 UTF-8→GBK 顺序探测修复（兼容"UTF-8 字节无标志位"导出包）；路径穿越防护。
- 输入不是归档（纯文本日志/`.xlsx` 直接传入）时按普通文件登记 `extracted_files`，不报 unsupported。
- `--include-rules`：只提取规则 glob 涉及的文件（归档仍递归解开），超大包不全量解压；未提取项登记 `not_matched_not_extracted`；glob 层级兜底（basename 递归）。**未提供 `--include-rules/--include-glob` 时 stderr 会提示按需提取**（全量解压通常只用到少数文件）。
- 多站点识别：按特征目录（当前告警/历史告警/BSP/OamLog/CloudMAT/PkiRoot …）聚类站点根（同一候选根 ≥2 个特征才判定；特征名归一去轮转/压缩尾缀，`clockstatuslog.txt.01` 与 `.txt` 只算 1 个特征，防止轮转副本被误判为多站点），写入 `sites` 与 `file_site_map`。
- **默认不复制源包**：源包在 workdir 外时只做读取、解压产物全部落在 workdir 内（GB 级包不再磁盘翻倍）；`--copy-source` 恢复旧行为（workdir 自包含源包副本）。多网元包一次解压到位，`sites` 已按网元聚类，不要按网元目录二次解压；定向某网元用 `--include-glob "**/<网元>/**"`。
- manifest.json 关键字段：`mode`、`extracted_files`、`not_matched_not_extracted`、`sites`、`file_site_map`、`binary_or_opaque_files`（兼容字段，文件名列表）、`opaque_files`（log_format 明细：kind/subtype/note）、`file_kind_counts`（格式分布）、`unsupported_archives`、`skipped`、`warnings`。
- **manifest.summary.json**：同步旁路写出的小摘要（mode/sites/warnings/kind_counts/各计数/manifest 路径），LLM 先读它；完整 manifest.json 可达数十万行，不要整体 Read。
- 不脱敏：证书/密钥等文件正常提取。

## 3. search_evidence.py

```bash
python3 scripts/search_evidence.py <root> --rules assets/evidence-rules.yaml --json evidence.json \
    [--profile assets/fault-profile.yaml]  # 直取 profile 内嵌规则（与 --rules 二选一）
    [--progress] [--max-evidence-per-rule 50] [--local-tz-offset 8] [--brief evidence.brief.md]
```

- `--brief <md>`：另写一份面向人/LLM 阅读的精简摘要（verdict 与各根因代入、逐规则 HIT/MISS/ERR + 匹配数 + 首条证据 `文件:行号 时间戳` + 原文片段、时序表、兜底/截断提示）。子 Skill 流程优先读它，需要上下文时再按规则 ID 查 evidence.json，避免一次性读取大 JSON。

- 流式逐行扫描；文件路由统一由 `log_format.classify_file` 判定：text/compressed_text 才扫描（gzip/bz2/xz 均透明解压，**任意后缀**的压缩文本都按内容识别，不再限 `.log.gz` 等名），binary/ciphered/archive 等跳过并记入结果 `skipped_files`（含 kind/subtype/note）；编码链 UTF-8/GB18030/Big5 自动识别；CSV 按 `columns` 匹配；`.xlsx`（OOXML，如 fm-active/fm-history 网管导出）按"行=记录"检索，`columns` 匹配表头列（每 sheet 首行作表头，措辞不符时全列兜底），`format: csv`/`xlsx` 或直接 glob `**/*.xlsx` 均可；密文/私有格式（BspMoni/verprotect/gnssoriginalmsg/uartlog）按名字跳过不扫。
- 规则类型 `text`（all_of/any_of/none_of/min_matches/context_lines）与 `numeric_trend`（value_pattern/trend/threshold/sustained_count/window_minutes）。
- 时间戳统一转 UTC（naive 按 `--local-tz-offset`）；未配 `time_extract` 时自动嗅探常见格式，含时钟日志逗号分隔 `YYYY-MM-DD,HH:MM:SS`（`TIME_FORMATS`/`TIME_LOOKS` 已覆盖）；规则 `time_extract.formats` 显式格式列表**优先于自动嗅探**逐条尝试（规避默认嗅探盲区，时钟类规则建议声明）。
- `associate_by`：`{pattern, group}` 从行/路径提关联键；`{site: true}` 按站点根自动分组。时序比较只配对同键证据。
- glob 层级兜底：某模式完整路径未命中时才按 basename 递归，命中记入 `glob_fallback_files`（需人工确认）；`.gunzipped` 同源副本与原件同时命中时去重（证据指向原件）。
- 全规则共享一次文件树索引（内存 glob 匹配，不再按规则重复遍历）；逐行流式扫描（`LineWin` 窗口提供 context_lines 上下文，GB 级文件不爆内存）。
- 文件级合并扫描（`search_all`）：每文件单遍评估所有命中规则——同一文件被多条规则 glob 命中时只解压+解码一次；xlsx/csv/行三种读取模式每文件各最多一遍；每规则独立 state，证据收集**按站点独立配额**（每网元保留 `--max-evidence-per-rule` 条代表证据，先扫描站点不会占满全局上限，保证多站点都有可溯证据）达配额后停止取上下文但**继续累计真实命中数**。
- 行级优化：`classify_file` 进程级缓存、站点根按文件一次计算、纯字面量 pattern 走子串匹配（不敏感时一次 lower）、te/ab/value 正则每规则预编译、时间格式记忆上次命中。
- `temporal`：逐条检查 first→second 的 `min/max_gap_minutes`；输出 `temporal_satisfied`/`temporal_violated` 与配对明细 `pairs`。
- **verdict**（evidence-rules 含 `root_causes` 段时自动输出）：
  - condition 以 `ast` 白名单安全求值（只允许规则 ID/and/or/not/括号），规则 ID 整词替换，`E1` 不会污染 `E10`。
  - 关联时序：`root_causes[].temporal` 显式声明优先；否则取 first/second 都以**肯定形式**出现在 condition 中的约束（`not E4` 不参与）。根因成立 = condition 为真 **且** 关联时序全部满足。
  - ≥2 个站点时 `groups` 按站点各算一份（组内规则命中 = 组内真实命中数 ≥ `min_matches`，取 `hits_by_site` 分站点计数桶，证据截断不影响判定；无站点归属的命中计入每个分组），`ALL` 为汇总；否则只有 `ALL`。
  - 站点键由 `detect_site_root` 推断：特征目录的父目录；特征文件直接位于扫描根时归 `ALL`（不以文件名充当站点键，轮转文件不再产生伪站点）。
  - 字段：`primary`（profile 顺序第一个成立的根因，否则 `NONE`）、`status`（`matched`/`unmatched`/`temporal_conflict`）、`groups[key].root_causes[]`（`condition`/`substituted`/`value`/`temporal_required`/`temporal_ok`/`established`/`note`）、`others`、`temporal_conflict`、`truncated`（证据收集截断的规则；`match_count` 为真实总数、`evidence_count` 为已收条数、`evidence_truncated` 标记）。
- evidence.json 顶层：`detected_sites`、`hit_rules`、`miss_rules`、`error_rules`、`glob_fallback`、`temporal_*`、`results[]`（含 `min_matches`、`encodings`、`skipped_files`（命中 glob 但按分类未解析的文件明细）、`evidence[]{file,line_no,line,timestamp,site,associate,context}`）、`temporal[]`、`verdict`、`file_hashes`（产出证据或被跳过文件的 sha256 原始字节指纹，供证据溯源）。

## 4. render_report.py（子 Skill）

```bash
python3 scripts/render_report.py --final --evidence evidence.json --out report   # 同基名出最终 report.md + report.html
# 或显式：--md report.md [--html report.html]
# 缺省（不传 --out/--md）：输出到 --evidence 所在目录 report.md + report.html（即待分析文件所在目录）
    [--profile assets/fault-profile.yaml] [--manifest <workdir>/manifest.json] \
    [--cloudmat cloudmat.json] [--clock clock.json] [--precheck precheck.json] \
    [--specialized specialized.json] \
    [--description "<用户描述>"] [--source "<包名>"] [--site <站点键>]
```

- 按 `assets/report_template.md` **面向非专业用户的结果导向结构**预填（标题带序号，网元只显示名称、不含路径，**无**执行/数据完整性/局限附录）：`## 1 结果统计`（加粗 `总网元/正常/故障` 计数 + 各站点一句话汇总表 `网元名称｜故障根因｜处理建议｜证据`，网元名为可点击锚点；多站点逐行）→ `## 2 网元详情`（逐网元 `### 2.N 网元名`，各含 `#### 2.N.1 故障根因`（大白话详述+判定依据）、`#### 2.N.2 处理建议`（有序步骤）、`#### 2.N.3 故障证据`（按文件分组的 `规则/行号/时间戳/原文` 四列表，短时间同类重复行自动去重））→ `## 3 故障现象与甄别`（含 lookalikes、CloudMAT/时钟摘要）。
- `--specialized <json>`：专项脚本复核结论（`{"primary": "R1"|null, "status": ..., "note": ..., "site": 组键, "events": [...]}` 或 `{"sites": {组键: {...}}}`）。verdict 宽松命中但专项精确判定不成立时，结论与 `--verify` 均以专项脚本为准；报告自动插"专项脚本复核"说明与"候选根因（未归因）"行；`events` 数组（`{timestamp, desc}`）用于网元详情"专项脚本关键时间"展示。
- `--precheck <json>`：公共预检结果（precheck.json）可传入供分析参考；**不渲染进报告**（报告面向非专业用户，不含覆盖/完整性附录）。
- `--clock <json 或目录>`：parse_clock.py 输出 JSON；传**目录**时自动合并目录内文件名含 `clock`/`brief` 的多站点时钟 JSON（`files[]` 拼接、gnss.spoofing_suspicions 列表拼接、gnss_records 求和，int 兜底为 list；等价 `parse_clock.py --merge-sites`）。
- 日常交付使用 `--final`：不生成 TODO，根因机理取 profile 的 `mechanism`（未登记则不推测），自动输出原因、证据和处置，不描述脚本运行过程。省略 `--final` 的旧草稿模式仅用于人工定制，TODO 必须清零后才能交付。
- `--site`：只输出某站点分组结论；键不存在时 stderr 告警并单列该站点（未命中）。
- manifest 缺版本类文件（`summary.json`/`*running_version*`/`*software_package_version*`）时，报告自动打 `> 制式/版本：⚠ 版本文件缺失（采集包缺 …），版本以目录名推断（未确认）`（提示不阻断交付）。
- `--html`：内置极简 Markdown→HTML，复用 `report_template.html` 的 CSS；TODO 标记转为可见 `.todo` 高亮，h2 锚点目录（`<nav class="toc">`），网元 h3 生成 `id=ne-<名称>` 锚点供汇总表跳转，含打印样式；单文件内联、无外部依赖。
- 默认读 `../assets/fault-profile.yaml`（需 PyYAML；规则/配置统一 yaml）。
- **不可变域指纹**：脚本填入的「网元详情 · 故障证据」四列表行（`EV4`）尾部带 `<!-- ev:EV4:HASH -->`（HASH = 该行文本 sha256 前 12 位），HTML 渲染时剥除。
- `--from-md report.md --html report.html --evidence evidence.json`：补完 TODO 后由**已编辑的 MD** 重渲 HTML（不重建 MD），保证 MD/HTML 一致。
- `--verify report.md --evidence evidence.json [--html report.html] [--site <键>] [--verify-detail]`：**交付门禁**，逐项打印 PASS/FAIL，退出码 0 通过 / 2 失败：
  - TODO 已清零；
  - 指纹行未被篡改（重算 HASH）且全部来源于本 evidence.json（用同一渲染函数重建行集合比对）——手改路径/行号/原文/时间戳、或从别的 evidence 粘贴证据行都会被检出；失配时输出**行级诊断**：逐字符 diff + 差异集中在反斜杠时提示「疑似反斜杠转义层数不一致（期望 N 个 \，实际 M 个 \）」，默认输出前 3 行摘要、`--verify-detail` 全量；
  - `判定状态` 与 `verdict.status`（或 `--specialized` 专项复核结论）一致；matched 时报告首选根因 == 期望 `primary`；unmatched/temporal_conflict 时报告不得给确定性根因；
  - 给 `--html` 时：HTML 含 MD 全部指纹行单元格、无 `.todo` 高亮、判定状态一致（否则提示 `--from-md` 重渲）。
  - 报告模板为用户定制件；`--verify` 只依赖脚本写入的固定措辞（`**判定状态**`、`- **根因**：R?`），定制模板若改动这两处会导致结论项 FAIL。
- numeric_trend 证据行反斜杠统一编码为 HTML 实体 `&#92;`（`search_evidence.py` 生成侧），保证 MD 字节与 evidence.json 权威行恒一致、人类重写不误改层数；`_inline` 渲染 HTML 时还原为 `\`。

## 5. parse_clock.py

```bash
python3 scripts/parse_clock.py <workdir> --merge --json clock.json
python3 scripts/parse_clock.py --merge-sites clock_siteA.brief.json clock_siteB.brief.json --json merged.json   # 多站点合并
```

- `--merge` 目录模式按 mList 自动合并 gz/bz2 轮转：按 **(父目录, 槽位前缀, 进程形态)** 分组不混流（同目录 `1-1-3-1`/`1-1-4-1` 槽位、PLAT/BSP 进程各自独立，多分组输出键为 `基名@组标签`）；组内按轮转序号 `.0N` 倒序拼成时间流（**无后缀 `.txt` 是当前文件排最后**，实测 DCM 包 `.txt` 最新、`.0N` 越大越旧）；`.gz`/`.bz2`/`.gunzipped` 与解压副本去重；magic bytes 识别伪装后缀压缩；坏文件跳过不中断。
- `Clk[XXX]` 行分发、一行多 Clk 拆分、健壮键值提取（日期时间保护/多词键名/括号/方括号）、位置格式（CLK021/027/028/095/100 GNS）。
- 2021 复位假时间修复（`timestamp_fixed`）；CLK032 告警 / CLK033 切换 / CLK036 Flg / CLK094 复位原因 / CLK099 ClkHealth bitmask 与 XO-OC 频偏 / CLK100 OCXO 黑匣子；UBX 0x0A1D 频谱报文；gnsssvinfo 完整解析（PRN 映射+卫星统计）与 GNSS 欺骗检测；关键字段阈值校验。
- `--merge-sites <JSON...>`：合并多站点 parse_clock 输出（`files[]` 拼接、`gnss.spoofing_suspicions` 列表拼接、`gnss_records` 求和、`last_status` 取最后非空）；`gnss.spoofing_suspicions` **必须为 list**，误写成 int 时自动归一为列表（类型兜底）；合并结果可直接喂 `render_report.py --clock`。
- 字段枚举、位掩码、告警码→字段映射见 `assets/shared/clk-field-dictionary.yaml`。

## 6. parse_cloudmat.py

```bash
python3 scripts/parse_cloudmat.py "<工装日志.log>" --json cloudmat.json
```

输出 `basic_info`（tfp/product/barcode/slot/run_mode/result/duration_seconds/ftp_upload）、`item_list`/`item_count`、`failed_items[]`（name、fail_rounds、首次/末次/连续起始轮次、reasons 含通过标准行）、`bitmaps[]`（与 itemList 对位）、`cycling_table`、`warnings`。

## 7. scaffold_child_skill.py / build_child_skill.py / publish.py

```bash
python3 scripts/scaffold_child_skill.py --profile fault-profile.yaml --check            # 只校验
python3 scripts/scaffold_child_skill.py [name] --profile fault-profile.yaml --path <skills根>
python3 scripts/scaffold_child_skill.py [name] --profile <子skill>/assets/fault-profile.yaml --path <skills根> --update
python3 scripts/scaffold_child_skill.py <name> --path <skills根> --update               # 省略 --profile：复用子 skill 内已有 profile
python3 scripts/scaffold_child_skill.py --inspect <子skill目录>                          # 更新度检查
python3 scripts/scaffold_child_skill.py --inspect <子skill目录> --diff assets/report_template.md
python3 scripts/scaffold_child_skill.py --inspect <子skill目录> --adopt-extra-scripts
```

- 校验项：步骤/规则/根因/时序 ID 唯一；规则 type/trend、根因 confidence、案例 type 枚举；condition 只含规则 ID/and/or/not/括号且引用存在；`verified: false` 规则不得被 condition 引用；temporal first/second 存在且不同、max ≥ min；`root_causes[].temporal` 引用存在；decision.goto ∈ 步骤 ∪ 根因 ∪ EXCLUDE；案例 expected_root_cause/evidence 引用存在、不自相矛盾；所有正则可编译；globs 非空；components 键合法；skill.name 合法（Agent Skills 规范：`^[a-z0-9]+(-[a-z0-9]+)*$`，≤64）；skill.title 非空。
- **frontmatter**（Agent Skills 规范）：仅 `name`、`description`（≤1024，JSON 双引号转义——别名含 `: # "` 也不破坏 YAML；含告警码 ASCII 锚点与一行英文触发词）。版本见 `assets/toolkit-version.json`，故障名见 profile。复制模板时忽略 `__pycache__/*.pyc/.cache`。
- 产物：`SKILL.md`（按组件条件渲染，尾部含版本戳注释）、`references/fault-profile.md`、`assets/fault-profile.yaml`、`assets/evidence-rules.yaml`（rules + temporal + root_causes）、`assets/quick-signatures.yaml`（quick_signatures 非空时）、`assets/common-rules.yaml`（common_rules 开启时，供预检 `--rules` 直接用）、`assets/toolkit-version.json`（版本戳）、按需脚本与共享字典、报告模板。
- 组件按需复制（`components` 显式或自动推断，打印逐项取舍）：`unpack`、`search`、`report`（随 search）、`parse_clock`、`parse_cloudmat`、`alarm_dict`、`clk_dict`、`common_rules`；`log_format.py` 为 unpack/search 公共依赖，随任一自动复制。推断信号见 `child-skill-spec.md` 2.8。
- **版本戳与更新机制**：每次生成/--update 写 `assets/toolkit-version.json`：`version`（TOOLKIT_VERSION，builder 工具箱对外行为变化时递增）、`digest`（受管文件 sha256 汇总）、`generated_at`/`updated_at`/`generation`（生成代数）、`files`（受管文件 sha256 表，SKILL.md 不入哈希——其尾部注释已携带版本戳）。
- **派生件白名单**：`evidence-rules.yaml`、`fault-profile.yaml`、`references/fault-profile.md`、`common-rules.yaml` 是由 profile/shared 源生成的导出型派生件，`--inspect` 只校验存在、不做字节级源比对（平台换行/键序差异不应误报 stale）。`common-rules.yaml` 生成时直接复制 builder 预导出副本（字节稳定）。
- `--update`：重算组件并增删脚本/字典，重渲染派生文件；保留用户定制的报告模板（`USER_CUSTOMIZABLE`）。**幂等**：若本次将写入内容与版本戳完全一致（builder 版本未变），打印"已是最新"并退出 0，**不写盘、不递增 generation**。省略 `--profile` 时复用子 skill 内已有的 profile（不会拿旧 profile 静默覆盖磁盘改动）。升级差异分行报告：`builder 更新`（受管脚本/资产同步 builder 新版）、`随 profile 重生成`（派生件）、`新增受管件`、`本地改动的受管脚本/资产已被覆盖`；profile 覆盖单列语义：`已按 --profile 输入更新`，若磁盘 profile 曾被本地修改且与本次输入不同会显式警告并提示先同步。
- `--inspect`：三方比对（版本戳 vs 磁盘 vs builder 源）→ 逐文件报告 `stale`（builder 有新版）/`local-modified`（本地改动）/`missing`/`new`（builder 新增件，按子 skill 自身 profile 推断期望组件避免误报）；报告模板差异为 `custom`（本地已改）/`tpl-drift`（builder 模板有新版，按占位符替换后的基线给出行级 +N/-M 差异），可用 `--diff <rel>` 看 unified diff；`--adopt-extra-scripts` 把目录内非受管 `.py` 一键写入 `components.extra_scripts`（备份 `fault-profile.yaml.bak`，无 PyYAML 时改打印可粘贴片段）；旧版无戳子 Skill 直接按 builder 源比对并提示"先 --update 打戳建基线"。
  - **退出码分级**：`0`=与 builder 同步；`1`=仅本地改动/报告模板漂移（可交付，建议人工看一眼，不必 update）；`2`=有 stale/missing/new 受管文件（需 --update）。

### 7.1 gen_specialized.py（专项复核结论归一，builder + 子 Skill）

```bash
python3 scripts/gen_specialized.py --out specialized.json --conclusions concl.json          # A/B/C 三形态
python3 scripts/gen_specialized.py --out specialized.json --site <键> --conclusion '{...}'  # 单条内联
python3 scripts/gen_specialized.py --out specialized.json --from-special special_*.json     # 直接读专项脚本输出（可多个，站点键从文件名或 --site）
```

- 归一规则：`status == "normal" → "unmatched"`；status 缺失按 primary 推导（非空→matched，否则 unmatched）；primary/status/note/source/events 逐项透传（`events: [{timestamp, desc}]` 供报告「专项脚本关键时间」）。
- `--from-special`：读取各站点专项 JSON 的 `summary`/`result`，`primary_condition` 1/2→`R1`、3/4→`R3`（None→`primary: null`）、命中→matched、未命中→unmatched，`note`=condition_text/description、`events` 从 `result.occurrence_time`/`description` 提取；多站点一键映射，免手工写 conclusions 桥接。


## 8. scan_rule_candidates.py

```bash
python3 scripts/scan_rule_candidates.py <已解压目录> --json candidates.json [--top 10]
```

扫描告警码频次（fm-active/fm-history `.csv` 与 `.xlsx`）、错误行聚类、关键字段命中（复位/时钟/链路/电源/温度）、数值序列，输出 `rule_suggestions[]`——含 `id`/`type`/`globs`/`any_of`（告警码规则按实际文件标 `format: xlsx|csv`，关键词规则 glob 从样本文件名推导）供专家确认后写入 profile。

## 9. init_case.py / run_regression.py

```bash
python3 scripts/init_case.py --cases-dir <root> --skill <slug> --id case-01-xxx --kind positive [--clock]
python3 scripts/run_regression.py --cases-dir <root> --skill <slug> --skills-dir <skills根> --json report.json
python3 scripts/run_regression.py --cases-dir <root> --skill <slug> --skills-dir <skills根> --e2e auto
python3 scripts/run_regression.py --case <case目录> --skills-dir <skills根>
```

- 案例库在 skill 之外，结构与 `case.yaml` 字段见 `validation-protocol.md` 第四章；`case.yaml` 是唯一断言来源，构建自动重建 profile 中的案例展示索引。
- 快速回归流水线 unpack → search（含 verdict）→（parse_clock）→ 结构化断言；根因断言直接取 `verdict.primary`（旧 evidence 无 verdict 时本地回退计算，逻辑同源）。
- 全量 `--cases-dir --skill` 自动执行覆盖率门禁：每个根因至少一个非负例案例，condition 引用的规则均被其案例的正/反断言覆盖。
- `--e2e auto`：每个根因选一例，另选反例、多站点和 `conflict` 标签案例，执行 `run_analysis.py` 并检查最终 MD/HTML 和 `--verify`；`--e2e all` 跑全部案例，默认 `none` 只跑快速判定。构建固定使用 `auto`。
- 断言只比关键字/文件后缀/次数，不比绝对路径/行号/整行；每次全新临时 `--workdir`，记录输入 sha256；退出码非 0 即有 FAIL/ERROR、覆盖率缺失或端到端失败。
- **增量缓存**：输入哈希 + case.yaml + 工具箱 digest 未变时复用上次快速判定结果（标记 `(cached)`），`--no-cache` 关闭；缓存存 `<workdir>/.cache/`。ERROR 不缓存；端到端始终重跑，不复用快速判定缓存冒充报告验证。

## 10. tests/selftest.py

```bash
python3 tests/selftest.py [--keep] [--only 8]                # --only 分节选择，依赖节自动带上
python3 tests/selftest.py --json selftest-report.json        # 结构化结果（节/条目/通过失败/退出码）
```

基于模板示例 profile 合成 4 个 CloudMAT 案例（正例 / 二次打包+前缀目录 / 反例 / 日志不完整→R2），在临时目录跑完 `--check`（含错误 profile 逐项报错）→ scaffold → `--update`（含幂等短路）→ init_case → run_regression → render_report → `--verify` 正反用例 → GBK 管道输出；另合成参考数据集同构的 DCM/FM 包回归格式支持（`.bak.N` magic-gzip→`.gunzipped`、时钟按槽位分组合并、collectinfo、`.bin` 登记、fm `.xlsx` 整文件提取与行列检索）。§0 额外校验模板不含脚本副本（生成时由 builder 复制）、模板目录无 `__pycache__`、SKILL.md 规范。任何一项失败退出码非 0。**修改任何脚本后必须跑通；生成/更新子 Skill 后也建议跑一次。** 运行结束打印固定机器可读末行 `SELFTEST_RESULT=PASS|FAIL: N 失败（共 M 项）`，`--json` 另写结构化报告（`result/exit_code/total/passed/failed/sections/failures`）。

**Windows / PowerShell 验证建议**：控制台输出很长，PowerShell 5.1 用 `2>&1` 会把 Python 的 stderr 当错误流触发 NativeCommandError、且 `&&` 不支持。推荐改用 cmd 重定向并抓退出码：

```bat
cmd /c "python tests\selftest.py --json selftest-report.json > out.txt 2>&1 & echo EXIT=%ERRORLEVEL%"
```

随后直接读 `selftest-report.json` 的 `result` 字段与 `out.txt` 末尾的 `SELFTEST_RESULT=` 行，无需翻长输出。

CI（`.github/workflows/selftest.yml`）在 ubuntu/windows/macos × Python 3.11/3.12/3.13 跑同一自测，另有 no-pyyaml job 验证零依赖路径。

## 11. check_env.py / validate_skill_md.py / package_skill.py

```bash
python3 scripts/check_env.py [--json env.json]                 # Python≥3.11(必需)/PyYAML/7z·unar/zstd/控制台编码/长路径/模板干净；1=版本不满足
python3 scripts/validate_skill_md.py <skill目录|SKILL.md>... | --skills-dir <skills根>   # Agent Skills 规范：仅 name/description 必填，扩展字段兼容但不渲染；无 PyYAML 时用内置简易解析
python3 scripts/package_skill.py [--skill <目录>] [--out dist/] [--name x.zip]            # 可复现 zip（排除 __pycache__/.cache，固定时间戳）+ .sha256
```

## 12. import_rule_library.py / rule_library_diff.py（builder 侧，不分发给子 Skill）

这两个脚本把 `规则文件/` 下的规则库解析为候选 fault-profile，或在规则库更新后对比增量。它们是 **builder 侧工具**，**不进入子 Skill 分发清单**（否则会被复制给子 Skill）。

### 12.1 规则库导入 import_rule_library.py

```bash
# 单文件导入（--type auto 自动嗅探黑匣子/蜂鸟）
python3 scripts/import_rule_library.py --source 规则文件/黑匣子.../vswd0.xlsx --out 候选/bbu-fault-vswd0.yaml --type auto --board VSWd0
# 目录导入：默认按文件拆分，每文件一个候选 profile；slug 自动去重同板双版本
python3 scripts/import_rule_library.py --source 规则文件/黑匣子... --out 候选/ --json 候选/meta.json
# 增强：--baseline <旧profile> 保持 E/S 编号稳定；--merge-into <profile> 合并更新（人工改动优先，库中已删除的规则标 library_removed，不删除，写前备份 .bak）
#       --list-groups / --only-groups <正则> 辅助人工拆分；--step-by group|sheet
```

**两类规则库**：

| 规则库 | 文件 | 列特征 | 解析要点 |
|---|---|---|---|
| 黑匣子 | 多个 xlsx（数量随规则库演进变化） | `关键字`/`日志路径和文件名`/`故障呈现`(+检查规则/维修建议/拦截列) | 表头行自动定位（容忍表头前的标题行、表头内空白/换行）；sheet 级宽松签名（关键字 + (路径或呈现)）；USB/SSD/other 样本 sheet 按 sheet 独立过滤；多 sheet 重复规则合并；关键字→`any_of`（OR，召回优先；逗号/顿号仅在括号外拆分，保护 `{1,3}`；re.compile 失败 re.escape；ASCII 关键字过短给告警）；路径转 `**/<basename>*`，路径为空回退 `**/*.log*` 并告警 |
| 蜂鸟 | 多个 xlsx（5-sheet 巡检报告） | `规则ID/规则类型/模块/规则名称/详细信息/具体影响/处理建议` | 仅「规则」sheet 有效；描述性文本走锚点抽取作 `any_of`（日志文件名→【】/引号/括号→领域词表，最多 6）；巡检故障报告/开发者确认 仅表头 → 记警告不产出 |
| 黑匣子 Metrics.csv | 1（GBK 度量字典，可选） | `Metrics对象/类型编号/...` | 非检测规则表，自动跳过并提示 |

**候选 profile 语义**：`mode: triage`、规则 `verified: false`、`root_causes` 空、`analysis.steps` 只做证据检索。可通过 `--check`（triage 豁免），但不可交付为可判定 skill。步骤按源表出现顺序、规则 ID `E{n}` 递增、步骤 ID `S{gi}`（`--baseline` 下沿用旧编号）；YAML 用内置最小序列化器（字符串标量 JSON 引号包裹，如 `- id: "E1"`）。`--board` 未给时从文件名推断（vswd1A→VSWd1A、HCC REPORT_SDR→SDR）。

**triage 闭环**：candidate 可 scaffold 为子 Skill——`evidence-rules.yaml` 透传 `mode`/`verified:false`；`search_evidence` 结果带 `verified`、summary 带 `mode`/`unverified_hit_rules`，brief 标「HIT(未验证)」；`render_report` 生成「分诊线索报告」（不给根因/判定，`--verify` 拒绝确定性根因）；`run_analysis` 摘要 `status: triage`；子 SKILL.md 渲染 `TRIAGE` 块；router 默认跳过 triage 候选（`skipped_triage`，`--include-triage` 仅展示）。

**容错**：xlsx 纯 stdlib（复用 `search_evidence.iter_xlsx_rows`）；csv 按 `utf-8-sig→GBK→GB18030` 回退解码，全失败记警告跳过；无法识别类型/Metrics.csv 自动跳过不中断整体；解析 0 条记警告（蜂鸟非「规则」sheet 属正常预期）；Windows 控制台重配 UTF-8。

### 12.2 差异对比 rule_library_diff.py

```bash
# 同板新旧版本对比
python3 scripts/rule_library_diff.py 规则文件/黑匣子.../vswd0.xlsx 规则文件/黑匣子.../vswd0-0804.xlsx --json diff.json
# 也可对比候选 profile（yaml 需 PyYAML）
```

身份键 = 故障呈现/规则名（重名加 `#n`），内容摘要 = 关键字+路径+检查规则。仅右侧出现 → added；仅左侧 → removed；两侧都有但内容摘要不同 → changed。`--fail-on-change`：有任何差异退出码 1（CI 用）。输出 `新增 N；删除 M；变更 K` + 明细；`--json` 写结构化结果（left/right/added/removed/changed）。

## 13. skill_sync.py（builder 侧，元 skill 与子 Skill 双向同步）

```bash
python3 scripts/skill_sync.py --sync <子skill目录>                       # 元 skill 基础能力 → 子 skill（保留子 skill 定制，幂等）
python3 scripts/skill_sync.py --reclaim <子skill目录>                    # dry-run：列出子 skill 与元 skill 基础能力差异
python3 scripts/skill_sync.py --reclaim <子skill目录> --diff              # 差异时输出行级 +/- diff（回收决策参考）
python3 scripts/skill_sync.py --reclaim <子skill目录> --apply [--include-template]
```

- 同步/回收范围 = **基础能力**：共享脚本（unpack/search/parse_clock/parse_cloudmat/log_format/scan_rule_candidates/gen_specialized/fm_alarm_align/render_report）、共享字典、公共规则、可选报告模板。
- `--reclaim` 默认 dry-run 只列差异（`+/-` 行数概览），`--apply` 才写回元 skill（写回 builder `scripts/` 单一源）；`--diff` 时每个差异文件输出 `difflib.unified_diff` 行级 +/-（含 `@ @` 定位）。
- **不回灌**子 skill 特有内容：profile 派生件、`components.extra_scripts` 专项脚本。回收后必须跑 `tests/selftest.py` 全绿再分发。
