# Windows 运行说明

Windows 版本**不提供一键部署包**，按以下方式直接运行即可。平台后端只用 Python 标准库，`vendor/yaml` 已随包提供，不需要 `pip install` 任何依赖。

## 1. 环境

- Windows 10/11 或 Windows Server 2016+，x86_64
- Python 3.11 及以上（推荐 3.12/3.13，64 位）
- 安装 Python 时勾选 `Add python.exe to PATH`

验证：

```bat
python --version
```

## 2. 放置目录

解压或复制到**可写目录**，例如：

```text
D:\bbu-fault-platform\
```

不要放在 `C:\Program Files` 下，否则 SQLite、上传目录和初始密码文件可能无法写入。平台默认在程序目录下创建：

```text
data\        数据库、上传、任务、密钥、初始管理员密码
skills\      运行时技能
cases\       技能案例
updates\     升级/迁移任务记录与临时工作区
backups\     覆盖恢复前的旧数据备份
config.json  可选配置
```

从 1.10.0 起也可以配置统一工作目录，把 data / skills / cases / updates / backups 放到独立磁盘：

```json
{
  "work_dir": "D:\\bbu-runtime"
}
```

data / skills / cases 会派生到 `D:\bbu-runtime\data` 等子目录；`work_dir` 优先于单项路径。也可以设置 `BBU_WORK_DIR` 环境变量。既没有 `work_dir` 也没有单项环境变量时，仍使用程序目录下的默认路径。

## 3. 可选：配置内网 pip 源

仓库已带两份相同配置：

- `deploy/pip.ini`：Windows
- `deploy/pip.conf`：Linux

Windows 直接双击或在命令行执行：

```bat
deploy\windows\set_pip_mirror.bat
```

它会把 `deploy\pip.ini` 复制到 `%APPDATA%\pip\pip.ini`，原文件存在时备份为 `pip.ini.bak`。内容为：

```ini
[global]
index-url = https://artsz.zte.com.cn/artifactory/api/pypi/public-pypi-virtual/simple
trusted-host = artsz.zte.com.cn
timeout = 60
retries = 3
```

验证：

```bat
python -m pip config list
python -m pip install --upgrade pip
```

如果输出里出现 `:env:` 的 `index-url`，说明当前终端设置了 `PIP_INDEX_URL` 环境变量，pip 会优先使用环境变量。先执行 `set PIP_INDEX_URL=`（Linux 用 `unset PIP_INDEX_URL`）再重开命令行验证。

Linux 可临时使用：

```bash
export PIP_CONFIG_FILE="$PWD/deploy/pip.conf"
```

或由管理员复制到 `/etc/pip.conf`。平台本身不依赖 pip 包；该配置用于后续安装诊断/运维辅助包。

## 4. 启动

双击：

```text
deploy\windows\start_platform.bat
```

或命令行：

```bat
cd /d D:\bbu-fault-platform
python run.py --host 0.0.0.0 --port 8080
```

浏览器访问 `http://127.0.0.1:8080/`。首次启动会创建 `admin`，随机密码在：

```text
data\初始管理员密码.txt
```

端口冲突时改 `--port`，例如 `python run.py --port 8090`。Windows 防火墙首次弹窗时允许专用网络访问。

## 5. Windows 兼容性处理

本次修复了两类 POSIX 专属调用：

| 问题 | 处理 |
| --- | --- |
| `ModuleNotFoundError: No module named 'fcntl'` | 新增 `bbu/filelock.py`：Linux 用 `fcntl.flock`，Windows 用 `msvcrt.locking`；`updater.py` 和升级助手不再顶层 import `fcntl` |
| 技能超时/取消时调用 `os.killpg` | Windows 改用 `taskkill /PID <pid> /T /F` 结束进程树；POSIX 保持进程组信号 |
| 子进程 `start_new_session=True` | 仅 POSIX 传入；Windows 不传，避免不支持参数 |

Windows 下「系统信息 → 离线升级」默认不可用，页面会提示手动更新。更新方法：

1. 停止平台；
2. 备份 `data\` 和 `config.json`；
3. 解压新版本，覆盖程序目录（保留 `data\`、`config.json`）；
4. 重新启动。

## 5.1 工作目录切换与迁移包（1.10.0）

管理员在「系统信息 → 工作目录与迁移」可以：

1. **切换工作目录**：填写一个空目录或尚不存在的绝对路径，预检通过后提交；平台先在旧目录的 `updates` 下生成精简工作区，再在程序根写入 `pending_maintenance.json`，提示手动停止并重新启动。重启时 `run.py` 在打开数据库前完成目录替换、更新 `config.json`，再启动服务；旧工作目录保留，不自动删除。
2. **导出迁移包**：设置独立迁移口令（至少 12 位）并再次输入，再输入管理员密码。后台生成 `.bbumig` 加密包，可在迁移记录中下载。
3. **覆盖恢复**：选择 `.bbumig`、填写迁移口令和管理员密码，上传后校验、解密、解压到 `updates/<任务>/import-stage`，再写 `pending_maintenance.json`，重启后替换 `data / skills / cases`。当前数据先移动到 `backups/import-<任务ID>`；替换或启动失败会恢复旧目录。

迁移包**不包含**原始上传、案例原始输入、解压目录、缓存和登录会话；历史任务只保留元数据、结论、报告和证据。导入后：

- 任务详情可查看历史报告，但「重跑」按钮隐藏；直接调用重跑接口也会被拒绝；
- 复核时不能勾选「回流案例」；
- 案例列表显示「原始输入未迁移」；依赖原始输入的评测不会选中这些案例。

迁移包使用 ChaCha20-Poly1305 认证加密，不依赖 `cryptography` 包，因此 Windows 和 Linux 离线实例可以互相导入；纯 Python 实现速度约 0.7 MB/s，包越大导出/导入越慢。迁移口令和包必须分渠道保管，包内包含 `master.key` 与模型 Key 密文。

## 6. 常见问题

| 现象 | 处理 |
| --- | --- |
| `python` 不是内部命令 | 重装 Python 勾选 Add to PATH，或用 `py -3.13` |
| 启动报端口占用 | `python run.py --port 8090` |
| 无法写数据库 | 换到可写目录，不要放 Program Files |
| 中文乱码 | 设置 `PYTHONUTF8=1` 后重启命令行 |
| pip 源证书错误 | 确认已配置 `trusted-host = artsz.zte.com.cn`，并在办公网/VPN |
| 需要后台服务 | 可用 NSSM 等工具把 `python run.py` 注册为服务；平台不自带 Windows 服务安装器 |
| 切换工作目录后页面提示等待重启 | 这是 Windows 的正常流程；停止 `python run.py` 后重新启动，`pending_maintenance.json` 会在启动前自动应用 |
| 重启后任务显示「重启导致迁移任务中断」 | 检查 `pending_maintenance.json` 是否还在；应用失败会保留旧目录并把任务标为失败，修复目标目录权限后重新提交 |
