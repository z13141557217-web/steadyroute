# 端到端演练与可交互预览（仅 Linux，开发用）

在隔离的假 Mac 目录里用真实的安装器、旧版（main）与当前版本程序完整走一遍：旧版运行 → `install.command`
迁移 → 设置页升级线路 → AI 分流体检 → 节点故障 → 关闭 / 重新开启专线 → 卸载。Clash Verge 的文件、内核校验
和 Mihomo 控制器是模拟的（`world.py`、`fake_core.py`、`fake_clash.py`）。

需要 PyYAML 和 Playwright（仅此目录使用，不进发布包）。**不要在 Mac 上运行**：它会在
`/Applications/Clash Verge.app` 下放一个模拟内核。

```bash
python3 sim/e2e/run.py /tmp/sr-e2e "$(command -v python3.9 || command -v python3)"
python3 sim/e2e/build_interactive.py
```

- `run.py WORKDIR PYTHON`：约 4 分钟；结果在 `WORKDIR/results.json`，看板与接口实录在 `WORKDIR/recording.json`。
  运行时占用 17654 端口。
- `build_interactive.py`：读取 `work/recording.json`（把 WORKDIR 设为 `sim/e2e/work`，或自行拷贝），生成
  `interactive/index.html`（真实看板回放实录）与 `interactive/settings.html`（真实设置页 + 浏览器内模拟后端）。
- `check_preview.py`：用 Playwright 把预览里的设置页操作点一遍（先在 `interactive/` 下起一个
  `python3 -m http.server 8765`）。
