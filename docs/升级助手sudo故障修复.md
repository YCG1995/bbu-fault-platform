# 网页升级助手 sudo 故障修复

日期：2026-11-26  
现象：网页“离线升级”提交后提示：

```
助手启动失败：sudo: 有效用户 ID 不是 0，/usr/bin/sudo 位于一个设置了“nosuid”选项的文件系统或没有 root 权限的 NFS 文件系统中吗？
```

## 1. 这不是升级包或助手 Python 的问题

平台 Web 进程以 `bbu` 用户运行，通过固定的 `bbu-upgrade-helper-trigger` 调 root/systemd 一次性助手：

```
bbu → sudo -n → /usr/local/bin/bbu-upgrade-helper-trigger → systemctl start bbu-upgrade-helper@<job>
```

报错发生在第一步 `sudo` 自身。常见原因有四类：

1. **平台主服务单元启用 `NoNewPrivileges=true`**（1.20.1 及更早的默认单元就是这样）：内核会禁止 setuid 提权，即使 sudo 权限完全正常也会报这个错；
2. `/usr/bin/sudo` 丢失 setuid 位（常见为 0755，正常应为 4755 root:root）；
3. `/usr/bin/sudo` 所在文件系统挂了 `nosuid`（setuid 被内核忽略），或挂了 `noexec`；
4. 当前账号没有可用的 sudoers 规则，或平台服务用户不是 sudoers 中的 `bbu`。

先看第 1 类：`systemctl show bbu-fault-platform -p NoNewPrivileges` 输出 `yes` 即命中。1.20.2 已从新安装单元移除该选项，并在升级时自动迁移旧单元。

## 2. 先确认是不是 NoNewPrivileges

这是 1.20.1 及更早 systemd 安装最常见的根因，优先检查：

```bash
systemctl show bbu-fault-platform -p NoNewPrivileges
```

输出 `NoNewPrivileges=yes` 时，`sudo` 的 setuid 会被内核忽略；即使 `ls -l /usr/bin/sudo` 是 `4755 root:root`，网页升级仍会报本文开头的错误。1.20.2 的新单元已移除该选项；已有安装可执行：

```bash
bash /opt/bbu-fault-platform/deploy/fix_upgrade_helper.sh --fix-nnp
```

## 3. 再用诊断脚本确认

在服务器上以 root 或普通账号执行只读诊断：

```bash
bash /opt/bbu-fault-platform/deploy/fix_upgrade_helper.sh --diagnose
```

重点看四行：

- `NoNewPrivileges`：应为 `disabled`；`enabled` 会直接阻断 setuid sudo；
- `sudo 权限/属主`：应为 `4755 root:root`；
- `setuid`：应为“正常”；
- `挂载`：不能含 `nosuid` / `noexec`；
- `systemd`：应为 `ok`，不能是 `not-running`。

## 4. root 一键修复

必须从 **root 控制台** 执行（例如 `su -`、云主机 VNC、`docker exec -u 0`）；不要用已经坏掉的 `sudo` 去启动修复脚本：

```bash
su -
bash /opt/bbu-fault-platform/deploy/fix_upgrade_helper.sh --fix-nnp
# 如同时发现 sudo setuid 丢失：
bash /opt/bbu-fault-platform/deploy/fix_upgrade_helper.sh --fix-sudo --fix-nnp
```

脚本会：

1. 检查主服务单元是否启用 `NoNewPrivileges`，必要时备份并移除该行、重启服务；
2. 检查 sudo 是否由 dpkg/rpm 的 `sudo` 包登记，拒绝把未知二进制设为 setuid root；
3. 在非 `nosuid`/`noexec` 文件系统上恢复 `root:root` + `4755`；
4. 重新执行 `install_upgrade_helper.sh`，安装固定 unit、触发器、sudoers 和 `/etc/bbu-upgrade.json`；
5. 用服务用户执行一次非法任务 ID，期望返回 `任务 ID 非法`（exit 2），确认 sudo 与触发器链路通了。

脚本本身必须是 UTF-8 编码。如果脚本被 Windows 记事本保存成 ANSI/GBK，中文会显示为乱码，早期版本还会因为 `grep` 中文失败而误报“自检未通过”；实际只要 `触发器自检 : exit=2` 且输出为 `任务 ID 非法`，助手链路就是通的。最新脚本已改为只按退出码 2 判断，不再依赖中文匹配。

```
[修复] 已恢复 /usr/bin/sudo 为 root:root 4755
[修复] 重装 bbu-upgrade-helper@.service / 触发器 / sudoers
[修复] 触发器自检 : exit=2
[修复] 输出       : 任务 ID 非法
[修复] 修复完成：网页升级助手可启动。
```

## 5. 如果 sudo 所在文件系统是 nosuid

脚本会明确拒绝继续，因为 `chmod 4755` 在 `nosuid` 挂载上无效。先确认：

```bash
findmnt -T /usr/bin/sudo -o TARGET,SOURCE,FSTYPE,OPTIONS
```

- 云主机/物理机：由系统管理员把 `/usr` 或根挂载去掉 `nosuid` 后重试；
- 容器：通常无法在容器内 remount 宿主挂载，网页自动升级不可用，改走 root 手工升级；
- 没有 root：任何“一键脚本”都无法自我提权，只能请维护人员执行手工升级。

## 6. 手工升级（不依赖网页助手）

```bash
# root 或具备程序目录写权限、能停止平台的账号
sha256sum -c bbu-fault-platform-1.20.2-offline.tar.gz.sha256
tar -xzf bbu-fault-platform-1.20.2-offline.tar.gz -C /tmp

systemctl stop bbu-fault-platform

bash /tmp/bbu-fault-platform/deploy/upgrade_offline.sh \
  --prefix /opt/bbu-fault-platform \
  --state-dir /var/lib/bbu-fault-platform \
  --no-service

systemctl start bbu-fault-platform
curl -fsS http://127.0.0.1:8080/api/health
```

没有 systemd 时，把 `systemctl stop/start` 换成停止/启动原 `run.py` 进程。升级脚本会保留数据库、密钥和配置，并在 `STATE_DIR/backups/` 生成升级快照。

## 7. 修复后仍失败时看哪里

```bash
systemctl status bbu-upgrade-helper@<job>
journalctl -u bbu-upgrade-helper@<job> -n 100 --no-pager
cat /etc/bbu-upgrade.json
sudo -u bbu sudo -n /usr/local/bin/bbu-upgrade-helper-trigger Utest
visudo -cf /etc/sudoers.d/bbu-upgrade
```

如果 `sudo -u bbu ... Utest` 返回 `任务 ID 非法`，说明 sudo 和触发器已经恢复；真正升级失败则应看 `helper.log` 和 `upgrade_offline.sh` 的输出，而不是继续修 sudo。
