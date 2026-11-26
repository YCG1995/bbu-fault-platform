# BBU FaultScope · BBU 故障诊断平台

面向内网 BBU / ITBBU / SDR 现场故障的**自包含诊断平台**：上传故障描述与日志包，AI 先基于描述、日志结构和技能目录生成动态波次计划，技能按证据门槛并行执行；也支持只执行一个指定技能。证据不足时最多补规划一次，随后仲裁并生成可交付的 HTML/Markdown 报告。

## 核心能力

- **不依赖第三方平台**：后端仅使用 Python 3.12 标准库，PyYAML 以纯 Python 源码 vendor 在 `vendor/yaml`；前端为免构建原生 JS/CSS，无 npm、无 CDN、无外网依赖。
- **多种 AI 接入**：支持 OpenAI 兼容协议（`/v1/chat/completions`，适配 OpenAI、vLLM、OneAPI、内网网关等）与 Anthropic 原生协议（`/v1/messages`）；未配置模型时全部走确定性路径，平台仍可完成诊断。
- **元 Skill 驱动的技能工坊**：整段粘贴故障资料即可生成技能。AI 只做语义提取，规则、正则、阈值、根因表达式、YAML 和案例断言由确定性编译器生成；可中途自由文本补充确认，经质量审查、案例回归和发布门禁后产出可直接参与规划的子 Skill。
- **可执行诊断推理**：每个技能除规则外还携带专家排查路径——竞争假设、证据检查顺序、关键鉴别点、阈值口径、误判陷阱和停止条件；这些推理绑定真实规则编号，planner 用它形成假设，报告只陈述本次真实命中/未命中，不伪造思维链。
- **真实案例与评测隔离**：真实日志登记为 `unlabeled` 时不参与准确率；只有带专家 `expected/excluded` 标签的 `labeled/golden` 案例才进入确定性评测，质量页单独展示“已复核任务 / golden 案例 / 独立测试集 / 真实命中率”四道成熟度门禁，合成数据不计入真实 golden。
- **模型参与评测**：默认 `llm_mode=off` 保持可复现基线；需要验证模型规划/报告能力时显式使用 `tools/real_cases.py evaluate --llm-mode full --model-id <id>`，结果记录实际模型、调用次数、token、模型叙述采纳与回退数量。
- **证据质量与拒判**：证据按文件独立性、规则独立性和重复度打分；单文件重复日志不能升级高置信，两个独立根因返回 `multiple_causes`，有异常但无已知根因返回 `unknown_fault`，不硬猜。
- **专家反馈闭环**：任务终态可确认、改判或驳回；未复核经验默认不进入 planner 检索；案例可回流为真实案例；技能支持 stable/canary 灰度通道，普通任务默认只用稳定技能。
- **生产可观测与断点恢复**：`/api/metrics` 与 Prometheus 文本暴露队列、任务、技能、耗时和模型健康；队列有上限，服务重启后 running 任务在次数上限内自动恢复并跳过已完成技能；支持包可脱敏导出并用离线脚本重跑。
- **历史经验与自动沉淀**：每次终态任务把计划、证据缺口、技能表现和最终结论写入经验库，相似任务可被 planner 检索参考；成功任务在后台生成「自学习」工坊草稿，必须由管理员在工坊确认后发布，不会自动上线。随包 `knowledge/reasoning_playbooks.json` 还提供三份专家排查剧本作为种子经验，明确标记为合成数据。
- **动态 DAG + 并行扇出**：`受理 → 盘点 → AI 规划 → 按计划执行（波次内并行）→ 可选一次补规划 → 仲裁 → 报告`；计划是线性波次，不设计循环。默认 4 个子 Skill 同时执行，任务级队列默认 4 并发。
- **有界 AI，拒绝无限循环**：LLM 只用于规划 1 次、补规划最多 1 次、冲突仲裁最多 1 次、报告研判 1 次；任务级 Budget 强制限制调用次数、Token 与墙钟，任一超限立即回退确定性结果。计划里的重复技能、目录外技能、超并行上限都会被确定性裁剪。
- **本地账号与 RBAC**：SQLite 元数据、scrypt 口令、HttpOnly/SameSite=Strict Cookie、CSRF 校验、首次登录直接进入工作台、IP+账号双维度登录限流、改密/重置/停用即撤销会话、admin/reviewer/engineer 三级权限、全量审计日志。
- **平台助手与 MCP Server**：所有登录用户可使用右下角悬浮助手查平台文档、自己的通知/任务事件/外部 IM；管理员额外拥有系统状态、备份/空间、升级/回滚、用户/模型/通知配置等受控运维能力。同一工具层通过 `POST /mcp` 暴露为 MCP Server，每个账号使用独立 Bearer Token，默认只接受 loopback。
- **统一工作目录与精简迁移**：管理员可在网页把运行数据根切换到独立磁盘；导出 ChaCha20-Poly1305 加密迁移包（不含原始上传、案例输入和解压目录），在新实例覆盖恢复。Linux 由 root/systemd 助手自动停服切换，Windows 重启前激活。
- **离线部署**：提供 `deploy/install_offline.sh`、systemd 单元、离线 release 打包与备份脚本；服务器无需 pip/npm。

## 1.24.5 MCP 客户端配置布局

本版本优化“系统信息 → MCP / 助手”的客户端配置；数据库 schema 保持 21，MCP 接口与 Token 流程不变。

- **左右分区**：左侧展示 read 模式说明、三步接入步骤和 Token 提示，右侧展示完整 JSON 配置。
- **代码面板**：textarea 改为带标题栏的代码块，复制按钮移到右上角，超长行自动换行。
- **复制行为**：复制完整配置 JSON 与 `<YOUR_TOKEN>` 占位，页面实体编码不会带入剪贴板。
- **移动端**：≤900px 说明与代码上下排列，390px 无横向溢出。
- 验证：MCP UI 冒烟通过；单元/集成 **222 passed**、REST 冒烟 **152/152**。详见 `docs/版本说明-1.24.5.md`。

## 1.24.4 迁移页覆盖恢复布局

本版本优化“系统信息 → 迁移”页面的覆盖恢复和迁移记录区域；数据库 schema 保持 21。

- **危险区左右分区**：左侧显示高风险说明、备份与回滚规则，右侧集中放置导入表单。
- **迁移包选择**：原生文件输入改为可点击的拖放区，选中后显示文件名与包大小，可重新选择。
- **口令并排**：迁移口令与确认口令在桌面两列、移动端单列；上传按钮与状态文案同排，避开悬浮助手。
- **记录空态**：空记录改为带图标的说明卡，标题旁保留 30 条与手动删除提示。
- 验证：新迁移 UI 冒烟通过；单元/集成 **222 passed**、REST 冒烟 **152/152**。详见 `docs/版本说明-1.24.4.md`。

## 1.24.3 任务空间布局优化

本版本优化“系统信息 → 任务空间”页面的信息层级与操作区；数据库 schema 保持 21。

- **右侧工具栏**：释放模式、批量释放与重新统计统一靠右，标题说明居左。
- **技能诊断单行**：技能下拉固定宽度，与「按技能诊断」按钮同高同一行，不再换行。
- **平铺概览**：四个空间数字改为平铺统计卡，释放规则收敛为一条提示，并单独展示磁盘剩余。
- **空态与清单**：无任务时不再渲染表头和空行，改为空态卡；有任务时先展示任务清单标题，再显示表格。
- **移动端**：≤720px 表格转卡片，任务状态、空间明细与操作纵向排列，390px 无横向滚动。
- 验证：UI 冒烟通过；单元/集成 **222 passed**、REST 冒烟 **152/152**。详见 `docs/版本说明-1.24.3.md`。

## 1.24.2 技能工坊空态布局

本版本优化技能工坊无草稿时的首屏：去掉“左侧空草稿列表 + 右侧空详情”的重复空态，改为全宽起始卡；数据库 schema 保持 21。

- **全宽引导**：无草稿时直接展示“从一段真实故障资料开始”，提供主按钮与资料支持说明。
- **流程可见**：右侧列出粘贴资料、AI 语义提取、确定性编译、案例回归、发布五个步骤。
- **不再重复空态**：草稿列表仅在已有草稿时显示；新建第一份草稿后恢复左侧列表 + 右侧详情的工坊工作区。
- 验证：单元/集成 **222 passed**、REST 冒烟 **152/152**。详见 `docs/版本说明-1.24.2.md`。

## 1.24.1 用户每日用量图表

本版本把 1.24.0 的用户每日用量表升级为图表视图；数据库 schema 保持 21，接口字段不变。

- **每日堆叠柱图**：按日期堆叠每个用户的用量，支持在 Token 与调用次数之间切换；图例显示每个用户在当前窗口内的合计，用户较多时合并为“其他用户”。
- **图表 + 精确明细**：图表下方保留用户汇总表与可展开的每日明细，图表看趋势、表格核对数字。
- **权限不变**：管理员可查看全部用户或指定用户；普通用户只显示自己的曲线。
- 验证：单元/集成 **222 passed**、REST 冒烟 **152/152**。详见 `docs/版本说明-1.24.1.md`。

## 1.24.0 用户每日用量

本版本在 1.23.0 用量统计基础上补充“每个用户每天的使用次数和总量”，数据库 schema 保持 21。

- **用户每日聚合**：`GET /api/usage/overview` 新增 `user_daily`，按本地时区日期给出每个用户的调用次数、失败次数、输入/输出/总 Token；普通用户只包含自己，管理员包含全部（指定 `user_id` 时只包含该用户）。
- **用户汇总**：页面新增“用户每日用量”表，展示每个用户的活跃天数、调用次数、失败、输入/输出/总 Token、日均调用与日均 Token；下方可展开每日明细（日期、用户、调用、Token）。
- **权限不变**：管理员可切换全部用户 / 指定用户；普通用户只能看到自己的记录，显式查询他人仍返回 403。
- 验证：单元/集成 **222 passed**、REST 冒烟 **152/152**。详见 `docs/版本说明-1.24.0.md`。

## 1.23.0 用量统计

本版本新增“用量统计”，把模型调用、任务 Token 与技能执行消耗统一落库；数据库 schema 从 20 升到 21。

- **模型每日统计**：新增 `llm_usage_events` 逐调用事实表。网关每次实际尝试（含规划、补规划、仲裁、报告、JSON 修复与失败重试）记录模型、来源、用途、状态、输入/输出/总 Token、耗时与任务归属；`model_configs` 与 `user_model_configs` 分别标记为共享 / 个人，页面分开展示。
- **任务 Token**：`GET /api/usage/tasks` 按任务汇总调用次数、输入/输出/总 Token 和参与模型；历史任务没有逐调用事件时回退 `tasks.stats_json`，页面标记“历史汇总”。
- **技能消耗**：按 `task_skills` 统计执行次数、失败、任务数、Token、工具调用、证据数、关联输入和平均耗时；关联输入按任务去重。
- **权限**：管理员可查看全部用户或指定用户；普通用户只能查看自己的调用，显式查询他人返回 403。页面默认近 30 天。
- **界面**：新增“用量统计”页，内联 SVG 堆叠柱图按日展示各模型 Token / 调用次数，附模型明细、每日明细、技能消耗和任务 Token 表；1440px 与 390px 均通过浏览器验收。
- 验证：单元/集成 **222 passed**、REST 冒烟 **152/152**。详见 `docs/版本说明-1.23.0.md`。

## 1.22.0 指定技能、全量证据与执行轨迹

本版本补齐 P1–P3：指定技能诊断、全量证据聚合与执行轨迹，并加固归档与复杂规则回归；数据库 schema 从 18 升到 20。

- **P1 指定技能**：诊断模式改为快速定位 / 深度分析 / 指定技能；指定技能模式只执行一个 ready/degraded 技能，不触发 AI 扩选与补规划。新建任务页、技能卡片和“系统信息 → 任务空间”均可直接发起。
- **P2 全量证据**：任务详情从证据库分页拉全量证据，按“根因 + 技能 + 规则”聚合；支持搜索、技能/根因筛选、查看全部与原文展开；新增根因决策卡，明确必要证据、反证、近邻鉴别和“缺扫描不等于反证”。
- **P3 工具与回归**：助手/MCP 新增 `create_skill_diagnosis`，复用来源任务日志创建指定技能任务，MCP 写模式要求 `confirm=true`；补齐归档安全矩阵、时间窗/多站点/数值边界/CSV/XLSX/多根因冲突回归。
- **执行轨迹**：诊断与工坊记录模型、工具、校验、计划和错误事件；管理员可在“执行轨迹”与任务详情“执行过程”查看。轨迹仅管理员可见。
- **修复**：运行配置 enum 下拉中文标签；助手输入框移除多余 placeholder；xlsx 绝对 rels 路径导致的整表漏检；归档绝对路径成员与损坏归档处理。
- 验证：单元/集成 **217 passed**、REST 冒烟 **150/150**、1.21.0→1.22.0 离线升级通过。详见 `docs/版本说明-1.22.0.md`。

## 1.21.0 管理与诊断体验 / 工坊重构 / 证据增强

本版本按四阶段计划完成管理与诊断体验、工坊重构与证据增强；数据库 schema 从 17 升到 18，新增按用户隔离的 `mcp_tokens` 表。

- **M1 阻断修复**：enum 运行参数保存、弹窗拖选误关闭、规则确认状态、错误定位、步骤/门禁一致性、AI 建议去重。
- **M2 管理与诊断**：系统概览与个人 MCP Token 对全员开放；专家拥有完整诊断/技能库/工坊入口，复核仅限被分配任务；首次登录直接进入工作台；新建诊断可指定技能且只执行所选技能；证据链长路径折叠；技能库可直接发起诊断。
- **M3 工坊重构**：步骤更名与状态统一、移除领域配方入口、规则标签与原始表达式、多文件/文件夹案例、后台任务详情、案例术语统一。
- **M4 证据增强**：恢复验证真实文件上传并记录 SHA256；工坊规则可用现有日志试跑；归档新增 xz / zst / 7z。
- **知识库**：沿用 1.20.3 的 BM25F、意图路由与多轮改写，不引入向量方案；评测入口 `python3 tools/eval_kb_retrieval.py`。
- 详见 `docs/版本说明-1.21.0.md`。

## 1.20.2 升级助手 NoNewPrivileges 修复

本补丁修复 systemd 部署下网页离线升级必然失败的问题；1.20.1 的助手/MCP 接口和 schema 不变。

- **根因**：主服务单元原先生成 `NoNewPrivileges=true`，内核会忽略 `sudo` 的 setuid 位，`bbu` 用户无法通过受限 sudo 启动 root 升级助手，页面报 `sudo: 有效用户 ID 不是 0`。
- **修复**：新安装单元不再写 NNP；已有安装升级时自动备份旧单元并移除该行；`deploy/fix_upgrade_helper.sh --fix-nnp` 可用于不换包的现场修复。
- **systemctl 路径**：升级助手触发器不再写死 `/usr/bin/systemctl`，兼容 `/bin/systemctl`；升级脚本会同步刷新触发器。
- **异步启动**：触发器使用 `systemctl --no-block start`，升级助手停服时不会再切断网页的 apply 请求；页面改为轮询任务状态。
- **边界**：sudoers 仍只允许固定触发器，服务仍启用 `ProtectSystem=strict`；若 sudo 所在挂载确实 `nosuid` 或容器禁止 setuid，则只能改挂载或手工升级。
- 详见 `docs/版本说明-1.20.2.md`。

## 1.20.1 真实模型助手评测修复

本补丁用真实模型完成悬浮助手智能化评测，并修复两个工具参数问题；不改变 1.20.0 的接口、权限和数据库 schema。

- **真实评测**：`deepseek-flash` 连续 2 轮 13/13；覆盖文档问答、任务/通知/审计/外部 IM、权限隔离、敏感信息拒答、写操作待确认和多轮上下文。
- **工具参数约束**：规划提示直接提供每个工具的完整参数 Schema（枚举、必填、最小值/最大值），避免真实模型猜错 `scopes` 或 `limit`。
- **审计失败检索**：中文“失败/错误”会自动映射为 `failed/error` 动作或 `detail ok=false`，不再因英文动作名漏掉失败记录。
- 详见 `docs/版本说明-1.20.1.md`。

## 1.20.0 平台助手与 MCP Server

本版本新增“全局悬浮小助手”和“本机 MCP Server”，不改变原有诊断主链路，不迁移数据库。

- **悬浮助手**：页面右下角固定入口，所有登录用户可见；支持平台文档问答、站内通知、任务事件和外部 IM 消息检索。模型只负责选工具和组织答案，数据与权限在统一工具层校验；没有可用模型时自动降级为本地文档/消息检索。
- **管理员运维**：管理员可查系统状态、磁盘、备份、升级记录、用户/模型/通知配置；写操作先进入 10 分钟待确认，确认后执行；升级/回滚、用户/模型变更等高风险动作额外校验管理员密码。
- **MCP Server**：`POST /mcp` 提供标准 JSON-RPC 2.0，支持 initialize、ping、tools/list、tools/call、resources/list、resources/read、prompts/list、prompts/get、批量请求；`GET /mcp` 返回 405，不提供 SSE。默认只允许 loopback，Token 只存 SHA-256，支持 read/admin 模式、轮换和撤销。
- **统一 20 个工具**：文档、消息、任务、技能、系统、备份、存储、升级、用户、模型、通知共 20 个工具；MCP read 模式约 12 个只读工具，admin 模式只额外放行低风险写工具，升级/回滚、用户/模型变更永不通过 MCP 执行。
- **权限分级**：普通用户只能问答、查自己的通知/任务事件、改自己的通知偏好；审计和运维工具仅 admin 可用。
- **接口**：新增 8 条 `/api/assistant`、`/api/mcp` REST 接口和 2 条 `/mcp` 路由；REST 从 **136** 增至 **146**，数据库 schema 保持 **17**。
- **验证**：全量 Python **182/182**、REST **146/146**、新增助手/MCP 单测 **7/7**、真实 Chromium UI 冒烟通过（桌面、375px、MCP Token 一次性展示）。
- 详见 `docs/版本说明-1.20.0.md`、`docs/使用手册.md` 第 10 节和 `docs/部署手册.md` 的 MCP 章节。

## 1.19.2 工坊流式调用修复（历史）

本版本在 1.19.1 基础上继续修复真实模型工坊的剩余问题，不更换模型。核心变化是技能工坊四个阶段改用 OpenAI SSE 流式读取，避免上游代理在约 30 秒空闲时断开，同时保留非流式回退。

- **流式调用**：`AIGateway.complete(..., stream=True)` 支持 OpenAI SSE；workshop 的 extract、review、repair、review-final 全部启用。响应不是 SSE 时按普通 JSON 解析；明确不支持 stream（400/404/405/422）时自动回退非流式。
- **真实模型复测**：看门狗场景四阶段全部在 30 秒内完成——extract 15.46s（5,893 input / 3,364 output）、review 27.62s（5,595 / 4,260）、repair 22.66s、review-final 6.65s。此前同样场景多次在 review/review-final 约 30 秒 `IncompleteRead`。
- **质量结论没有被放松**：最终评审仍报告“E1–E4 全部 verified=false”这一条 error，确定性审计通过但按现有门禁不会把该技能当成已验证发布项；流式修复只解决传输，不伪造证据。
- **可观察性**：attempt 记录增加 `stream` 标记，记录实际传输方式（回退后为 `false`）；流式 usage 缺失时仍做保守 token 估算。新增 `tests/live_model_probe.py` 可复现真实流式探针，不污染主库。
- **测试**：全量 Python **175/175**、REST **136/136**、工坊十场景 **10/10**、工坊生成 **20/20**、三配方回归全部通过；`test_ai_protocols.py` 新增 SSE、完整 message、失败流重试和“不误回退非流式”测试。
- **安装升级**：1.19.2 Linux/一键包均已完成临时安装与健康检查；1.19.1→1.19.2 升级保留管理员密码和配置，回滚演练通过。
- 当前包数据库 schema **17**、REST **136 条**；详见 `docs/版本说明-1.19.2.md`。

## 1.19.1 模型调用优化与缺陷修复（历史）

本版本不更换模型，只调整技能工坊的模型调用协议、上下文和回退语义，并修复 1.19.0 之后验收发现的接口与状态问题。1.19.0 的离线包保持原路径与原哈希。

- **模型上下文裁剪**：新增 `model_spec()`，模型只接收业务字段，递归剥离 `provenance`、`diagnostic_reasoning`、`recipe_candidates`、`hard_fields` 等内部元数据；页面与溯源仍使用完整 `public_spec()`。看门狗样本的模型 prompt 从 46,383 字符降到 16,506 字符，真实 extract input 从 15,184 tokens 降到 5,945 tokens。
- **结构化输出按能力路由**：只有明确声明支持 strict schema 的端点才发送原生 `json_schema`；普通 OpenAI 兼容端点改用 `response_format={"type":"json_object"}`；不支持 JSON 模式时退回提示词约束，本地严格 Schema 校验始终执行。
- **评审与修复拆小**：首次评审输出上限从 8k 降到 5k，最终复核改为只带上一轮 error、修复后 spec 和确定性质量，输出上限 3k，不再重复整段原文。
- **评审结论不被回退覆盖**：修复失败且已有评审结果时，使用确定性审计与评审结果合并，保留评审 error；新增 `extract_status / review_status / repair_status`，前端显示“提取成功，评审/修复未完成”。
- **失败调用可观察**：`AIGateway.complete()` 支持 `attempt_sink`，失败调用记录 stage、模型、延迟、错误、HTTP 状态；没有 usage 时记录 `estimated_input_tokens`。
- **验证**：全量 Python **171/171**、REST **136/136**、工坊十场景 **10/10**、工坊生成 **20/20**、三配方回归全部通过；通用时钟真实模型完成提取 → 评审 → 修复 → 最终评审，质量分 92。看门狗和 USB 仍受上游约 30 秒断连影响，失败已记录但真实模型不是稳定 3/3。
- 当前包数据库 schema **17**、REST **136 条**；详见 `docs/版本说明-1.19.1.md` 和 `docs/技能工坊模型调用优化-2026-11-26.md`。

## 1.19.0 缺陷修复（历史）

本版本不新增 R3–R6 功能，只修复 1.18.0 逐条验收确认的确定性缺陷，并正式升版，不覆盖 1.14.0 / 1.18.0 发布包。

- **报告数字必须能在证据原文定位**：`validate_claims()` 不再用 claim 文本自证；数字/时间按 `evidence_id` 回到证据原文匹配，无引用直接报 `missing_evidence`，claim 校验失败时不再调用报告 writer。
- **默认 shadow 不再放行安全关键违规**：审批缺失/不匹配、角色/站点/设备越权、路径越界、Schema 非法、网络未受控等命中后直接 `allow:false`；`change/reset/write/delete` 等风险别名正确归一为 high/destructive。
- **人工锁真正保护值**：`merge_specs()` 在规范化/映射前抓取所有 `locked:true` 字段，合并后恢复原值，并记录 `locked_restored`。
- **工坊硬字段来源覆盖率 100%**：支持空白规范化定位、行级回退和可追溯推导；三个 intake 样本 SSD 46/46、USBFLASH 30/30、watchdog 30/30。
- **恢复验证必须有后验证据**：`recovered` 要求同一任务/对象 + 恢复后新增独立文件 ≥1；`observe()` 自动写 `recovery.verification` EvidenceEvent，学习门禁增加 `same_case`。
- **假设状态进入运行链路**：planner 候选生成 HypothesisState，执行中维护 supported/weakened/refuted，仲裁后确认 chosen；会话事件新增 `tool_started/tool_finished/evidence_added` 和 budget 快照。
- **补充文件按 glob 选择性失效**：解析 zip 成员和技能声明的 `globs`，只失效真正命中的技能，响应返回 `invalidation_reasons`。
- **供应链 `verified` 语义收紧**：只有 `manifest_status=verified` + `verification.status=case_regression` + 回归通过才算 verified；tamper/bad_signature 一律 incompatible，ZIP 导入仅 quarantine，不冒充案例回归。
- **同根因新变体不再丢弃**：同根因但源输入摘要不同时写入 `same_root_variant` 候选，供管理员审批。
- **OpenAI 原生结构化输出与预算原子预占**：发送 `json_schema` + `strict:true`，不支持时逐级降级；Budget 增加 `reserve/settle/release`，`check()`/`snapshot()` 计入 pending，避免并发超卖。
- 数据库 schema 保持 **16**，REST 接口保持 **135 条**；单元测试 **144/144**、HTTP 全量冒烟 **135/135** 通过。1.18.0 和 1.14.0 发布包保持原路径与原哈希，1.19.0 使用独立离线包。详见 `docs/版本说明-1.19.0.md`。

> 本轮只修复确定性缺陷；报告中的 Agent Loop/DAG、真实 hidden 集、任务卡、规则变异矩阵、组织离线签名等结构性能力仍未完成，详见 1.19.0 版本说明的“仍未完成”章节。

## 1.18.0 功能更新（R3–R6）

- **工具授权与沙箱**：每个技能暴露 ToolManifest（输入 Schema、可写根、网络声明、风险等级）；`shadow` 只记录 `would_deny`，`enforce` 才阻断。高风险工具必须审批，审批绑定任务、工具、版本、参数哈希、会话 revision、状态和过期时间。沙箱采用环境变量白名单、RLIMIT 和 Python audit hook，不是容器/seccomp 级隔离；当前宿主机 `unshare -n` 不可用，边界已在版本说明中写明。
- **模型网关与导入供应链**：OpenAI 兼容 endpoint 用 16 token 探测 `response_format=json_object`，结果按 endpoint 缓存 24 小时；探测不发业务数据，能力未知时保守降级并始终执行本地严格 Schema 校验。外部技能导入先 quarantine、流式限流解压，拒绝路径穿越/符号链接/加密包/压缩比超限，再计算清单和 HMAC-SHA256 签名，失败返回 422。
- **有界诊断 Agent**：固定动作空间 `select_registered_skill / execute_skill / request_more_data / replan_once / stop`，校验波次、技能总数、重规划次数、技能注册和 gate；禁止 shell、任意 URL、自动发布等开放动作。默认 shadow 记录轨迹，bounded 模式才作为门禁。
- **混合检索、反证与校准**：经验检索由纯词法升级为词法 + 字符三元组向量混合打分，过滤失效经验并返回相似反例；根因决策卡区分“缺扫描”和“已扫描但反证”，缺扫描统一记为 `missing_data`。置信度校准只用 dev 集，样本不足显示 `uncalibrated`，不展示伪精确概率。
- **可信报告**：报告生成结构化 claim，校验根因/动作必须有本任务证据引用、数字/时间有支撑、未支撑断言回退确定性模板；报告附“结构化报告校验”说明。
- **工坊字段溯源**：故障规格的每个关键字段记录 JSON Pointer、来源 revision、原文区间、quote hash、归一化方式和验证状态；支持人工锁字段，模型修复不能静默覆盖。阈值解析为指标/比较符/数值/单位/时间窗/版本范围。
- **案例矩阵与持续学习**：按技能/根因/版本聚合案例矩阵，签名同时绑定技能、构建 SHA、结果集和兼容范围；专家改判/驳回生成幂等增量候选，需管理员审批，不直接改技能。
- **只读恢复验证闭环**：现场回传恢复前后只读证据，系统做确定性前后对照（`recovered / still_fault / new_fault / inconclusive`），不改设备、不发命令。只有“恢复状态 + 独立证据文件 + 专家复核”三项同时满足才允许进入学习。
- 数据库 schema 升到 **16**，REST 接口 **135 条**；单元测试 **140/140**、HTTP 全量冒烟 **135/135** 通过。1.14.0 发布包保持原样，不覆盖、不静默替换；升级请使用独立 1.18.0 包。详见 `docs/版本说明-1.18.0.md`。

## 1.14.0 功能更新

- **数据出域治理**：工坊默认只发送筛选后的最小摘录，诊断默认只发送脱敏证据视图；完整 `source_text`/原始日志不再进入模型请求。审计只落长度、行数、SHA-256 和策略版本，不落正文。
- **统一证据与会话**：schema 升到 12，新增 `evidence_events`、`session_events`、`task_supplements`。旧技能证据旁路适配进统一证据库，保留文件/行号/时间戳/摘录/内容哈希/原文件 SHA-256；诊断会话按 revision 记录假设、gap、补充输入和终态摘要。
- **同任务补证续跑**：任务 `needs_data` 时可在详情页补交日志或回答阻塞问题，校验 `expected_revision` 与 `Idempotency-Key + request_hash` 后在同一 task ID 下重新排队；历史结果不删除，按 `invalidated` 过滤后只重跑受影响技能。新增 5 条 REST 接口。
- **评测泄漏门禁**：新增任务卡 Schema、跨 split 精确/近重复泄漏检查、hidden 最低样本与 F1 门禁、Wilson/bootstrap 95% 区间；hidden 有泄漏或样本不足时拒绝运行，不使用合成案例补齐。
- REST 接口从 109 条增至 **114 条**；数据库 schema 12；单元测试 **115/115**、HTTP 全量冒烟 **114/114** 通过。详见 `docs/版本说明-1.14.0.md`。

## 1.12.0 功能更新

- **用户组织字段收敛**：用户与邀请彻底移除「班组 / 站点」，只保留「领域」；个人中心、用户详情和复核专家选择不再显示班组/站点。数据库 schema 升到 11，旧库通过幂等 `ALTER ... DROP COLUMN` 删列并保留领域与账号数据。
- **历史备份管理**：系统信息新增「备份」标签页，扫描 `STATE_DIR/backups` 下的升级快照、迁移恢复前备份和手工备份，展示创建时间、类型、版本、大小与文件数；管理员可删除不再被升级任务使用的历史快照。新增 `GET /api/backups`、`DELETE /api/backups/{name}`。
- **免邀请码自助注册**：运行参数新增 `allow_self_register`（默认关闭，保存后即时生效）。开启后登录页出现「自助注册」入口，`POST /api/auth/register` 固定创建普通工程师账号，支持填写显示名和领域；不自动登录，也不覆盖已存在的邀请账号。
- REST 接口从 106 条增至 **109 条**；数据库 schema 11；单元测试 **94/94**、HTTP 全量冒烟 **109/109** 通过。详见 `docs/版本说明-1.12.0.md`。

## 1.11.0 功能更新

- 系统信息拆为「概览 / 运行参数 / 任务空间 / 迁移 / 升级」五个标签页，可快速跳转并延迟加载对应接口。
- 任务空间支持分别释放「原始输入」和「中间产物中的解压文件」；解压文件释放只删除 `tasks/<id>/skills/*/unpacked`，报告、证据和案例保留。
- 技能工坊五个步骤改为标签页，可随时返回任一步修改；支持改原文后重新解析，后续规则审查、案例回归和构建结果自动失效并重跑。
- 技能工坊草稿支持删除；已发布草稿只删草稿记录与工作目录，不回滚已发布技能。新增 `DELETE /api/workshop/drafts/{id}`。
- 备份默认排除原始上传、案例输入、解压目录和运行临时目录；报告、证据、技能与案例定义仍保留。
- 用户管理编辑账号时可维护「班组 / 站点」，个人中心实时读取，不再显示旧值。
- 保留 1.10.2 的内联 SVG 尺寸修复与 1.10.1 的 favicon、静态资源 404、备份解释器修复。
- 数据库 schema 仍为 10；单元测试 88/88、HTTP 全量冒烟 106/106 通过。详见 `docs/版本说明-1.11.0.md`。

## 1.10.1 补丁

- 修复浏览器标签页缓存旧 Flask favicon：新增品牌 `favicon.ico`，前端加版本参数刷新旧缓存，缺失静态资源改为 404，不再回退 `index.html`。
- 修复 Ubuntu 16.04 上 `backup.sh` 因系统 Python 3.5 触发 `SyntaxError`：优先使用 `/opt/bbu-python313` 或 `/opt/python3.13`，校验 Python ≥ 3.7，并让升级脚本显式传递 `PYTHON_BIN`。
- 不改变数据库 schema 和 REST 契约；1.10.0 可直接覆盖升级，1.8.0/1.9.0 仍按原迁移路径升级。

## 1.10.0 本轮变化

- 用户可见“病例”统一改为“案例”，数据库表和 API 字段保持兼容；“现场真实案例库”与“技能回归案例”仍是两个对象。
- 新建模型默认公司 MaaS，支持“测试当前填写值”不落库；新建诊断取消人工标题，由 AI 生成 40 字内标题，失败保留确定性占位。
- 任务详情固定为诊断概览 / 证据与技能 / 诊断报告 / 运行详情四个一级分区，完成态默认打开报告，SSE 刷新保持当前页签。
- 用户管理只保留用户名（默认显示名）、角色、领域、初始密码；管理员可设 9 位一次性密码，首次登录强制改密。
- 技能工坊改为五步引导、后台任务和真实阶段进度；支持自动 name/title、案例列表与覆盖、模型失败提示和重试。
- 系统信息新增全量并发/预算/容量在线编辑、保存并重启、仅重启；新增任务空间统计与选择性释放，保留报告和证据（1.11.0 起可单独释放解压文件）。
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

API Key 使用 `data/master.key` 派生密钥加密后存 SQLite，只在服务端解密，前端只显示掩码。可给同一配置勾选多个角色：`default`（兜底）、`planner`（生成动态计划）、`router`（老部署的规划回退）、`arbiter`（冲突仲裁）、`writer`（报告研判）、`workshop`（技能起草）、`assistant`（悬浮助手工具规划与总结）。同一角色多个配置按 `priority` 从小到大尝试，失败自动切换下一个。

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
# 产物：dist/bbu-fault-platform-1.24.5-offline.tar.gz
```

拷贝到内网服务器后：

```bash
tar -xzf bbu-fault-platform-1.24.5-offline.tar.gz
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
python3 tests/test_workshop_ai_contract.py
python3 tests/test_workshop_scenarios.py
python3 tests/workshop_regression.py
python3 tests/reasoning_e2e.py
python3 tests/diagnostic_benchmark.py
python3 tests/e2e_smoke.py
python3 tests/api_full_smoke.py
```

`test_workshop_ai_contract.py` 覆盖工坊模型 prompt 的上下文裁剪、评审失败不丢错误、失败调用 attempts 记录；`test_workshop_scenarios.py` 用临时目录构造十个不同工坊场景，覆盖三类领域配方、四条通用故障资料、信息不完整阻断、配方/原文失效和完整构建发布闭环。`e2e_smoke.py` 会在临时目录启动独立实例（不污染 `data/`），验证登录、模型配置、上传日志、AI 计划、单/多 Skill 执行、一次补规划、历史经验检索与 HTML 报告生成。`test_planner.py` 覆盖计划校验、gate、补规划停止条件和经验检索。`workshop_regression.py` 把三类整段资料编译成技能，并用覆盖全部根因的合成正例/反例跑 `build_child_skill.py` 回归。`api_full_smoke.py` 逐条覆盖全部 136 个 REST 路由，并验证 401/403/404/409/400、CSRF、管理员/工程师权限、SSE，以及“未验证禁止发布、正反例回归后才可发布”。

## 关键约束

- 日志包仅接受 `zip` / `tar.gz`（含 `tar`、`tgz`、`tar.bz2`、单文件 `gz/bz2`）；默认单包上限 2048 MB，解压总量上限 8192 MB、文件数上限 200000。
- 原始日志不会发给模型；只发送抽取后的证据片段与技能目录。
- 输出是现场辅助诊断结论，不替代厂家规范；涉及复位、升级、割接等操作须按现网流程审批。
