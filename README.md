# BBU FaultScope · BBU 故障诊断平台

> 公开镜像说明：本仓库为代码演示版本，默认模型地址、模型名和示例企业内网配置已替换为占位值；`data/`、`config.json`、运行时技能/案例、发布构建物和现场日志不入库。部署时请填写自己的 OpenAI 兼容 MaaS 地址与密钥。

面向内网 BBU / ITBBU / SDR 现场故障的**自包含诊断平台**：上传故障描述与日志包，AI 先基于描述、日志结构和技能目录生成动态波次计划，技能按证据门槛并行执行；证据不足时最多补规划一次，随后仲裁并生成可交付的 HTML/Markdown 报告。

## 核心能力

- **不依赖第三方平台**：后端仅使用 Python 3.12 标准库，PyYAML 以纯 Python 源码 vendor 在 `vendor/yaml`；前端为免构建原生 JS/CSS，无 npm、无 CDN、无外网依赖。
- **多种 AI 接入**：支持 OpenAI 兼容协议（`/v1/chat/completions`，适配 OpenAI、vLLM、OneAPI、内网网关等）与 Anthropic 原生协议（`/v1/messages`）；未配置模型时全部走确定性路径，平台仍可完成诊断。
- **元 Skill 驱动的技能工坊**：整段粘贴故障资料即可生成技能。AI 只做语义提取，规则、正则、阈值、根因表达式、YAML 和案例断言由确定性编译器生成；可中途自由文本补充确认，经质量审查、案例回归和发布门禁后产出可直接参与规划的子 Skill。
- **可执行诊断推理**：每个技能除规则外还携带专家排查路径——竞争假设、证据检查顺序、关键鉴别点、阈值口径、误判陷阱和停止条件；这些推理绑定真实规则编号，planner 用它形成假设，报告只陈述本次真实命中/未命中，不伪造思维链。
- **真实案例与评测隔离**：真实日志登记为 `unlabeled` 时不参与准确率；只有带专家 `expected/excluded` 标签的 `labeled/golden` 案例才进入确定性评测，质量页单独展示“已复核任务 / golden 案例 / 独立测试集 / 真实命中率”四道成熟度门禁，合成数据不计入真实 golden。
- **证据质量与拒判**：证据按文件独立性、规则独立性和重复度打分；单文件重复日志不能升级高置信，两个独立根因返回 `multiple_causes`，有异常但无已知根因返回 `unknown_fault`，不硬猜。
- **专家反馈闭环**：任务终态可确认、改判或驳回；未复核经验默认不进入 planner 检索；案例可回流为真实案例；技能支持 stable/canary 灰度通道，普通任务默认只用稳定技能。
- **生产可观测与断点恢复**：`/api/metrics` 与 Prometheus 文本暴露队列、任务、技能、耗时和模型健康；队列有上限，服务重启后 running 任务在次数上限内自动恢复并跳过已完成技能；支持包可脱敏导出并用离线脚本重跑。
- **历史经验与自动沉淀**：每次终态任务把计划、证据缺口、技能表现和最终结论写入经验库，相似任务可被 planner 检索参考；成功任务在后台生成「自学习」工坊草稿，必须由管理员在工坊确认后发布，不会自动上线。随包 `knowledge/reasoning_playbooks.json` 还提供三份专家排查剧本作为种子经验，明确标记为合成数据。
- **动态 DAG + 并行扇出**：`受理 → 盘点 → AI 规划 → 按计划执行（波次内并行）→ 可选一次补规划 → 仲裁 → 报告`；计划是线性波次，不设计循环。默认 4 个子 Skill 同时执行，任务级队列默认 4 并发。
- **有界 AI，拒绝无限循环**：LLM 只用于规划 1 次、补规划最多 1 次、冲突仲裁最多 1 次、报告研判 1 次；任务级 Budget 强制限制调用次数、Token 与墙钟，任一超限立即回退确定性结果。计划里的重复技能、目录外技能、超并行上限都会被确定性裁剪。
- **本地账号与 RBAC**：SQLite 元数据、scrypt 口令、HttpOnly/SameSite=Strict Cookie、CSRF 校验、强制首改密、IP+账号双维度登录限流、改密/重置/停用即撤销会话、admin/engineer 两级权限、全量审计日志。
- **统一工作目录与精简迁移**：管理员可在网页把运行数据根切换到独立磁盘；导出 ChaCha20-Poly1305 加密迁移包（不含原始上传、案例输入和解压目录），在新实例覆盖恢复。Linux 由 root/systemd 助手自动停服切换，Windows 重启前激活。
- **离线部署**：提供 `deploy/install_offline.sh`、systemd 单元、离线 release 打包与备份脚本；服务器无需 pip/npm。

## 1.10.0 本轮变化

- 用户可见“病例”统一改为“案例”，数据库表和 API 字段保持兼容；“现场真实案例库”与“技能回归案例”仍是两个对象。
- 新建模型默认企业 MaaS，支持“测试当前填写值”不落库；新建诊断取消人工标题，由 AI 生成 40 字内标题，失败保留确定性占位。
- 任务详情固定为诊断概览 / 证据与技能 / 诊断报告 / 运行详情四个一级分区，完成态默认打开报告，SSE 刷新保持当前页签。
- 用户管理只保留用户名（默认显示名）、角色、领域、初始密码；管理员可设 9 位一次性密码，首次登录强制改密。
- 技能工坊改为五步引导、后台任务和真实阶段进度；支持自动 name/title、案例列表与覆盖、模型失败提示和重试。
- 系统信息新增全量并发/预算/容量在线编辑、保存并重启、仅重启；新增任务空间统计与选择性释放，只删原始输入，保留报告和证据。
- 新增 `tools/bbu_ops.py` 与 `bbu-ops`/`bbu-ops.cmd`，统一 status / doctor / start / stop / restart / logs / backup / storage / release / config。
- 数据库 schema 10；单元测试 83/83、HTTP 全量冒烟 105/105 通过。详见 `docs/版本说明-1.10.0.md`。

## 快速开始（开发/单机）

要求：Python 3.12（最低 3.11），无需安装任何 pip 包。

```bash
cd bbu-fault-platform
python3 run.py --host 127.0.0.1 --port 8080
```

首次启动会自动创建管理员 `admin`，随机初始密码写入：

```text
data/初始管理员密码.txt
```

浏览器打开 `http://127.0.0.1:8080/`，用该密码登录并按提示修改密码。首次启动还会把 `examples/skills` 下的两个示例技能安装到 `skills/`。

Windows 不需要一键部署包，Python 3.11+ x64 直接运行：

```bat
cd /d D:\bbu-fault-platform
python run.py --host 0.0.0.0 --port 8080
```

也可双击 `deploy\windows\start_platform.bat`。Windows 兼容性、可写目录、内网 pip 源和常见问题见 [`docs/Windows运行说明.md`](docs/Windows运行说明.md)。内网 pip 源配置在 `deploy/pip.ini`（Windows）和 `deploy/pip.conf`（Linux）。

演示日志在 `tests/fixtures/logs/`：

- `clock-demo.zip`：时钟失锁
- `optical-demo.zip`：光模块收光异常
- `mixed-demo.zip`：时钟 + 光模块混合故障（用于验证多 Skill 并行）
- `normal-demo.zip`：无明显故障

## 接入 AI

进入「模型接入」添加配置：

| 协议 | Base URL 示例 | 模型名示例 | 说明 |
| --- | --- | --- | --- |
| OpenAI 兼容 | `https://api.openai.com/v1` 或内网 `http://vllm:8000/v1` | `gpt-4o-mini` | Base URL 填到 `/v1` 结尾，平台请求 `/chat/completions` |
| Anthropic | `https://api.anthropic.com` | `claude-sonnet-4-5` | 原生 `/v1/messages` 协议 |
| Mock | 留空 | `mock` | 仅用于联调，返回确定性占位结果 |

API Key 使用 `data/master.key` 派生密钥加密后存 SQLite，只在服务端解密，前端只显示掩码。可给同一配置勾选多个角色：`default`（兜底）、`planner`（生成动态计划）、`router`（老部署的规划回退）、`arbiter`（冲突仲裁）、`writer`（报告研判）、`workshop`（技能起草）。同一角色多个配置按 `priority` 从小到大尝试，失败自动切换下一个。

## 规划与自学习配置

`config.json` 中的相关开关（也可在部署时统一预置）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `planner_enabled` | `true` | 有真实 planner 时生成动态计划；关闭则只用确定性波次 |
| `planner_replan` | `true` | 允许在证据不足时补规划，代码保证最多一次 |
| `planner_max_steps` | `4` | 单计划最大波次数 |
| `planner_max_total_skills` | `8` | 单计划最多技能数 |
| `planner_replan_max_skills` | `4` | 补规划最多新增技能数 |
| `auto_distill` | `true` | 成功任务后台沉淀为工坊草稿，仍需人工发布 |

计划门槛固定为 `always` / `previous_unmatched` / `previous_conflict` / `previous_low_confidence` / `previous_gap`；首步强制 `always`。

## 一键离线安装（内网服务器）

在能拷贝文件的机器上生成离线包：

```bash
bash deploy/make_release.sh
# 产物：dist/bbu-fault-platform-1.10.0-offline.tar.gz
```

拷贝到内网服务器后：

```bash
tar -xzf bbu-fault-platform-1.10.0-offline.tar.gz
sudo bash bbu-fault-platform/deploy/install_offline.sh --port 8080
```

默认安装：

- 程序：`/opt/bbu-fault-platform`（只读代码）
- 运行数据：`/var/lib/bbu-fault-platform/{data,skills,cases,updates,backups}`
- 系统用户：`bbu`
- systemd 服务：`bbu-fault-platform.service`

已部署实例升级（从新包目录执行，自动备份并保留数据与配置）：

```bash
sudo bash bbu-fault-platform/deploy/upgrade_offline.sh
```

详细参数、反向代理、升级回滚、备份与故障排查见 `docs/部署手册.md` 和 `docs/升级与回滚.md`。

## 目录结构

```text
bbu/                 后端（标准库 HTTP 服务、认证、AI 网关、流水线、技能、工坊）
web/                 免构建前端（index.html + assets/js 原生 ES Module + CSS）
meta/                元 Skill（技能工坊的构建契约与脚本）
examples/skills/     随包示例技能（首次启动安装到 skills/）
skills/              运行时子 Skill 目录
cases/               技能案例目录（案例回归）
vendor/yaml/         纯 Python PyYAML
tests/               单元测试、E2E 冒烟、推理基准与合成日志 fixtures
datasets/            诊断推理语料、基准题与专家 playbook（合成数据）
knowledge/           随包种子经验 JSON
deploy/              离线安装、systemd、发布打包、备份
docs/                架构、部署、使用与验收文档
data/                运行数据（数据库、上传、任务、密钥；不应进入发布包）
```

## 诊断推理数据层

平台自带 75 条可执行合成案例（看门狗 25、SSD 26、USBFLASH 24），覆盖全部根因、阴性、阈值边界、组合证据、深层目录、zip/tar.gz/嵌套压缩包；每条案例的 `case.yaml` 是唯一断言来源。

```bash
# 重新生成案例、推理语料、基准题和 playbook
python3 tools/generate_diagnostic_dataset.py

# 技能检索与报告推理验收
python3 tests/diagnostic_benchmark.py
python3 tests/reasoning_e2e.py

# 把专家剧本写入本地经验库（随包版本由平台启动自动加载）
python3 tools/seed_reasoning_experiences.py
```

数据目录：

- `cases/<skill>/<case-id>/`：技能回归案例，可用元技能 `run_regression.py` 直接消费。
- `datasets/diagnostic_reasoning/`：JSONL 推理语料、benchmark 题、`playbooks/*.yaml`、数量统计。
- `knowledge/reasoning_playbooks.json`：随部署包分发，启动时写入 `plan_experiences`，标记 `source=curated_reasoning`。

这些是合成数据，不是真实现网日志；报告会明示“未验证项”和“证据不足”，不会把检索到的历史经验当成本次结论。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 tests/workshop_regression.py
python3 tests/reasoning_e2e.py
python3 tests/diagnostic_benchmark.py
python3 tests/e2e_smoke.py
python3 tests/api_full_smoke.py
```

`e2e_smoke.py` 会在临时目录启动独立实例（不污染 `data/`），验证登录、模型配置、上传日志、AI 计划、单/多 Skill 执行、一次补规划、历史经验检索与 HTML 报告生成。`test_planner.py` 覆盖计划校验、gate、补规划停止条件和经验检索。`workshop_regression.py` 把三类整段资料编译成技能，并用覆盖全部根因的合成正例/反例跑 `build_child_skill.py` 回归。`api_full_smoke.py` 逐条覆盖全部 105 个 REST 路由，并验证 401/403/404/409/400、CSRF、管理员/工程师权限、SSE，以及“未验证禁止发布、正反例回归后才可发布”。

## 关键约束

- 日志包仅接受 `zip` / `tar.gz`（含 `tar`、`tgz`、`tar.bz2`、单文件 `gz/bz2`）；默认单包上限 2048 MB，解压总量上限 8192 MB、文件数上限 200000。
- 原始日志不会发给模型；只发送抽取后的证据片段与技能目录。
- 输出是现场辅助诊断结论，不替代厂家规范；涉及复位、升级、割接等操作须按现网流程审批。
