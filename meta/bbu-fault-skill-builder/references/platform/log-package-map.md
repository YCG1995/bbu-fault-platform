# BBU 日志包结构速查（Log Package Map）

> 访谈第 4 步定位"波及的 log"时查阅。内容来自三个示例包 + 一个工装测试日志的实测结构；实际包随机型/版本有差异，**以案例包实测为准**，规则路径一律用 glob 描述并在案例上验证。

## 目录

1. 三类采集包总览
2. 归档 1：设备侧全量日志（tar.gz，BBU/OSS）
3. 归档 2：SDR 网管一键采集（zip）
4. 归档 3：ITBBU DCM 深度采集（zip）
5. 工装测试日志（CloudMAT 纯文本）
6. 通用文件类型与解析方式
7. 命名规律与编码注意事项

## 1. 三类采集包总览

| 维度 | 设备侧 tar.gz | SDR 一键采集 zip | DCM admin 采集 zip |
|---|---|---|---|
| 典型设备 | BBU/OSS（含 PG 数据库） | SDR（BS8700，LTE TDD） | ITBBU（5G NR / 毫米波） |
| 标识示例 | `739447200350_时间.tar.gz` | `905_926852_站名.zip` | `DCM_admin_时间.zip`，内部 `1_991102` |
| 顶层特征 | `logs/`、`mnt/flash/` | 中文目录名（当前告警/单板状态…） | 英文模块目录（BspLog/OamLog/…） |
| 通用汇总 | 无（原始日志为主） | summary.json + fm-* CSV + R_*.csv | 同左，另有 InnerMetrics、CoreDumpLog |

三类包与工装日志共有的排查入口（若存在，优先看）：`当前告警/fm-active_*.csv` → `单板状态查询/*query_board_running_info*.csv` → `单板异常信息/*ExcpData*.log` → 再深入模块日志。

## 2. 归档 1：设备侧全量日志（tar.gz）

关键路径与 glob：

| 排查目标 | 路径 glob（相对解压根） | 说明 |
|---|---|---|
| PostgreSQL 数据库 | `**/logs/PG_LOG/rdbs_svr_local_log/*.log*` | 本地 DB 服务日志（.gz 轮转） |
|  | `**/logs/PG_LOG/postgresql-*.log.*.gz` | 按星期 + 序号轮转，N 越大越旧 |
|  | `**/logs/PG_LOG/entry_output.log` | PG 容器启动日志，`时间戳:unix时间: 内容` |
| OSS 侧 | `**/logs/OssLog/ushell_debug.log`、`mem*.log`、`mgr_init*.log`、`oomMoni_*.log` | 内存/启动/OOM 监控 |
|  | `**/logs/OssLog/tcpBigPackag*/*.log` | 注意目录名可能少尾字母 |
|  | `**/logs/oss/oss/rdbs-*/` | 容器内 OSS 日志 |
| NF-OAM/NETCONF | `**/logs/nfoam/nfoam*.log`、`monitor_*.log.tar.gz`、`yangrepo_check.txt` | |
| 内核 | `**/logs/kernel/kernel.klog*` | |
| 抓包 | `**/logs/BSP/pkt/*.pcap`、`**/logs/brs/pcap/**/*.pcap` | 二进制，Wireshark |
| PaaS/框架 | `**/logs/litepaas/lpmslave_stdout_*.log`、`**/logs/luban/**/lubanmaster.log*` | |
| 虚机/持久化 | `**/mnt/flash/OamLog/vmp/vmp.log*` | 虚拟机管理 |
| 升级/软件管理 | `**/mnt/flash/1/swm/**/TMP/*.xml`、`*.json`、`vmpVersionReport.log`、`ActiveEnforce.log` | 版本下载/激活结果、状态机文件 |
| 证书 | `**/mnt/flash/1/swm/PkiRoot/**` | CA/CRL/USER 证书链 |

## 3. 归档 2：SDR 网管一键采集（zip）

顶层为中文目录，核心 glob：

| 排查目标 | 路径 glob |
|---|---|
| 采集摘要 | `**/基站配置数据/NeFile-*/summary.json` |
| 活动/历史告警 | `**/当前告警/fm-active_*.csv`；`**/历史告警/fm-history-recently_*.csv`；`**/历史通知/fm-notification-recently_*.csv` |
| 运行/软件版本 | `**/基站运行版本/*running_version.csv`；`**/基站软件包版本/*software_package_version.csv` |
| 单板状态 | `**/单板状态查询/*query_board_running_info*.csv` |
| 单板异常 | `**/单板异常信息/*_mop_*.log`、`*_moe_*.log`、`*-ExcpData-*.log` |
| Shell 回显 | `**/基站Telnet信息/**/*ShellCollRlt_*.log` |
| 数据库配置表 | `**/基站数据库表文件/**/R_*.csv`（约 283 张，`R_` 前缀） |
| BBU 黑匣子/BSP | `**/日志文件*/**/C1_*.bbx.gz`、`C1_bsp*.gz`、`C1_mgr_init.log`、`C1_bsplog.*.gz`、`C1_MCP_*.log` |
| 链路类 | `**/日志文件*/**/C1_bbu_linkinfo.log`、`C1_rudp_break.log`、`C1_softrtc.txt` |
| RRU 日志 | `**/IR RRU日志*/**/*-SysLog.bin`、`-AlmLog.bin`、`-UserLog.bin`（二进制）；`*Rsp.xml`（采集结果） |
| 内嵌备份包 | `**/基站配置数据/NeFile-*/SDR_*.zip`（需二次解压） |

`*_mop_*.log` 行格式：`时间 源文件 L:行 F:函数 [FunID(...), MOI(...), Attr(...), CfgValue(...), LCSValue(...), ValidValue(...)]`，用于 MO 数据一致性问题。

**fm 单独导出的 xlsx 形态**（实测 `fm-active-告警监控-<用户>-<时间><站名>.zip`、`fm-history-所有-*.zip`）：zip 内仅**一个中文名 `.xlsx`**（OOXML）。本次三份样例按 OOXML 实际行分别读出 5、78、10 行，关键列包括网元、告警码名称、告警码、发生时间、告警原因、告警级别、根因分析结果；样例告警 `198092217 / 系统时钟不可用`。`search_evidence.py` 按“行=记录”直接检索；规则 glob 写 `**/fm-active_*` 或 `**/fm-*`（`.csv`/`.xlsx` 通吃），`format` 写 `csv`/`xlsx` 均可。其他常见列：告警源、确认状态、告警 ID、网元类型、板件信息、DN、清除时间等。**不得依赖 worksheet 的 `dimension`、`max_row` 判断记录数**：样例错误声明 `A1`，但实际有多行；必须迭代真实 sheet 行。

## 4. 归档 3：ITBBU DCM 深度采集（zip）

**实测包形态**（2025—2026 三份采集样本）：文件名 `DCM_<用户>_<采集时间>[_<子网>_<网元>_BspLog_<导出时间>].zip`，内部根目录为 `<子网ID>_<网元ID>/BspLog/`（样例为 `6323_2595757`、`6321_2595975`、`6321_2625542`）；三份仅 BspLog 的包分别有 140、74、115 个成员，全量 DCM 另含 OamLog/OssLogVbp/TcfsLog 等模块目录。BspLog 根下固定有 `collectinfo.log`（各采集实例 uri/时间窗/succ|fail，**先看它确认哪些日志没采到**）、各槽位 `*PLAT.EXE_BspLog_<实例>_<序号>_<文件名>` 采集结果（含 `.txt`/`.txt.0N(.gz)` 轮转与 `.bin` 抓包），以及 `BSP_*HWM_APP.EXE_*.tar`、`local_*-rcm_*.tar`、`uds_*.tar` 等需二次/三次解压的嵌套 tar。时钟文件形如 `1-1-3-1-PLAT.EXE_BspLog_0_12_clockstatuslog.txt.02.gz`，同时可能存在于 VBP 槽位散文件与嵌套 BSP tar 内，解析时须按槽位/进程分组，不得混流。

| 目录 | 内容 | 重点 glob |
|---|---|---|
| 顶层汇总 | 与归档 2 同构的中文 CSV/JSON | `**/fm-active_*.csv`、`**/*query_board_running_info_itbbu_*.csv` |
| 采集过程 | 各模块采集结果 | `**/CoreDumpLog/collectinfo.log`（采集 uri、时间窗、succ/fail） |
| BSP 板级 | 复位/时钟/链路/光模块主战场 | `**/BspLog/*.tar`（见下）、`*PLAT.EXE_BspLog_*.txt`、`DPSC-*.log.gz`、`iq_EcpriLink-*.log.gz` |
| 基带 OAM |  | `**/BsoLog/dc-*.log.gz`、`RRU_OAM-*.tar`、`AAX-*.tar` |
| OAM 模块 | 模块名-时间戳-序号.log.gz | `**/OamLog/**/*.log*`（amp/cos_*/ddm/idb/pm_*/smm/usm/weblmt 等） |
| VBP 侧 OSS |  | `**/OssLogVbp/**/*PLAT.EXE_OssLogVbp_*.log*` |
| Shell 采集 |  | `**/ShellLog/*ShellCollRlt_*.txt`、`*-rcm.tar.gz` |
| 内部指标/审计 |  | `**/InnerMetrics/OperationLog/Audit_*.log`（syslog 格式，Host/Action/Result 分号分隔）；`InnerMetricsUPA_*.bin.gz`（二进制） |
| 文件系统/黑匣子 |  | `**/TcfsLog/**/*.log.gz` |
| 软件管理 |  | `**/SwmLog/swm_vmp-*.log.gz`、`vmp_*.tar` |
| 告警 |  | `**/AlmLog/nfamp-*.log.gz` |

### BSP 重点：`BSP_1-1-1-1-HWM_APP.EXE_0.tar`（解压后为 `BSP/`）

| 排查目标 | 文件 | 格式/要点 |
|---|---|---|
| 复位历史（必查） | `BSP/reboot_history` | `last reset[soft reset,0x13],count[本次,历史],at[时间, 距上次开机时长],boot/cpu: 版本` |
| BSP 主日志 | `BSP/BspLog.log` | `"时间", 函数, 行号, 级别, 消息` |
| 上电初始化 | `BSP/BoardInit.log.bak.N(.gunzipped)`（N 越大越旧；`.bak.N` 实为 gzip，解包/检索均按 magic bytes 识别） | |
| 内核 | `BSP/kernel.log.bak.N` | 同左，可能为伪装后缀 gzip |
| VBP 断链快照 | `BSP/VbpBroken*.log` | 端口/链路状态快照，含 `VBP BROKEN LOG BEGIN`、`All Ports Status` |
| S1/TCFS 异常 | `BSP/S1Broken.log.bak.N`、`BSP/TcfsBroken.log` | |
| 时钟 | `BSP/clockstatuslog.txt(.0N)(.gz)`、`clockdatalog.txt`、`clockocxolog.txt`、`clockhwinfolog.txt`；BspLog 根下另有各槽位 `*PLAT.EXE_*clockstatuslog*`（长周期 VBP 时钟） | `Clk[n]时间 Ref= sts= ... 1588=,Gps=,Bch=` |
| 卫星 | `BSP/gnsssvinfo.txt(.N.gz)` 文本；`gnssoriginalmsg.txt` 二进制 | |
| 光模块 | `BSP/OptMoni.log` | |
| 文件系统/MCU | `BSP/fsck_info.log`、`McuLog.log` | |
| 抓包 | `BSP/pkt/*.pcap` | Wireshark |
| 不可直读 | `BspMoni.log.bak.N`、`verprotect.log.bak.N`、`uartlog_N.gz`（后者解压后多为调试密文） | 专用工具，标注人工解析 |

容器日志：`uds_*HWM_APP.EXE_*.tar`、`local_uds-*rcm_0.tar`、`local_dps-*rcm_0.tar` 内为 `uds/`、`local/` 目录下编号 `N(.tar.gz)` 的日志包。

### 4.1 时钟日志专项（BBU 主控：SDR / ITRAN / VSWe）

时钟类故障必须先弄清包层级，不同采集形态嵌套深度不同。

**SDR 包层级**：

| 形态 | 嵌套 |
|---|---|
| a | `xxOMMB_NE_LOG.zip` → `xx.gz` |
| b | `xx.zip` → `xxOMMB_NE_LOG.zip` → `xx.gz` |

**ITRAN 包层级**：

| 形态 | 嵌套 |
|---|---|
| VSW-a | `xx.zip` → `BSPxxHWM_APPxx.tar` → `xx.gz` |
| VSW-b | `BSPxxHWM_APPxx.tar` → `xx.gz` |
| VBP | `xx.zip` → `xxPLATxxclockstatuslogxx.gz`（VBP 单板时钟日志直出） |

**时钟文件清单（mList，10 类）**：`gnsssvinfo`（卫星视图）、`clockdatalog`、`clockstatuslog`（主日志）、`gnssspecinfo`、`gnssoriginalmsg`（UBX 二进制报文，见下）、`clockhwinfolog`、`clockocxolog`（OCXO 黑匣子）、`gnssmovinfolog`（移动检测）、`gnsscrosscnomsg`（交叉载噪比）、`inputclock`。

**轮转合并**（`parse_clock.py <目录> --merge` 自动完成）：按 mList 基名 rglob 检索；按 **(父目录, 槽位前缀, 进程形态)** 分组——同目录下 `1-1-3-1-*` 与 `1-1-4-1-*` 槽位、VBP 中 `*PLAT*clockstatuslog*`（PLAT 进程）与 `*Bsp*clockstatuslog*`（BSP 进程）各自独立成流，多分组输出键为 `基名@组标签`；文件名中的采集序号/时间戳数字段归一化后视为同一流；组内按轮转序号 `.0N` 倒序拼接（实测 DCM 包 **无后缀 `.txt` 为当前文件排最后、`.0N` 越大越旧**，合并结果为最旧→最新）；VSW 散文件中名称含 `all`/`db` 的排除；`.gz`/`.bz2`/`.gunzipped` 与解压后副本去重（保留带压缩后缀版本）；`.bak.N` 等伪装后缀 gzip 按 magic bytes 读取；损坏的 gzip/**bz2** 单文件跳过不中断。`.gz` 与 `.bz2`（含 `.tar.bz2`）均由 unpack_logs / parse_clock 透明支持。

**时钟日志行格式**：`Clk[XXX]YYYY-MM-DD,HH:MM:SS data...`（逗号分隔时间）。`XXX` 为 CLK 索引，关键语义：

| CLK | 含义 | 排查点 |
|---|---|---|
| 000 | DA 历史 | Ref/sts/Mp/cFCV/RFCV/Tick（+Smoo/T/rDa/Ms，见 VSWe 手册） |
| 001 | GNSS 接收机状态 | Ant(1正常/128短路/129开路)、LockGps/LockBd、FixType、Agc、AveCno |
| 005 | PPS_TOD | UartErr、PpsStat、SerialSts（+MasterSerialNo/MasterMEID 上级站点） |
| 011/012 | SyncE / SyncE+1588 | DegVal、SsmQL、Pdv、frq（SsmQL 2=PRC 正常） |
| 016 | SyncE 质量 | `Clk[016]` 后接 Ref MPhase Degrade CurDa RecDa |
| 021 | GNSS 质量（旧版位置格式） | Ref Lock FixT FixF Agc MPhase（+APhase CurDa RecDa） |
| 022 | PPS+TOD 详情 | Ref MPhase APhase CurDa RecDa |
| 023 | 1588 分析 | Pdv(μs)、TE(ns)、PllSts（+Sts 0停止/1运行/2暂停） |
| 024 | LINE_CLK 测试 | 线路类时钟 |
| 025 | SYNCE 测试 | SyncE 详细 |
| 026 | CASCADE_CLK 测试 | 级联时钟 |
| 027/028 | SyncE+1588 质量 | PllStat、MPhase、cFCV、rFCV、Toffset（+T1/T2/T3/T4 时间戳） |
| 029 | RRU GNSS 质量 | Rack Lock RruFn BbuFn RPhase MPhase APhase CurDa |
| 031 | RRU GNSS 消息统计 | Type(0首收/1丢包/2任务阻塞) Id FstRt Exp Rcv Snd RackNo |
| 032 | 时钟告警 | `ref:X <AlarmType> -a/-r` |
| 033 | 参考源切换 | BetId/CurId、Ref sw Tid、Ref sw end |
| 034 | GPS 跳周 | FstJumpWeek/LstJumpWeek |
| 035 | CLK_ACTION | Block/Unblock + 1588/Gps/Bch 检测开关 |
| 036 | 参考源差异检查 | Flg(-1异常/2正常)、Dif(ns)、F(ppb)；clockdatalog 含 RecentDac/OC jump/Ref Block Flg 行 |
| 037 | SDR1588 ATR | Sta CurDa RecDa Toft Flr Ori + 指示码 BIT0-7 |
| 038 | 1588 时间戳 | `[T%hu]SeqId=,Ts=,Cf=`，仅相位同步打印 |
| 041 | PPS 时间差 | PS、Dif(us) |
| 084 | GNSS 移动 | Ref/sts/Mp/cFCV/Lat/Log |
| 085 | 主备相位差 | Dif(ns) 突变 + CLK036 Flg 定位异常参考源 |
| 087 | Toffset 调整 | Adj Toffset:-xxxx ns |
| 089 | TOD | Rcv TOD vs Local TOD 差值 |
| 092 | 帧号 | FnDif、sw/fpga 帧号差 |
| 093 | 时钟配置 | Wm/RefSw/SycE/Hold/1588Dly |
| 094 | GNSS 接收机复位原因 | ReasonId 1-15（12=伪GNSS保持24h后复位、13=相位跳变复位） |
| 095 | OCXO 初始化 | Kf/fDA/maxVol |
| 096 | I 值质量 | I=0x... bitmask（按 Ref 分版本） |
| 097/098 | 保持控制/参数 | F、Temp、HT、FD |
| 099 | 时钟状态 | Lock/Loss/Drift bitmask、ClkHealth（VBP 32bit / VBPd1 7bit） |
| 100 | OCXO 黑匣子 | cur_fcv/max_fcv/avg_fcv_locked(99999.99=未锁定)/ref_sts/active_ref/avg_temp |

> 本表是 **CLK 索引语义的唯一来源**（每个索引是什么、排查什么）。字段枚举/阈值/位定义见 `assets/shared/clk-field-dictionary.yaml`；VSWe 专属行格式（000 的 Smoo/Ms、036 clockdatalog 子行、085 Drift 分支、099 位格式）见 `references/domains/vswe-clock-maintenance.md` §5，不在此重复；VBP `ClkHealth` 位定义见字典 `CLK099_CLKHEALTH_VBP`。

**复位假时间（fix_timestamp）**：单板上电初期时间退到 `2021-01-01,00:00:xx`，同步后才恢复真实时间。`parse_clock.py --fix-timestamp`（默认开启）基于 `tick=` 值差校正该段时间。**分析时序前必须先修复**，否则复位段事件会被误排到 2021 年。

**UBX 二进制报文**：`gnssspecinfo` / `gnssoriginalmsg.txt` 内为空格分隔的十六进制字节串，`B5 62 0A 1D ...` 为 u-blox SPEC 频谱报文（class=0x0A id=0x1D），payload 头 12 字节：`rang(U1) type(U1) specLen(U2) freOffect(I4,Hz,有符号) fre(U4,Hz)`，后接 `data[specLen]`。`parse_clock.py` 对 CLK099 行内的该报文自动解析为 `_ubx` 结构；`gnssoriginalmsg` 原始记录仍默认登记为二进制。

**GNSS 欺骗识别**：`gnsssvinfo` 每行含 15 字段状态头（9 字段旧格式自动补齐：Ref/View/Lock/Fix/AGC…/Spoofing/Drift）+ 卫星记录（5 值 `prn used ele azi cno` 或 7 值加 `cno1 cno2`），PRN 按 NMEA 段映射 GP/GL/GA/BD/QZ。`parse_clock.py --merge` 自动解析并按 SatType 分组检测：多星 C/N0 一致高簇（窗口 2dB）+ 仰角跨度 ≥30° + 分离间隙 ≥10dB 或主导占比 ≥80%（簇均值 ≥35dB），仰角-C/N0 正相关 r>0.3 排除。阈值与算法见 `assets/shared/clk-field-dictionary.yaml`。

**解析器其他能力**：一行含多个 `Clk[xxx]` 自动拆分；`inputclock` 无 `Clk[]` 前缀行（`10ms=`/`RefClk=`）归 CLK099；日期时间中的冒号保护（不误判为键值分隔符）；多词键名（`Last Time`→`Last_Time`）；`Ant=1(  1)` 取括号前值、`OcJmp[0]`→`OcJmp=0`；CLK099 `XO=a/b/c/d`、`OC=...`、`Temp=in/out` 斜杠拆分；无关键词位置格式 CLK021（9 值）、CLK027/028（5 值）、CLK095（3 值）、CLK100 GNS/GPS 卫星行；关键字段阈值校验（状态枚举/绝对范围，异常入 `anomalies`）。

## 5. 工装测试日志（CloudMAT，纯文本）

文件名：`<条码>_<开始时间>_<结果>_<时长秒>@<槽位>_.log`，UTF-8 纯文本。

- 行格式：`行号 轮次 时间戳 【级别】 模块:内容`；级别：普通/数据/调试/平台/告警/错误。
- 固定检索锚点：
  - 总体结论：`CloudMAT:测试结果`、`测试时长`
  - 失败项：`TestItemResult.Fail`、`第N轮结束，结果：`
  - 失败原因：`通过标准`（实际值 vs 标准值）、`保存失败参数表`
  - 测试项全集：`itemList`（83 项）
  - 汇总位图：`getItemResultInfo`、结果位图（每位对应 itemList 一项，1=失败）
  - 拷机表：`保存拷机表数据为`（CycleCount / Res / FailureTime / SlotID / Remark1 高温有效时间）
- 定位顺序：总体结论 → 失败项清单 → 失败项的"通过标准/保存失败参数表" → 位图与 itemList 对位 → 失败项接口调用返回值（`调用:...接口,参数:[...],返回值:[{'result': ...}]`，由 `parse_cloudmat.py` 提取为 `interface_calls`）。

## 6. 通用文件类型与解析方式（统一分类学）

默认策略：明文/压缩文本自动检索；二进制与密文**默认不解析**（只登记，等用户提供格式描述再支持）。编码 auto 链为 UTF-8(含 BOM) → GB18030 → Big5 → latin-1。

统一由 `log_format.py` 分类（名称权威层 → magic bytes → 已知二进制后缀 → 内容层 NUL/编码链探测），**后缀只作参考、以内容为准**（`.txt` 实为二进制结构体、`foo.dat.gz` 实为文本均按内容路由）。盘点任意包：`python3 scripts/log_format.py <目录|zip> --json inv.json`。

| kind | 典型形态 | 处理方式 |
|---|---|---|
| `empty` | 0 字节文件 | 登记忽略 |
| `archive` | `.zip/.tar/.tar.gz/.7z/.rar`（含伪装后缀） | unpack_logs 递归解包（层数/体积保护） |
| `compressed_archive` | gzip/bz2 内层是 tar/zip（如 `.tar.gz`、`BspIqLog.tar.gz`） | 解压后继续递归 |
| `compressed_text` | `.log.gz/.bbx.gz/.csv.gz/.klog.gz`、`.bak.N`（伪装后缀 gzip）、任意后缀的 gz/bz2 文本 | 透明解压后逐行检索；unpack 对伪装后缀另产 `*.gunzipped` 副本，原件/副本都可检索 |
| `compressed_binary` | gzip/bz2 内层为二进制（实测 `boot_ker_log_N.txt.gz`、IQ/射频二进制压缩件） | 登记 opaque，不解析 |
| `container_doc` | `.xlsx/.xlsm/.xltx`（fm-active/fm-history 网管导出） | unpack 整文件提取不展开；search_evidence 按"行=记录"检索 + 表头列匹配；`.docx` 等其余 OOXML 仅登记 |
| `text` | `.log/.txt/.csv/.json/.xml/.Cfg`（明文形态）；subtype `log`（时间戳行）/`table`（CSV 列）/`text` | 逐行检索；编码链 UTF-8-SIG→GB18030→Big5→latin-1 |
| `ciphered` | `BspMoni.log*`、`verprotect.log*`、`gnssoriginalmsg.txt`、`uartlog_*.gz` | 名称命中即判定，专用工具人工解析 |
| `binary` | `.pcap/.pcapng`（subtype pcap→Wireshark）、`.bin`（RRU SysLog/AlmLog/UserLog、InnerMetricsUPA、DspNCap）、`.exe/.dll/.so`、`.db/.sqlite`、OLE2 旧 Office（`.xls`，需先转换）、`.txt` 实为二进制结构体（实测 `m0_read_special_regs.txt`、`bd_base_clk_desc.txt`、`bsp_boot_ver_*`）、`unknown` 不可读 | 登记 opaque（含 subtype/note），专用工具或需格式描述 |

manifest 对应字段：`file_kind_counts`（分布）、`opaque_files`（kind/subtype/note 明细）、`binary_or_opaque_files`（兼容名表）；search 结果 `skipped_files` 记录"命中 glob 但按分类未解析"的文件。

## 7. 命名规律与编码注意事项

- 网元目录：`<子网ID>_<网元ID>`（如 `905_926852`、`1_991102`、`6323_2595757`）。
- 槽位：`架-框-槽[-子槽]`（如 `1-1-1`、`1-1-9`、`1-1-7-1`、`1-1-1-1-HWM_APP.EXE`）。
- DCM 采集件：`<槽位>-<进程>_BspLog_<实例>_<采集序号>_<原文件名>`（如同槽位三个 `clockstatuslog.txt.0N` 来自不同采集序号，属同一时钟流）。
- 轮转：`模块-时间戳-序号.log(.N).gz`；`.N` / `.bak.N` 越大越旧。
- 汇总类固定前缀：`fm-*`、`*_running_version`、`*query_board_running_info*`、`ne_change_history`。
- 编码：中文目录/文件可能 UTF-8 与 GBK 混杂，部分 `summary.json` 中文乱码；检索关键字优先用英文/数字/告警码等 ASCII 锚点，中文关键字需两种编码都试。
- 采集失败：先看 `collectinfo.log` 中 `COLLECT_*` 与 `succ/fail`，避免把"没采集到"误判为"无此问题"。
