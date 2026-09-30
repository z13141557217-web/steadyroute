# 安装、升级与迁移

从 v0.5.1 起，稳航只有一个产品、一条安装路径：仓库根目录的 `install.command`。
首次安装、升级、换电脑和从旧版迁移都用它；从 git clone 运行和从发布包
`SteadyRoute-v<版本>.zip` 运行完全相同，两者目录结构一致：

```text
VERSION
install.command
uninstall.command
使用说明.txt                       （仅发布包）
config/route-policies.default.json
scripts/installer.py
src/steadyroute/…
```

`install.command` 只做一件事：确认有 Apple 命令行工具后运行
`/usr/bin/python3 scripts/installer.py install`。安装器只用 Python 标准库。

## 安装后的位置

| 用途 | 路径 |
|---|---|
| 程序与运行数据 | `~/Library/Application Support/SteadyRoute/` |
| 设置 | `~/Library/Application Support/SteadyRoute/config/route-policies.json` |
| 运行状态 | `~/Library/Application Support/SteadyRoute/state.json` |
| 写入 Clash 的记录 | `~/Library/Application Support/SteadyRoute/clash-applied.json` |
| AI 规则同步状态 | `~/Library/Application Support/SteadyRoute/ai-rules.json` |
| Clash 文件备份 | `~/Library/Application Support/SteadyRoute/clash-backups/`（最近 10 次） |
| 版本备份 | `~/Library/Application Support/SteadyRoute-backups/`（最近 3 个版本） |
| 已停用的旧开机自启 | `~/Library/Application Support/SteadyRoute-backups/legacy/` |
| 日志 | `~/Library/Logs/SteadyRoute/` |
| LaunchAgent | `~/Library/LaunchAgents/com.steadyroute.plist`（label `com.steadyroute`） |
| 看板 | `http://127.0.0.1:17654/`，设置页 `/settings` |

LaunchAgent 通过环境变量 `STEADYROUTE_BASE_DIR`、`STEADYROUTE_LOG_DIR` 显式指定上面两个
目录；直接运行 `weighted_router.py` 时默认值相同。`STEADYROUTE_POLICY_CONFIG` 可以指定
其他设置文件，只用于开发和测试。

## 安装与升级

在仓库目录或解压后的发布包目录运行：

```bash
./install.command
```

也可以在 Finder 中右键 `install.command` → 打开（首次需要这样）。不自动打开看板时：

```bash
python3 scripts/installer.py install --no-open
```

安装器按以下顺序执行，任何一步失败都会停止：

1. **预检**：必须是 macOS、Python ≥ 3.9、程序文件和默认设置完整。找不到 Clash Verge
   或它没在运行只给出提示，不阻止安装。
2. **停止旧服务**：停掉 `com.steadyroute`，以及任何运行 `weighted_router.py` 的其他
   LaunchAgent（例如 v0.5.1 之前位于 `~/Library/Application Support/Clash-Verge-Stability-Router`
   的旧服务，或 v0.5.0 的 `com.steadyroute.share`）。等待 17654 端口释放；端口被其他程序
   占用时重新启动旧服务并报错退出。
3. **备份**：已有安装时把整个程序目录复制到 `SteadyRoute-backups/<版本>-<时间>/`，只保留
   最近 3 份。
4. **写入程序**：逐个文件原子替换程序文件和 `VERSION`。
5. **设置**：已有新格式设置时原样保留；旧版的固定台湾 / 香港设置会转换成新格式，并附带
   一条 `migration` 建议（见下文）；都没有时使用默认设置。
6. **带过旧数据**：新目录还没有 `state.json` 或日志时，从旧安装复制过来。
7. **启动**：写入 plist 并 `launchctl bootstrap`，最多等待 40 秒，直到 `/api/status`
   报告刚安装的版本且 `profile` 为 `auto_lock`。
8. **失败回滚**：启动或健康检查失败时，恢复上一版本并重新启动；没有上一版本时删除新
   plist，并重新启动旧版服务。
9. **收尾**：只有新服务健康后，才把旧 LaunchAgent plist 移到
   `SteadyRoute-backups/legacy/`。旧程序目录保留在原处，不删除。

安装从不修改 Clash。重复运行同一版本等于重新安装；状态、历史和设置都保留。

## 从旧版迁移

v0.5.1 之前的固定台湾 / 香港方案（手写策略、静态节点名单、隐藏发现组）已经退役。安装器
检测到旧安装后自动迁移：

- 运行状态、节点历史和日志复制到新位置。
- 旧设置转换为统一的 `auto_lock` 设置；原来的业务检测地址写入
  `auto_lock.group_business_urls`，原来的线路记为 `migration` 建议。
- 安装完成后看板直接打开设置页，顶部显示“旧版线路待迁移”。确认前不向 Clash 写入任何
  内容。

在设置页接受迁移后：

- “AI 台湾家宽线路”成为 AI 家宽专线（台湾，名称不变）。
- “香港家宽自动备援”成为托管的家宽线路（香港，名称不变）。
- 两者都改为按国家与家宽自动筛选，不再依赖静态节点名单。
- 旧的发现组“SteadyRoute 发现·台湾家宽”“SteadyRoute 发现·香港家宽”被移除。

写入前会列出每条线路的节点增减和规则变化。原有分组定义保存在 `clash-applied.json` 中，
关闭专线或卸载时原样恢复。也可以选择“不迁移”，设置页不再提示。

换电脑：把仓库克隆或把发布包复制到新 Mac 后运行 `./install.command`。运行状态和设置
属于本机数据，不随仓库迁移；新机器从默认设置开始。

## 查看状态

```bash
python3 scripts/installer.py status
```

输出已安装的版本、是否在运行，以及每个分组锁定的国家和家宽候选数量。更多内容见看板和
[运维手册](OPERATIONS.md)。

## 回滚

安装失败会自动回滚，不需要人工操作。需要主动退回某个版本时，用那个版本的发布包或标签
重新安装（仅限 v0.5.1 及以后的版本）：

```bash
git switch --detach v0.5.1
./install.command
```

`SteadyRoute-backups/` 中的版本备份用于诊断和自动回滚；不要从不同备份手工拼接文件。

AI 家宽专线的回滚在设置页完成：关闭专线即恢复写入前的 Clash 分组和规则。

## 卸载

```bash
./uninstall.command
```

等价于 `python3 scripts/installer.py uninstall`。卸载顺序：先停止服务（避免它把专线写回），
再从 Clash 撤销稳航写入的分组和规则并恢复原有内容，最后删除 LaunchAgent、程序目录和
版本备份。日志默认保留；连日志一起删除：

```bash
python3 scripts/installer.py uninstall --purge
```

稳航曾向 Clash 写入专线而 Clash Verge 没有运行时，卸载会拒绝执行并提示先打开 Clash Verge；
撤销失败时服务保持停止、程序文件保留，修复后可再次卸载。

## 测试

`tests/test_installer.py` 通过可注入的钩子（`launchctl`、状态接口、端口、时钟）在临时目录
中运行完整流程，不触碰真实的 `~/Library`、LaunchAgent 或 Clash：全新安装、升级保留数据、
备份数量上限、启动失败回滚、端口占用、旧版迁移与失败恢复、卸载撤销 Clash 改动，以及从
发布包安装。
