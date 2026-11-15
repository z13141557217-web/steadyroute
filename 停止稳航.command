#!/bin/sh
# 独立操作已安装的稳航，不执行安装或改动 Mihomo 启动项。
STEADYROUTE_ENTRY_DIR="$HOME/Library/Application Support/SteadyRoute"
if [ ! -f "$STEADYROUTE_ENTRY_DIR/service_cli.py" ]; then
  echo "请先安装支持总开关的稳航版本。"
  exit 1
fi
/usr/bin/python3 "$STEADYROUTE_ENTRY_DIR/service_cli.py" stop
