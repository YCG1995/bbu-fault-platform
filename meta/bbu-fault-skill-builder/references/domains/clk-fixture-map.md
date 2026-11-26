# 时钟工装测试方案速查（单板域：VSWe 主控板）

> **适用**：VSWe 主控板时钟工装测试（工装日志中的接口调用/返回值、`cchelp 1` 诊断输出、板内告警寄存器）。
> **何时读本文件**：台架/工装测试失败——ClockMAT 工装日志、`cchelp 1`、BSP 接口返回值、板内时钟告警寄存器位图。现场网管告警、ShowDrvData/ClockDiagInfoShow 等维测命令排查，读 `vswe-clock-maintenance.md`。
> **重要（单板差异）**：**其它单板不一定相同，可能根据单板变化**——寄存器地址、PLL 器件（5518/35018）、
> 测试项支持范围均可能不同。本文件按单板域引用，**不得写死为通用规则**；处理新单板时按
> 访谈流程重新确认后另立域（`VSWE_FIXTURE_*` 前加新单板前缀，如 `VSWD_FIXTURE_*`）。

## 1. 测试项清单（VSWe，18 项 + 补充）

| 测试项 | 继承/改动/新增 | 目的 | 主要接口/寄存器 |
|---|---|---|---|
| 1588_1pps 时钟测试 | 改动 | 1588 1PPS 是否正常 | `Bsp1588RefPpsTest` |
| 面板输出 PP1S_TOD 测试 | 继承 | 两路面板 485 环回 | `UartLoopTest` |
| 烧电压测试 | 改动 | 计算中心频偏并写 E2PROM | `BspGcmVkStart` / `BspGcmVkStateGet` |
| DCO 拉偏测试 | **新增** | 无压控下 DCO 好坏 | `DcoPllBiasTest` |
| 相位超差测试 | 改动 | 无压控下 OCXO 初始频偏 | `MTE_CalcKPara`（-5/0/+5ppm 映射 0V/2.5V/5V） |
| VSW 到背板 61.44M 时钟检测 | 继承 | 反馈 61M44 寄存器 | 0xC468 |
| 61.44M 时钟测试 | 继承 | 主/主备 61M44 寄存器 | 0xC448 / 0xC458 |
| GPS 接收机状态检测 | 继承 | 接收机运行状态 | `BspGcmTestReceiver` |
| 天馈开路短路测试 | 继承 | 开短路检测 | `IntRcvAntStatusGet` |
| PP1S 测试 | 继承 | GNSS 接收机 1PPS | 0xC600/0xC608 + `BspReceiverInitFinishFlag` |
| GPS 接收机复位测试 | 继承 | 复位信号 | `BspIntReceiverResetTest` |
| 时钟 I2C 总线测试 | **新增** | I2C 器件读写 | `BspOcxoCurrentDectTest/OutTempSensorTest/EepromTest` |
| 锁相环配置参数测试 | **新增** | PLL SPI 读写 | `BspAu5518RegWrite/RegShow`（35018 待兼容） |
| 锁相环各输出锁定测试 | 改动 | PLL 锁定状态 | `Mte_BspGetPllLockStatus` |
| PP1S 中断测试 | **新增** | 中断重新触发 | `BspPpsIntrTest` |
| 1588_Synce 恢复时钟测试 | 改动 | SyncE 参考 + FPGA 告警/SyncE 环 | `Si5345InputRefSwTest` |
| 板内时钟告警测试 | 改动 | FPGA 告警寄存器（新增 10M 检测） | `BspGcmGetClkStatus` |
| 环境 GPS 检测 | 继承 | 环境板 GPS（与受测板无关） | 工装自实现 |

补充项：晶振频偏测试（`BspClockHwInfoStatusGet` + `BspGetPllRefDrift`，5518 相对 54M、35018 相对 10M）、
1588 PTP 打戳测试（`BspPtpStampTestStart`/`BspGetPtpStampTestResult`）、`cchelp 1` 统一诊断。

GPS 三项（状态检测/PP1S/复位测试）已讨论**合并流程**：配置参考源 → 等版本查询成功(≤100s) → 等初始化完成 →
记录运行时间 T0 → 复位 → 等 T1<T0 → 清 1PPS 告警 → 等 5s 查丢失 → 清参考源；任一失败则复位后重跑一轮。

## 2. 寄存器映射（FPGA / EPLD）

**工装测试模式**：`0x4200`(16896) 写 `0x55aa`(21930) 进入工装模式（其他值恢复正常）；`0x4204`(16900) 控制
模拟 SDA/SCL/MS_STATUS/背板 2 路 fsync 与 61.44M 时钟使能（[6:5] 控制 2 路 61.44M 使能）。

**FPGA 告警位图（板内时钟告警测试，`BspGcmGetClkStatus` 返回按位判）**：

| 寄存器 | 位 | 含义 | 定位 |
|---|---|---|---|
| 0xC408 | 1 | DCO 10M 时钟异常 | R21A1 / D1A5A1 / R20A1 |
| 0xC448(50248) | 8 | 61.44M 下发时钟错误 | 输入串阻 R40A1 虚焊 |
| 0xC458(50264) | 9 | 61.44M 主备时钟错误 | R38A1/R39A1 虚焊；C12/C13/R131/R132/R135/R136/R124 |
| 0xC468(50280) | 16 | 反馈 61.44M 信号（第 0 路） | R637 / R18A1 / D1A8A1(PIN8 3.3V) / R131A1 |
| 0xC4C8 | 4 | 反馈 FSYNC0 丢失 | R542 / A1A9A1 / R1A9A1 |
| 0xC4D8 | 22 | FSYNC1 丢失（未使用） | — |
| 0xC488 | 3 | 250M 丢失（未使用） | — |
| 0xC4E8 | ? | OCXO 原始 10M 丢失（**待新增**） | G1A3A1 / R11A3A1 / D1A2A1 / R17A1 |

主备状态：0x4308 初始上电结束（[0] AD80227 初始化完成使能主备竞争）、0x4370 时钟主备状态（1=主/0=备）、
0xC4CN 第 0 路 fsync 丢失（仅主板检测）。

**EPLD**：

| 寄存器 | 含义 |
|---|---|
| 0x120 | PLL 类型：0=AU5518、1=8A35018 |
| 0x124 bit0 | OCXO 类型：1=有压控（启用有压控烧电压流程）、0=无压控 |
| 0x810 | FPGA I2C 状态：[15:5] 接收 FIFO 剩余字节、[2] 读写完成、[1] 总线忙、[0] 器件无应答 |
| 0x20C [6] | 接收机复位：写 0=拉低复位、写 1=复位解除（3.3V） |
| 0xAA8 [6] | 接收机电源：写 0=断电、写 1=上电 |

## 3. BSP 接口返回值语义（规则素材）

| 接口 | 返回值 | 语义 |
|---|---|---|
| `Bsp1588RefPpsTest` | BIT0 | FPGA 检测 1588 1PPS 丢失（R2A5 及 CPU/FPGA 焊盘虚焊） |
| | BIT1 | 30s 频偏检测内鉴相值获取超时（1.25s；本地/1PPS 异常） |
| | BIT2 | 1588 1PPS 频偏异常（门限 `DCO_BAIS_ERROR_GATE`=50ppb；前几秒相位差过大→初始化等待不足） |
| `UartLoopTest` | bit0 | TOD(GPS 串口) → 1PPS(面板串口) 环回失败 |
| | bit1 | 1PPS(面板串口) → TOD(GPS 串口) 环回失败 |
| `BspGcmVkStateGet` | 0xXX00_0000 | 烧电压阶段：1=读 0V 相位、2=读 5V、3=读 2.5V、4=存储、5=完成、6=运行中 |
| | 0x00XX_XX00 | 细节位：0x100=相位 READY/PP1S 丢失、0x200=相位符号异常、0x400=数据不合理 |
| | 0x0000_00FF | 错误码（**用 == 判定，非 BIT**）：0 成功/1 未发起/2 电压标记清除失败/3 未检测外部 1pps/4 0V 相位异常/5 5V 相位异常/6 中心电压异常/7 E2PROM 写异常/8 未知/9 2.5V 相位异常/10 2.5V-0V 相位差超限/11 5V-2.5V 相位差超限 |
| `DcoPllBiasTest` | BIT0/1/2 | -4ppm / +4ppm / 0ppm 频偏计算失败 |
| | BIT3/4 | 拉偏范围越界：`|P-4-P0+4ppm|` 或 `|P4-P0-4ppm|` 超过 `DCO_BAIS_ERROR_GATE` |
| `IntRcvAntStatusGet` | 1 | 天馈正常 |
| | 128 | 天馈短路（去 SMA 负载复测 → 万用表测中芯对地短路） |
| | 129 | 天馈开路（接负载复测；X1A6A1 虚焊） |
| | 126/127 | 初始化中/未知 |
| `BspGcmTestReceiver` | OK | 接收机初始化状态 >1（查询到版本=读写正常） |
| `BspIntReceiverResetTest` | OK | 复位后接收机启动时间变小（T1<T0） |
| `Mte_BspGetPllLockStatus` | 0x0-0xF | 5518：reg 0x06\|0x07 & 0xF，bit0-3=PLL A-D，0=锁定 1=失锁（状态或事件任一失锁） |
| | bit31 | 寄存器读写失败 |
| | 35018 | DPLL2-4/6/SYSPLL(8)/APLL(9) 依次对应 cpu/synce/cpri/dco/50M；burst 读 DPLL_STATUS，任一环 `(val&0x1F)!=0x3` 即失锁（BIT3/BIT5 未使用取消） |
| `Si5345InputRefSwTest` | BIT0 | SyncE 环在本地 10M 参考下失锁（与 PLL 锁定测试同失败） |
| | BIT1-5 | SyncE 环在外部 SyncE 参考下失锁 |
| | BIT6-10 | FPGA 检测 SyncE 频偏超 50ppm（多端口判断硬件链路） |
| | BIT11 | 面板五端口均未连接（需环回头） |
| `BspPpsIntrTest` | OK | 清中断 → 延时 1.5s → 中断重新触发 |
| `BspGetPtpStampTestResult` | bit0-10 | 各端口打戳测试失败（收发数量不等或时间戳差超门限：光口 50ns/电口 500ns） |
| | bit11-21 | 对应端口未测试过 |
| `BspGetPllRefDrift` | int(fDrift*1000) | 5518：ucReference=0 相对 54M（0=OCXO/1=SyncE/2=DCO/3=50M）；35018：ucReference=1 相对 10M。OCXO 相对 54M 需取负 |

## 4. cchelp 1 输出结构（稳定表格，可解析）

统一诊断命令 `cchelp 1`，输出固定分段：

| 段 | 关键内容 | 解析要点 |
|---|---|---|
| 单板信息 | 单板类型/槽位/背板类型/FPGA/EPLD/软件/平台版本/同步模式/激活参考源/参考源锁定状态 | 同步模式：相位同步；参考源锁定：FREE RUN |
| GPS 信息 | 天馈状态(开路/短路阈值：输出电流<2mA 开路、>100mA 短路)、锁定卫星数(正常≥4)、天馈补偿量、GPS 状态(time fix/3d fix/not fix)、GPS 串口状态指示(ok) | 需配置 GPS 参考源才输出 |
| OCXO 信息 | OCXO 型号(xx 无压控，EPLD ID 电阻识别)、厂家(FEC/DP，IIC 读取)、温度(IIC)、电流(>10mA) | 型号/厂家/温度/电流异常分别对应 EEPROM/温感/电流传感器 IIC 项 |
| PLL 信息 | 器件型号(35018/5518)、版本、SyncE 当前参考(本地时钟:10M / 恢复时钟:SyncE)、每行 PLL 名称/Ref 名称/PLL 状态(OK\|LOL)/Ref 状态(OK\|Loss\|Drift)/Ref 频偏(参考 54M、参考 10M) | Ref 状态 Drift→频偏异常、Loss→链路丢失；区分诊断 |
| LT PLL 锁定状态 | 序号 PLL0-6 功能(SOC/NTL0/NTL1/PCS/PSYNC/PTP/LDPC) 状态 OK | 光模块/接口侧 PLL |
| 鉴相信息 | 压控电压状态/值、主同步次数、第 1-6 组鉴相(参考1/参考2/鉴相值 ns/有效标识) | 有效标识 invalid=鉴相不可用 |
| 告警信息 | 告警名称/地址/告警(OK)/错误统计(3 列)/结果分析 | 地址对应 §2 寄存器表 |

**PLL 失锁决策链（`cchelp 1` → 处理）**：

| 现象 | 定位 | 更换 |
|---|---|---|
| 锁相环全部失锁 | 54M 晶体周边阻容虚焊短路 | G1A7A1 |
| DCO 环失锁 | 读 Ref 状态：Loss→10M 链路（G1A3A1/R11A3A1/D1A2A1/R146A1/R143A1）；Drift→OCXO 问题 | 按 OCXO 处理 |
| SyncE 环失锁 | 当前参考=本地 10M→与 CPRI 同处理；=SyncE→查端口环回头/上级，R415A5/R421A5 | — |
| CPRI 环失锁 | R144A1 / R145A1 虚焊短路 | — |
| 本地环失锁 | 50M 晶振 G1A4A1 电源 L1A4A1/使能 R1A4A1/输出串阻 R2A4A1；BUF D1A1A1 电源 L1A1A1/使能 R1A1A1/串阻 R147A1/R148A1 | G1A4A1 |
| 54M 环失锁 | 同"全部失锁" | G1A7A1 |

**烧电压 / DCO / 相位超差的器件级结论**：烧电压失败本身按 DCO 拉偏测试（换 PLL `D4A7A1`）与相位超差测试（换 OCXO `G1A3A1`）定位，不单独判器件。

## 5. 与现有能力衔接

- **工装日志直接复用**：文档中接口调用片段与 CloudMAT 工装日志行格式一致（`调用:接口,参数:[...],返回值:[...]`，
  含 `id` 字段），`parse_cloudmat.py` 的"接口调用返回值"提取可直接应用于 VSWe 工装日志，无需新解析器。
- **数值/位图规则**：频偏/相位差（烧电压斜率 |K|≥1000、相位超差 ±1ppm、DCO ±4ppm 门限）→ `numeric_trend`
  规则（`abs_threshold`/`threshold`）；寄存器位图/返回值 BIT → `text` 规则（`any_of` 关键字如 `TestItemResult.Fail`、
  `返回值`、`BspGcmVkStateGet` 错误码）。
- **单板差异**：寄存器与器件按单板分域，规则 profile 必须写明适用单板（`适用领域`），不得跨单板套用；
  新单板需访谈确认寄存器/BIT 语义后再落地。
- **文档状态**：VSWe 工装测试方案为初稿（2022-06 评审后修订），"检查 PLL 兼容 35018""板内告警新增 OCXO 10M 检测"
  等仍为待办；未验证内容标 `verified: false`。

## 6. 关键关键字速查（作规则 any_of/all_of 素材）

- 总体：`cchelp 1`、`工装测试模式`、`BspGcmVkStateGet`、`TestItemResult.Fail`、`TestItemResult.Success`
- 烧电压错误码：`VOLTAGE_SUCCESS`、`VOLTAGE_1PPS_ERROR`、`VOLTAGE_E2PROM_WRITE_ERROR`、`VOLTAGE_2P5V_MINUS_0V_ERROR`
- 天馈：`GPS_ANTENNA_STATE_SHORT`(128)、`GPS_ANTENNA_STATE_BREAK`(129)
- PLL：`LOL`（失锁）、`Loss`（参考丢失）、`Drift`（频偏过大）、`Au5518`、`35018`
- GPS：`time fix`、`3d fix`、`GPS串口状态指示`、`锁定卫星数`
- OCXO：`普通无压控`、`FEC`、`DP`、`温度：`、`电流：`
