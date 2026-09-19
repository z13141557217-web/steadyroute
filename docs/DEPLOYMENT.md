# 本地部署与回滚

## 安全边界

- 命令默认是 dry-run，不写生产、不停止或重启服务。
- 真实写入必须显式增加 `--apply`。
- 第一次生产写入必须在交互终端再次输入脚本显示的完整确认短语，不能在 CI 中绕过。
- 生产部署只接受干净工作树、通过 `./scripts/check.sh`、版本与 commit 匹配且当前 commit 带 `v<VERSION>` 标签的发布包。
- 不直接编辑生产目录；失败版本只作为诊断证据保留。

## 构建与预演

```bash
./scripts/check.sh
./scripts/build-release.sh
./scripts/deploy-local.sh
```

预演会验证工作树、全量测试、发布包外层 SHA-256、逐文件清单、`VERSION`、
`GIT_COMMIT`、`RELEASE.json`、Python 语法、LaunchAgent plist 和 Clash Verge
增强配置的必需结构。输出 `DRY-RUN validated` 后即结束，不创建备份，也不调用
`launchctl`。

## 首次生产部署

合并并创建 `v<VERSION>` 标签后，重新从该 commit 构建发布包，再运行：

```bash
./scripts/deploy-local.sh --apply
```

脚本会再次要求输入类似下方的完整短语：

```text
首次部署 /Users/nurture/Library/Application Support/Clash-Verge-Stability-Router
```

确认后按以下顺序执行：停止 LaunchAgent 并确认服务与监听端口都已退出、创建完整备份、校验备份、在同一文件系统
准备目标目录、以目录重命名原子切换、原子替换 plist、启动 LaunchAgent并做冒烟检查。

## 自动健康检查

部署和回滚只有同时满足以下条件才成功：

1. `launchctl print` 显示 `state = running`。
2. 本地状态接口返回合法 JSON，且服务状态为 `running`。
3. 香港和台湾两个必需代理组都存在。
4. 每个组的当前节点非空且属于其候选列表。
5. `service.started_at` 晚于部署前进程且不早于本次启动动作。
6. `updated_at` 晚于部署前状态、不早于新进程启动时间，并且默认不早于 90 秒前。

默认健康等待时间为 45 秒，覆盖至少一个 20 秒检测周期。`bootout` 返回失败、
LaunchAgent 仍存在或状态端口仍接受连接时，部署会在任何目标写入前终止。任一关键检查失败，脚本会停止失败版本、恢复切换前完整目录和 plist、重启并再次
执行同一套冒烟检查。失败目录和原因保存在备份根目录的 `diagnostics/` 下。

## 非生产演练路径

`--apply` 的生产模式只允许内置的三个精确默认路径。临时目录或开发环境 apply 必须
同时提供 `--allow-non-production --non-production-root <专用根目录>`；目标、备份和
plist 必须严格位于该根目录下且彼此不重叠。`/`、用户主目录、源码仓库、部分生产
路径组合以及宽泛或互相包含的路径都会被硬拒绝。默认 dry-run 不需要该开关。

## 备份内容

默认备份根目录：

```text
/Users/nurture/Library/Application Support/Clash-Verge-Stability-Router Backups
```

每个备份包含完整应用目录、LaunchAgent plist 和 `backup.json`。元数据记录版本、
commit、UTC 时间、每个文件的 SHA-256 以及清单总 SHA-256。回滚前会重新验证全部
散列，损坏的备份不会启用。

## 一键回滚

先预览将使用的最新完整备份：

```bash
./scripts/rollback-local.sh
```

确认目标后执行：

```bash
./scripts/rollback-local.sh --apply
```

也可以用 `--backup /绝对路径/到/备份目录` 指定版本。回滚自身仍会运行冒烟检查；
若回滚版本检查失败，会恢复回滚前版本，并保留失败证据。

## 临时目录演练

集成测试只使用 `TemporaryDirectory`、假的 `launchctl` 和本地文件状态响应，不访问
真实 Clash Verge、生产目录或真实 LaunchAgent：

```bash
python3 -m unittest tests.test_deploy -v
```

测试覆盖默认无写入、完整备份、成功部署、bootout 失败、旧服务或端口仍存活、旧状态
无法证明新周期、危险路径拒绝、激活后故障注入自动恢复、独立回滚以及脏工作树拒绝。
`STEADYROUTE_TEST_FAIL_AFTER_ACTIVATE` 仅允许非生产目标用于测试，
生产路径会拒绝故障注入。
