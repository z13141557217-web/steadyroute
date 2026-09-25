# 验收模拟环境

在一台机器上同时运行两个版本的稳航后台进程（例如 v0.4.2 与候选版本），各自连接一个
可编排的假 Mihomo 控制器，用同样的剧本制造故障并录制看板接口，用于上线前验收。

- `fake_mihomo.py`：Unix socket 假控制器，支持节点断开、不稳定节点、目标站点故障、
  本机断网、唤醒后短暂全部失败；记录选路、关闭连接与探测。
- `run_router.py`：把生产路径重定向到临时目录后以 `--daemon` 运行指定源码树，
  不会写入 `~/Library` 下的任何生产位置。
- `run_scenarios.py`：并行运行全部场景，输出一个 JSON 结果文件。

```bash
git worktree add ../steadyroute-v0.4.2 v0.4.2
python3 sim/run_scenarios.py --old ../steadyroute-v0.4.2 --new . --out /tmp/sim.json
```

全部场景约 6 分钟（真实时间）。本目录只用于验收，不进入发布包。
