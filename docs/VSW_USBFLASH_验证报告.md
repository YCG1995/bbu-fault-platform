# VSW 单板 USB FLASH 故障案例验证报告

## 1. 生成结果

- 技能名：`bbu-fault-vsw-usbflash`
- 标题：VSW 单板 USB FLASH 故障分析
- 版本：`0.1.0`
- 规则：4 条（E1、E2、E4、E5）
- 根因：3 个（R1 写保护、R2 器件/I/O 故障、R3 SMART 获取失败）
- profile 校验：通过
- 平台工坊构建：通过，标记为“未验证”（`verified=false`）
- 未发布到正式技能库；待真实 C1–C5 日志回归后再发布

profile 文件：`examples/profiles/vsw-usbflash.yaml`

## 2. 规则设计

| 规则 | 证据位置 | 判定 |
|---|---|---|
| E1 | 内核日志 | `usb 1-1: USB disconnect`；仅 1-1 是 USB FLASH |
| E2 | 内核日志 | `blk_update_request: critical medium error` 或 `Buffer I/O error on dev sdb` |
| E4 | BoardInit / BoardHealth | `protect_status` 非 0 |
| E5 | BoardInit / BoardHealth | `this flash not support`、`idVendor = 0` 或 `Manufacturer:(null)` |

根因条件：

```text
R1 = E4
R2 = (E1 or E2) and not E4
R3 = E5 and not E4 and not E1 and not E2
```

## 3. 规则级验证

本次消息没有随附 C1–C5 的真实日志包，因此使用用户原文中的字段和日志行构造了 5 个合成样例。合成样例只验证规则逻辑，**不等同于真实案例回归**。

| 案例 | 期望 | 实际 verdict | 命中规则 | 结果 |
|---|---|---|---|---|
| C1 正常 | 排除本故障 | `unmatched / NONE` | 0/4 | 通过 |
| C2 `usb 1-2` 断开 | 排除本故障 | `unmatched / NONE` | 0/4 | 通过 |
| C3 `protect_status: 20102` | R1 写保护 | `matched / R1` | E4=1 | 通过 |
| C4 `Buffer I/O error on dev sdb4` | R2 I/O 故障 | `matched / R2` | E2=1 | 通过 |
| C5 `this flash not support` | R3 SMART 异常 | `matched / R3` | E5=3 | 通过 |

端到端使用生成的 `run_analysis.py` 对 5 个合成样例各跑一次，均返回 `ok: true`，报告 MD/HTML 正常生成，无 warnings。

## 4. 当前局限

1. **真实 C1–C5 附件缺失**：消息中只有案例卡片和文字描述，没有 `BoardInit.log.bak.1`、`USB FLASH反例 kernel...tar`、`写保护 BoardInit...`、`IO error kernel.klog.2.gz`、`意烁...BoardHealth.log` 这些实际文件。因此没有做真实包回归，也不能宣称技能已验证。
2. **7z 依赖**：当前环境没有 `7z/7za/7zr`。真实包中如有 `kernel.klog.1.7z`，需要安装 7z 才能解压；`.tar`、`.gz`、`.log`、`.bak.N` 可按现有能力处理。
3. **C2/C4/C5 合成目录出现 `glob_fallback`**：因为测试目录只放了裸文件名，解析器按 basename 兜底命中；真实标准目录应带 `OssLogVsw/...` 或 `BspLog/...`，届时不会依赖兜底。真实包入库后需确认 `glob_fallback=0`。
4. **未做案例库回归**：`build_child_skill.py --skip-regression` 仅生成，平台工坊显示“未验证”，未执行案例覆盖率、根因断言和端到端门禁。

## 5. 下一步

1. 上传 C1–C5 的真实日志包。
2. 用 `init_case.py` 建立案例库，按 `case.yaml` 写 `kind`、`expected.root_cause`、`hit_rules`、`not_hit_rules`。
3. 运行：

```bash
python3 scripts/build_child_skill.py \
  --profile examples/profiles/vsw-usbflash.yaml \
  --path <skills根> \
  --cases-dir <案例库根>
```

4. 全案例通过后再发布；发布前确认 C1/C2 反例为 `unmatched`，C3/C4/C5 分别命中 R1/R2/R3。
