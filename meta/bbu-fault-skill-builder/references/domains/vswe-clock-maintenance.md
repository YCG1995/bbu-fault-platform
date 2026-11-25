# VSWe 时钟维测手册速查（单板域：VSWe）

> 来源：VSWe 时钟维测需求（V4，2021.11）。告警地址表只在 4.1 保留一份，各命令引用。
> **何时读本文件**：现场/网管时钟告警、前台诊断命令（cchelp / ShowDrvData / ClockDiagInfoShow）、CLK 日志字段解释。工装台架测试（ClockMAT、BSP 接口返回值、板内告警位图）读 `clk-fixture-map.md`。
> **单板域约定**：告警地址（0x0cxxx 8 位偏移风格）、cchelp 输出、RANCLI 命令均为 VSWe 主控特有；ITRAN 等其它单板寄存器布局不同（见 `fpga-register-map.md`），不得跨单板套用。

## 1. 排查总纲（四层递进）

1. 告警（§2）：按告警相关章节逐步排查；
2. 网管诊断（§3 RANCLI）：定位成功则跳 5；
3. 前台诊断（§4 cchelp/ShowDrvData 等）：信息不足时取更多信息，定位成功则跳 5；
4. 后台 Log（§5 CLK 记录）：结合历史信息定位。
5. 收尾：硬件问题→维修；外部环境问题→环境整改；上级设备问题→上报待定。

## 2. 时钟告警（12 类：检测原理与排查要点）

| 告警 | 检测原理（门限/时长） | 排查要点 |
|---|---|---|
| 2.1 系统时钟不可用 | 外部源异常超本地晶振可保持时间 / 主控硬件故障 | **伴随其它告警，按伴随告警定位** |
| 2.2 同步丢失 | 晶振压控电压超限定范围且维持 5min；恢复 5min | V4 无压控方案，**无此告警** |
| 2.3 时钟源间相位差过大 | 主用 vs 第一备用相位差超门限且持续 30s；恢复持续 1h | 排除是主用源异常还是备用源异常 |
| 2.4 1588 时钟链路异常 | 20s 周期检测，连续 9 次异常告警 / 连续 6 次正常恢复 | 包延时抖动过大；上级时钟质量不满足；协商不一致；报文收发异常；上级时间异常 |
| 2.5 SyncE 时钟异常 | 异常维持 10min（默认）告警；恢复 5min（默认） | 频偏超门限；使能 SSM 收不到报文；SSM 等级不可用；千兆电口上级配了从模式 |
| 2.6 1PPS+TOD 时钟源异常 | 异常维持 10min 告警；恢复 5min | 串口收不到消息；秒脉冲不可用；1PPS 丢失；相位跳变或与其它源偏差>阈值 |
| 2.8 GNSS 天馈链路故障 | 开路/短路维持 10min 告警；恢复 5min | 开路：输出电流<2mA 且搜星异常（假开路=无电流但搜星正常，不告警）；短路：电流>100mA 且搜星异常，**关闭电源输出**（间隔检查）。单板 vs 线路问题区分：万用表测 SMA 输出>5V 非单板问题；与正常站交换线缆验证；馈线/天线/避雷功分器逐级排查 |
| 2.9.1 搜星不足 | 低于门限 10min 告警；达标 5min 恢复 | 单模<4 颗；混模（GPS/BDS、GPS/GLONASS、GPS/GALILEO）两系统均<4 且总数<5 |
| 2.9.2 信号不可用 | 异常 10min 告警；五项正常 5min 恢复 | 频偏>50ppb；相位>20us（可调）；闰秒=0；TOD<2018；经纬度变化>0.01° |
| 2.9.3 信号降质 | 配置≥2 参考源且异常 10min 告警；恢复 5min | 跳变：与所有正常源同向跳变>100ppb；偏离：与所有正常源相位差>1.5us |
| 2.9.4 锁星失败 | 搜到但无法锁定且不满足门限 10min 告警；锁星>4 颗 5min 恢复 | |
| 2.10 系统时钟保持模式 | 配置源全部异常且进入保持状态 | 保持能力：频率同步 30 天；相位同步受温度/源频偏影响，锁定 24h 才具 24h 保持能力 |
| 2.11.1 锁相环失锁 | DCO/CPRI/SyncE/CPU 任一环失锁即告警 | 环说明见原理 §1.3.1 |
| 2.11.2 本地时钟丢失 | OCXO 相关：PLL 检测 OCXO 输入丢失（xx18 检测）+ FPGA 检测 DCO 10M 丢失 | |
| 2.11.3 接收机故障 | 1PPS 无输出或串口无电文 10min 告警；正常 5min 恢复。原因码：接收机通讯失败 / 接收机 1PPS 丢失（基本为硬件，不单区分） | 双异常→接收机挂死概率大；单异常→线路问题（示波器抓波形）；复位重初始化；连 GNSS 串口用 uCenter 分析；无法恢复→插拔重启单板 |
| 2.11.4 鉴相值获取异常 | 持续 1min 从 FPGA 获取鉴相状态 error → PLost=1 | |
| 2.11.5 Serdes 时钟丢失 | 输出反馈 SERDES 时钟丢失（50Chip bit2-7） | |
| 2.11.6 帧同步信号错误 | Fsync 校验/失步/早到/晚到 | |
| 2.12 单板输入时钟异常 | 输入时钟无信号累计 60s 告警；正常持续 60s 恢复 | 输入系统时钟 / 输入同步时钟 / 对板输入时钟 / 对板输入数据 |

## 3. 网管诊断（RANCLI）

| 命令 | 用途 | 关键参数（排查点） |
|---|---|---|
| `queryClockStatus` | 时钟参考源状态（开站/日常） | 当前激活参考源、控制状态、时钟模块主备（只主用提供时钟）、网口 PLL 锁定、鉴相频偏(ppb)、压控电压、晶振出厂压控电压/Kf、环境温度、降质等级与状态（SyncE+1588 或 SyncE 配置时）、相位差信息（备用源 vs 主用源，ns） |
| `queryInnerGNSSInformation` | 内置 GNSS 参考源信息 | 串口状态、定位类型（3D/位置保持才可精确授时）、干扰状态、工作模式、初始化状态、1PPS 状态、天馈状态（正常/初始化/短路/开路）、闭塞状态、搜星状态、各系统锁定卫星数、位置、UTC、卫星信息（类型/号/方位角/仰角/载噪比/状态） |
| `query1588ClockInformation` | 1588 参考源状态 | 主/备链路信息（链路号/工作模式/链路状态/相位/频率/ClockClass/GM Id/stepsRemoved/Priority/Announce/Sync/Delay_Req/Resp 间隔）、1588 时间、UTC、闰秒、闭塞状态、SyncE 使能、SyncE 频偏(ppb)、SSM 使能/状态、SyncE 等级（PRS/PRC/SSU-A/ST2/SSU-B 才可同步） |
| `query1PPSTodInformation` | 1PPS+TOD 参考源信息 | 串口状态、时间解析状态、秒脉冲状态、GPS 时间、UTC、闰秒、1PPS 状态、闭塞状态、上级 MEID、上级单板硬件串号 |
| `querySyncEInformation` | SyncE 参考源信息 | SyncE 状态、SSM 使能、时钟等级、频偏(ppb)、闭塞状态 |
| `queryRruGnssInformation` | RRU GNSS 参考源状态 | 当前所选通讯链路（GNSS ID）、GPS/UTC 时间、闰秒、闭塞、链路状态、链路对应 RRU/AAU 的 LDN |
| `queryFddRruGNSSReceiverInfo` | FDD RRU 侧 GNSS 接收机（仅 FDD RRU） | GNSS ID、工作模式（外置单 GPS/GLONASS/GPS+GLONASS/拉远 GPS/单北斗/GPS+北斗）、接收机类型（M12/K161/CRESCENT/TRIMBLE/ublox/QUICK200/SigNav/CDS641F/RGCI 等）、版本、帧号属性、定位类型、初始化状态、天馈状态、位置、GPS/UTC 时间、闰秒、锁星数、天馈故障次数、初始化阶段、搜星状态、串口状态、卫星丢失次数、鉴相值、帧号、卫星信息 |
| `queryIRRRUGNSSInformation` | AAU/TDD RRU 侧 GNSS 接收机（仅 AAU/TDD RRU） | 定位类型、初始化、天馈、位置、时间、闰秒、锁星数、卫星信息 |

## 4. 前台诊断命令

### 4.1 cchelp 命令

通用告警输出表结构：`告警名称 | 地址(0x0cxxx) | 告警(OK/error) | 错误统计(3 列) | 结果分析`。**结果分析 = error 时告警生效**（statistics 3 列为最近 3 次统计值）。完整地址表见 4.1.0。

- **CcHelp 1 时钟信息采集**：单板/FPGA/EPLD/软件/平台版本、同步模式、当前激活参考源与锁定状态、配置参考源优先级、GPS 信息（天馈状态/锁定卫星数/天馈补偿量/GPS 状态/串口状态）、鉴相信息（压控电压状态/主同步次数/6 组鉴相 参考1-参考2-鉴相值-有效标识）、告警表。
- **CcHelp 2 主控信息采集**：面板 485 方向（PP1S/TOD in-out、GPS CMD）、GPS UART 发送选择、UART0-3（波特率/校验/RX FIFO 异常/RX 帧异常统计）、SW 恢复钟有效、PHY 状态（MDINT/FASTLINK_FAIL/COMA_MODE/CLK_SQUELCH_IN）、80227 状态（STATUS/LD/REFMON）、告警表（I2C 应答）。
- **CcHelp 3 Fsync 信息采集**：GSM/WCDMA/TD 帧号（3 次采样）、告警表（fsync 校验/异步/早到/晚到、反馈 PP2S、decode PP1S）。
- **CcHelp 4 主备信息采集**：本板/对板硬件主备、本板/对板时钟状态（编码同 THIS_CLOCK_STATUS 语义）、上电结束标识、槽位优先、80227 sync 状态、主备控制（驱动器/竞争使能/本板告警/运行/倒换请求/复位输出）、主备状态（对板上电/运行/告警/主备/复位/倒换请求）、单板状态、告警统计（对板复位次数/10ms 同步次数/软件倒换次数）、告警表（A/B/B2 类、对板不在位、互传数据错误、同备/同主异常）。
- **CcHelp 5 1588 信息采集**：80bit 系统时间（3 次）、LTC 同步状态（Sync status/TsMode/InSchedule/SyncStep/SyncTimes）、外部锁相环（UnLock）、告警表（250M 时钟错误、1588 PP1S 丢失/过长/过短）。
- **CcHelp 6 synce 信息采集**：外部锁相环参考源选择、synce 频率（PHY0/1 125M、SW0/1 1953125）、参考源 1-3、告警表（SW/PHY synce 无效/时钟错误）。
- **CcHelp 13 告警一键采集**：= 1/3/4/5/6 的告警表汇总（内容重复，不再展开）。
- **CcHelp 14 打印常用命令**：防重启 `OSS_DbgOK`、VLAN `BspESwitchDebugVlanTest 1`、面板 PPS 输出 `BspSetTestClk(ulChan,usSelect)`：

  | usSelect | 输出 | 说明 |
  |---|---|---|
  | 0x00 | local_pp1s | 本地 pp1s，脉宽 3us |
  | 0x01 | 10ms | 10ms 脉冲，占空比 50% |
  | 0x02 | gsm_4165us | GSM 4.165ms，占空比 50% |
  | 0x03 | pp2s_61m44 | 61.44M 域 pp2s，脉宽 3us |
  | 0x04 | ref_pp1s | 参考 pp1s |
  | 0x05 | wcdma_80ms_clk | WCDMA 80ms 测试信号，占空比 50% |
  | 0x06 | pp2s_61m44_frnt | 提前 pp2s |
  | 0x07 | decode_10ms | Fsync 解码 10ms，负脉冲 1ms |
  | 0x08 | decode_pp2s | Fsync 解码 pp2s，负脉冲 1ms |
  | 0x09 | gsm_60ms_clk | GSM 60ms 测试信号，脉宽 1ms |
  | 0x0a/0x0b | synce_clk[0]/[1] | PHY 第 0/1 路 synce |
  | 0x0c/0x0d | synce_clk[2]/[3] | SW 第 0/1 路 synce |
  | 0x0e | fsync_pp1s | Fsync 分发参考用 pp1s |
  | 0x0f | fsync_frnt_pp1s | Fsync 提前发送用 pp1s |
  | 0x10 | manual_cfg_pulse | 手动可配脉冲（周期 ms、脉宽 16.2ns） |

  帧号连续打印（软件进快捕后打印）：WCDMA `umtsfnlog 1/0`、GSM `gsmfnlog 1/0`、TD `tdfnlog 1/0`；鉴相值连续打印 `ulpdtest=1/0`。
- **CcHelp 15 采集所有信息**：= 1-6 全量采集。

### 4.1.0 cchelp 告警地址表（VSWe 主控，完整一份；单板域隔离）

| 地址 | 名称 | 地址 | 名称 |
|---|---|---|---|
| 0x0c208 | 对板不在位 | 0x0c4c8 | 第0路 Fsync 丢失 |
| 0x0c218 | 互传数据错误 | 0x0c4d8 | 第1路 Fsync 丢失 |
| 0x0c228 | 同备异常 | 0x0c508 | 第0路 Fsync 校验 |
| 0x0c238 | 同主异常 | 0x0c518 | 第0路 Fsync 异步 |
| 0x0c308 | B2 类错误 | 0x0c528 | 第0路 Fsync 早到 |
| 0x0c318 | B 类错误 | 0x0c538 | 第0路 Fsync 晚到 |
| 0x0c328 | A 类错误 | 0x0c548 | 第1路 Fsync 校验 |
| 0x0c408 | 10M 时钟丢失 | 0x0c558 | 第1路 Fsync 异步 |
| 0x0c418 | 80227 失锁 | 0x0c568 | 第1路 Fsync 早到 |
| 0x0c438 | 鉴相时钟失锁 | 0x0c578 | 第1路 Fsync 晚到 |
| 0x0c448 | MAIN 时钟错误 | 0x0c598 | 第0路反馈 PP2S 过长 |
| 0x0c458 | 主备时钟错误 | 0x0c5a8 | 第0路反馈 PP2S 过短 |
| 0x0c468 | 第0路反馈时钟错误 | 0x0c5c8 | 第1路反馈 PP2S 过长 |
| 0x0c478 | 第1路反馈时钟错误 | 0x0c5d8 | 第1路反馈 PP2S 过短 |
| 0x0c488 | 鉴相时钟错误 | 0x0c6f8 | decode PP1S 丢失 |
| 0x0c498 | 参考 pp1s 丢失 | 0x0c708 | decode PP1S 太长 |
| 0x0c4a8 | 参考 pp1s 过长 | 0x0c718 | decode PP1S 太短 |
| 0x0c4b8 | 参考 pp1s 过短 | 0x0c868 | 250M 时钟错误 |
| 0x0c698 | 1588 PP1S 丢失 | 0x0c808 | PHY0 synce 时钟错误 |
| 0x0c6a8 | 1588 PP1S 过长 | 0x0c818 | PHY1 synce 时钟错误 |
| 0x0c6b8 | 1588 PP1S 过短 | 0x0c828 | SW0 synce 无效指示 |
| 0x0ca08 | OCXO I2C 地址应答 | 0x0c838 | SW0 synce 时钟错误 |
| 0x0ca18 | OCXO I2C 数据应答 | 0x0c848 | SW1 synce 无效指示 |
| 0x0ca28 | 扩展 I2C[0] 地址应答 | 0x0c858 | SW1 synce 时钟错误 |
| 0x0ca38 | 扩展 I2C[0] 数据应答 | | |
| 0x0ca48 | 扩展 I2C[1] 地址应答 | | |
| 0x0ca58 | 扩展 I2C[1] 数据应答 | | |

**注**：VSWe 告警地址（0x0c4xx 系列、SW1 与 SW0 同地址 0x0c838/0x0c858 原样保留）与 ITRAN 的 `0xc4xN` 布局不同，属单板差异，勿混用。

### 4.2 ShowDrvData 1（接收机驱动诊断）

- **Gnss Config Data**：配置最小卫星数 `ulLeastSatNum`（默认 4）、工作模式、时间栅（0-UTC/1-GPS/2-GLN/3-BDS/4-GLI）、位置。
- **Gnss Ref Status**：串口错误 `ucGpsUartErr`、1PPS 丢失 `ucPpsLost`、1PPS 相位错误 `ucPpsPhaseErr`、接收机型号 `ucRcvType`（9/18=M8T 系）、定位状态 `ulRcvFixStatus`、天馈状态 `ulRcvAntennaState`、初始化完成 `ulRcvInitFinishFlag`、TOD OK `ulRcvTodOkFlag`、可用状态 `ulRcvUseableFlag`。
- **Rcv Detail Info**：平均载噪比 `AvgCno`、闰秒 `ucLeapSeconds`。
- **Rcv reset Info**：复位次数 `ulRcvResetTimes`、复位原因 `Reason`、Lock Failed Time。
- **Rcv Temp Info**：天馈故障次数、TOD OK 计数、初始化起止时间、波特率、固件版本（`aucGnssFwVer`）、双接收机保护参数、伪 GNSS 相关（`ucFakeGpsFreqErr/PhaseErr/MsgErr`）、干扰/欺骗检测状态（`ucJamInd/JamingState/SpoofDetState`）。

### 4.3 ShowClockCfg 1（时钟配置）

Base Clock Work mode（Phase sync）、Fn Enable、Ref Switch Mode、Hold Over Time（24h）、SyncE Assist Holdover、Clock MS Backup、参考源（名称/ID/优先级/丢失门限 600s/天线时延/工作模式/时间模式/最小卫星数）。

### 4.4 ClockDiagInfoShow（时钟诊断汇总）

- 主表：Fsync 错误/丢失/晚到/早到计数（0/1 路）、ClkFsyncErr/Status、Clk10MHzErr、Clk80MHzErr、GetPhaseErr、Serdes/网口 PLL 锁定、主备切换次数、GcmMainBackState、OppBoard 在位/数据/时钟状态、OscType、相位同步次数、压控电压。
- **Clk Config Info Diag**：工作模式/参考数/参考 ID/优先级。
- **CurRef Info Diag**：激活参考 ID、主/辅鉴相值、当前压控电压、相位控制状态、闰秒、保持能力 `ulHoldCapability`、剩余保持时长、运行 OK 时长、时钟错误标志。
- **Fn Info Diag**：帧号设置使能、总帧号错误计数。
- **GNSS Info Diag**：参考 ID、工作模式、天馈跳变次数、接收机类型/版本、天馈状态、初始化完成、锁星数（GPS/GNS）、定位类型、卫星丢失、闰秒、串口错误、1PPS 丢失、各告警标志（天线开路/短路/锁星失败/锁星 PPS 错误/闭塞/热风错误）、可见卫星数。
- **1pp1+tod Info Diag / Info Diag**：`BspPpsTodRefAlmDiag 14 return not ok`、`BspGet1588DrvStatus Cnt 10 return not ok` 等为接口调用失败直出（接口调用行复用 `parse_cloudmat.py` interface_calls 语义）。

### 4.5 系统状态

- `ShowCommonClkInfo`：当前参考源状态。
- `ShowCLk`：软环状态变化 + `CurFcv` 当前频率控制字；CtrStat：0 预热/1 快捕/2 锁定（常规）/3 高精度锁定（常规）/5 保持（常规）/7 SYNCE 增强保持/8 自由运行（常规）/9 辅助相位控。
- `ocxolog 1`：传感器数据 + 频率控制字（= OCXO 黑匣子，parse_clock CLK100 语义）。
- `BspCurClkStatusDiagInfo`：时钟源 ID、同步模式、PLL 状态（同 CtrStat）、保持能力(s)、剩余保持时长、运行持续时长、锁定次数。
- `BspGetHoldCapability`：0=无 / 1=1h / 2=4h / 3=24h / 4=SYNCE 增强保持。

### 4.6 打印接收机信息

| 命令 | 内容 |
|---|---|
| `Printsatlog 1` | GPS 状态全量：锁定状态、天馈状态、卫星数量、TOD、相位 |
| `gpslog 1` | 同 Printsatlog 精简版 |
| `svlog 1` | 仰角、方位角、载噪比 |
| `showclk 101` | 十五分钟载噪比 |
| `IntRcvAntStatusGet` | 天馈：1 正常 / 126 初始化 / 127 未知 / 128 短路 / 129 开路 |
| `Printsatctrllog` | 相位控制日志：`Run=, Run_Counter=, PhaseCtrl: Main: -6ns, Assist: -4ns. tick=` |

## 5. VSWe 专属 CLK 行格式

通用 CLK 索引语义（每个索引是什么、排查什么）见 `references/platform/log-package-map.md` §4.1；字段枚举/阈值/位定义见 `assets/shared/clk-field-dictionary.yaml`。本节只保留与通用表**不同**的 VSWe 行格式，不重复索引含义。

| CLK | VSWe 特有字段/格式 |
|---|---|
| 000 | `Ref Sts Smoo Mp cDa rDa Ms T Tick`（Smoo=smooth 标志；Ms=主相位同步累计；T=温度；Tick=上电后每秒 +100） |
| 036 | clockstatuslog：`clear dif`、`ref ch:add X to Y,num=`、`Id Ch Flg Ref Dif F PhaF DacF RcentV`；clockdatalog：`RecentDac`、`OC jump:Xppb, NewRecentDac`、`Ref Ch Ref Block Flg DifCnt F PhaF DacF FValid`、`PhaDif CurDac Tick64`；Flg 枚举见字典 `CLK036_FLG` |
| 085 | clockstatuslog：`Ref Dif Alm`（主备相位差与告警标志）+ `1588:%.2f,Gnss:%.2f,OcJ:%.2f,t=`、`Drift %u Ref Rcr`（1588=1/GNSS=2/0 正常）；clockdatalog：`DifFlg GPS time`、`PdDif Dac 1588Frq GnssFrq OcJmp Tick` |
| 099 | `10MHz=%u 80MHz=%u NetPll=%u Pps=%u Fsync=%03x 50Chip=%03x SesPLL=%u PLost=%u`，位定义见字典 `CLK099_VSWE` |
| 037 / 038 | ATR 指示码 BIT0-7 见字典 `CLK037_ATR`；038 仅相位同步时打印，且服务器时间 <1563062419（1970 起 2062 周）才存 |

## 6. 与现有能力衔接

- **告警语义**：cchelp 告警地址表与 `clk-fixture-map.md` §2 寄存器映射同域互补（工装测试位图 vs 维测诊断位图）；规则 any_of 素材可取自告警名称与地址。
- **日志解析**：CLK 行由 `parse_clock.py` 解析（mList 合并/位置格式/复位假时间修复）；索引语义统一见 `log-package-map.md` §4.1，字段枚举见字典，本节只留 VSWe 差异。
- **命令输出**：前台命令的输出字段（ShowDrvData/ClockDiagInfoShow）可作 `text` 规则检索锚点；接口调用失败直出（`BspGet1588DrvStatus Cnt 10 return not ok`）复用 `parse_cloudmat.py` 的 `interface_calls` 语义。
- **单板隔离**：VSWe（0x0cxxx 告警地址、80227 PLL、BspSetTestClk 输出表）与 ITRAN（0xcxxN 寄存器布局）不混用；CLK 日志字段为 BBU 主控通用，跨单板可共享解析，但寄存器/命令输出按单板域判定。
